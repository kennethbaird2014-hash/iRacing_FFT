"""
modal_panel.py — Qt widget providing the 5-tab modal analysis display.

Imports modal_analyzer.ModalAnalyzer for all computations.
SSA runs on a dedicated background QThread.
"""

import numpy as np
from collections import deque
from typing import List

import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel,
    QPushButton, QSpinBox, QDoubleSpinBox, QComboBox,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QColor

from modal_analyzer import ModalAnalyzer

SAMPLE_RATE = 360.0

RPM_CHANNELS = ['fl', 'fr', 'rl', 'rr', 'engine']
RPM_COLORS   = {
    'fl':     '#ff6b35', 'fr': '#4ecdc4',
    'rl':     '#45b7d1', 'rr': '#96ceb4',
    'engine': '#ffeaa7',
}
RPM_LABELS = {
    'fl': 'FL', 'fr': 'FR', 'rl': 'RL', 'rr': 'RR', 'engine': 'Engine',
}


# ── SSA background worker ─────────────────────────────────────────────────────
class _SSAWorker(QObject):
    finished = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._analyzer = ModalAnalyzer(SAMPLE_RATE)
        self._signal   = None
        self._window   = 128
        self._n_comp   = 4

    def set_params(self, signal: np.ndarray, window: int, n_comp: int):
        self._signal = signal.copy()
        self._window = window
        self._n_comp = n_comp

    def run(self):
        if self._signal is None:
            return
        result = self._analyzer.compute_ssa(self._signal, self._window, self._n_comp)
        self.finished.emit(result)


# ── Main panel ────────────────────────────────────────────────────────────────
class ModalAnalysisPanel(QWidget):
    """
    5-tab analysis panel:
      0 — Cepstrum / HPS (fundamental detection)
      1 — Order Analysis + Campbell diagram
      2 — Damping / ACF
      3 — Kurtogram (spectral kurtosis)
      4 — SSA (mode separation, on-demand)
    """

    auto_eq_requested = pyqtSignal(list)   # emits [(freq_hz, Q), …]
    CAMPBELL_ROWS = 150
    _SSA_COLORS   = ['#ff6b35','#4ecdc4','#45b7d1','#96ceb4',
                     '#ffeaa7','#dfe6e9','#b2bec3','#636e72']

    def __init__(self):
        super().__init__()
        self._analyzer      = ModalAnalyzer(SAMPLE_RATE)
        self._freq_bins     = None
        self._mag           = None       # linear magnitude (not dB)
        self._signal_buf    = None
        self._rpm_data      = {ch: 0.0 for ch in RPM_CHANNELS}
        self._sk_frames     = deque(maxlen=128)
        self._campbell_buf  = None
        self._active_chans  = {'fl'}
        self._last_camp_spd = -9999.0

        self._setup_ui()
        self._setup_ssa_thread()

    # ── Setup ─────────────────────────────────────────────────────────────────
    def _setup_ssa_thread(self):
        self._ssa_thread = QThread()
        self._ssa_worker = _SSAWorker()
        self._ssa_worker.moveToThread(self._ssa_thread)
        self._ssa_worker.finished.connect(self._on_ssa_done)
        self._ssa_thread.start()

    def _setup_ui(self):
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_fundamentals(), "Cepstrum / HPS")
        self._tabs.addTab(self._tab_order(),        "Order Analysis")
        self._tabs.addTab(self._tab_damping(),      "Damping (ACF)")
        self._tabs.addTab(self._tab_kurtogram(),    "Kurtogram")
        self._tabs.addTab(self._tab_ssa(),          "SSA")
        vb.addWidget(self._tabs)

    # ── Tab builders ──────────────────────────────────────────────────────────
    def _tab_fundamentals(self):
        w = QWidget(); vb = QVBoxLayout(w); vb.setContentsMargins(4,4,4,4)

        tb = QHBoxLayout()
        tb.addWidget(self._lbl("N harmonics:"))
        self._hps_n = QSpinBox(); self._hps_n.setRange(2,8); self._hps_n.setValue(4)
        self._hps_n.setFixedWidth(48)
        tb.addWidget(self._hps_n)
        self._fund_lbl = QLabel("Fundamental: —")
        self._fund_lbl.setStyleSheet("color:#00ff88;font-family:Consolas;font-size:10px;")
        tb.addSpacing(12); tb.addWidget(self._fund_lbl); tb.addStretch()
        auto_btn = QPushButton("Auto-EQ from HPS")
        auto_btn.clicked.connect(self._auto_eq_from_hps)
        tb.addWidget(auto_btn)
        vb.addLayout(tb)

        # Cepstrum plot
        self._cep_plot = self._make_plot("Quefrency (s)", "Cepstrum", 140)
        self._cep_curve  = self._cep_plot.plot(pen=pg.mkPen('#e67e00', width=1.2))
        self._cep_marker = pg.InfiniteLine(angle=90,
            pen=pg.mkPen('#00ff88', width=2, style=Qt.PenStyle.DashLine))
        self._cep_plot.addItem(self._cep_marker); self._cep_marker.setVisible(False)
        vb.addWidget(self._cep_plot, 1)

        self._add_sep(vb)

        # HPS plot
        self._hps_plot = self._make_plot("Frequency (Hz)", "Amplitude", 140)
        self._hps_spec  = self._hps_plot.plot(pen=pg.mkPen((0,190,100,100), width=1))
        self._hps_curve = self._hps_plot.plot(pen=pg.mkPen('#ff4444', width=1.5))
        self._hps_mark  = pg.InfiniteLine(angle=90,
            pen=pg.mkPen('#ffff00', width=2, style=Qt.PenStyle.DashLine))
        self._hps_plot.addItem(self._hps_mark); self._hps_mark.setVisible(False)
        vb.addWidget(self._hps_plot, 1)
        return w

    def _tab_order(self):
        w = QWidget(); vb = QVBoxLayout(w); vb.setContentsMargins(4,4,4,4)

        tb = QHBoxLayout()
        self._chan_btns = {}
        for ch in RPM_CHANNELS:
            btn = QPushButton(RPM_LABELS[ch])
            btn.setCheckable(True); btn.setFixedWidth(56)
            btn.setStyleSheet(
                f"QPushButton:checked{{background:{RPM_COLORS[ch]};color:#000;}}")
            btn.toggled.connect(lambda checked, c=ch: self._on_chan_toggle(c, checked))
            self._chan_btns[ch] = btn; tb.addWidget(btn)
        self._chan_btns['fl'].setChecked(True)

        tb.addSpacing(12); tb.addWidget(self._lbl("Campbell:"))
        self._camp_mode = QComboBox()
        self._camp_mode.addItems(["Continuous", "Speed threshold"])
        self._camp_mode.setFixedWidth(130); tb.addWidget(self._camp_mode)
        self._camp_thr = QDoubleSpinBox()
        self._camp_thr.setRange(1,50); self._camp_thr.setValue(5); self._camp_thr.setSuffix(" mph")
        self._camp_thr.setFixedWidth(72); tb.addWidget(self._camp_thr)
        tb.addStretch()
        self._rpm_lbl = QLabel("No rotation data")
        self._rpm_lbl.setStyleSheet("color:#666;font-family:Consolas;font-size:9px;")
        tb.addWidget(self._rpm_lbl)
        vb.addLayout(tb)

        # Order spectrum
        self._ord_plot = self._make_plot("Order (× rotation freq)", "Amplitude", 150)
        self._ord_curves = {}
        for ch in RPM_CHANNELS:
            c = self._ord_curves[ch] = self._ord_plot.plot(
                pen=pg.mkPen(RPM_COLORS[ch], width=1.3), name=RPM_LABELS[ch])
            c.setVisible(ch in self._active_chans)
        for o in range(1, 21):
            self._ord_plot.addItem(pg.InfiniteLine(pos=o, angle=90,
                pen=pg.mkPen('#333', width=1, style=Qt.PenStyle.DotLine)))
        vb.addWidget(self._ord_plot, 1)

        self._add_sep(vb)
        vb.addWidget(self._lbl("Campbell diagram  (x = order, y = time/speed)", '#555'))

        # Campbell image
        self._camp_plot = self._make_plot("Order", "Time →", 120)
        self._camp_img  = pg.ImageItem()
        self._camp_img.setColorMap(pg.colormap.get("plasma"))
        self._camp_plot.addItem(self._camp_img)
        vb.addWidget(self._camp_plot, 1)
        return w

    def _tab_damping(self):
        w = QWidget(); vb = QVBoxLayout(w); vb.setContentsMargins(4,4,4,4)

        self._acf_plot = self._make_plot("Lag (s)", "Normalised ACF", 200)
        self._acf_plot.setYRange(-1.05, 1.05)
        self._acf_plot.addItem(pg.InfiniteLine(pos=0, angle=0,
            pen=pg.mkPen('#333', style=Qt.PenStyle.DashLine)))
        self._acf_curve = self._acf_plot.plot(pen=pg.mkPen('#45b7d1', width=1.2))
        vb.addWidget(self._acf_plot, 1)

        self._damp_tbl = QTableWidget(0, 4)
        self._damp_tbl.setHorizontalHeaderLabels(["Freq (Hz)", "Damping ζ", "Suggested Q", "Note"])
        self._damp_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._damp_tbl.verticalHeader().hide()
        self._damp_tbl.setMaximumHeight(110)
        self._damp_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._style_table(self._damp_tbl)
        vb.addWidget(self._damp_tbl)
        return w

    def _tab_kurtogram(self):
        w = QWidget(); vb = QVBoxLayout(w); vb.setContentsMargins(4,4,4,4)

        info = QLabel(
            "Spectral Kurtosis: high values indicate impulsive (spike/kerb) content at that frequency.\n"
            "Red = SK > 3 (impulsive).  Use to guide slew-rate limiter threshold.")
        info.setStyleSheet("color:#666;font-size:9px;font-family:Consolas;")
        vb.addWidget(info)

        self._kurt_plot = self._make_plot("Frequency (Hz)", "Spectral Kurtosis", 200)
        self._kurt_spec  = self._kurt_plot.plot(pen=pg.mkPen((0,190,100,60), width=1))
        self._kurt_curve = self._kurt_plot.plot(pen=pg.mkPen('#e53935', width=1.5))
        self._kurt_plot.addLine(y=3.0,
            pen=pg.mkPen('#ffaa00', width=1, style=Qt.PenStyle.DashLine))
        vb.addWidget(self._kurt_plot, 1)

        rb = QHBoxLayout()
        self._slew_rec = QLabel("Recommended slew limit: —")
        self._slew_rec.setStyleSheet("color:#ffaa00;font-family:Consolas;font-size:10px;")
        rb.addWidget(self._slew_rec); rb.addStretch()
        vb.addLayout(rb)
        return w

    def _tab_ssa(self):
        w = QWidget(); vb = QVBoxLayout(w); vb.setContentsMargins(4,4,4,4)

        tb = QHBoxLayout()
        tb.addWidget(self._lbl("Components:"))
        self._ssa_n = QSpinBox(); self._ssa_n.setRange(2,8); self._ssa_n.setValue(4)
        self._ssa_n.setFixedWidth(48); tb.addWidget(self._ssa_n)
        tb.addWidget(self._lbl("Window:"))
        self._ssa_win = QSpinBox(); self._ssa_win.setRange(32,512)
        self._ssa_win.setValue(128); self._ssa_win.setSingleStep(32)
        self._ssa_win.setFixedWidth(56); tb.addWidget(self._ssa_win)
        self._ssa_btn = QPushButton("Run SSA")
        self._ssa_btn.setFixedWidth(80)
        self._ssa_btn.clicked.connect(self._run_ssa)
        tb.addWidget(self._ssa_btn)
        self._ssa_st = QLabel("Idle")
        self._ssa_st.setStyleSheet("color:#555;font-family:Consolas;font-size:10px;")
        tb.addWidget(self._ssa_st); tb.addStretch()
        vb.addLayout(tb)

        self._ssa_plot = self._make_plot("Time (s)", "Amplitude (Nm)", 250)
        self._ssa_curves = []
        for col in self._SSA_COLORS:
            c = self._ssa_plot.plot(pen=pg.mkPen(col, width=1.2))
            c.setVisible(False); self._ssa_curves.append(c)
        vb.addWidget(self._ssa_plot, 1)

        self._ssa_tbl = QTableWidget(0, 3)
        self._ssa_tbl.setHorizontalHeaderLabels(["Component", "Freq (Hz)", "Strength"])
        self._ssa_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._ssa_tbl.verticalHeader().hide()
        self._ssa_tbl.setMaximumHeight(110)
        self._ssa_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._style_table(self._ssa_tbl)
        vb.addWidget(self._ssa_tbl)
        return w

    # ── Public update API ─────────────────────────────────────────────────────
    def update_spectrum(self, freqs: np.ndarray, mag_linear: np.ndarray,
                        signal_buffer: np.ndarray, speed_mph: float = 0.0):
        """Called from MainWindow._on_spectrum; only refreshes the active tab."""
        self._freq_bins  = freqs
        self._mag        = mag_linear
        self._signal_buf = signal_buffer
        self._speed_mph  = speed_mph
        self._sk_frames.append(mag_linear.copy())

        tab = self._tabs.currentIndex()
        if tab == 0: self._refresh_fundamentals()
        elif tab == 1: self._refresh_order(); self._update_campbell()
        elif tab == 2: self._refresh_damping()
        elif tab == 3: self._refresh_kurtogram()
        # tab 4 (SSA) only refreshes on button press

    def update_rpm(self, rpm_data: dict):
        """Called when IRacingThread emits rpm_ready."""
        self._rpm_data = rpm_data
        parts = [f"{RPM_LABELS[ch]} {hz:.1f}Hz"
                 for ch, hz in rpm_data.items() if hz > 0.5]
        self._rpm_lbl.setText("  ".join(parts) or "No rotation")

    # ── Tab refresh methods ────────────────────────────────────────────────────
    def _refresh_fundamentals(self):
        if self._freq_bins is None: return
        # Cepstrum
        cep, quefrency, fund_cep = self._analyzer.compute_cepstrum(
            self._mag, self._freq_bins)
        mask = quefrency <= 0.5
        self._cep_curve.setData(x=quefrency[mask], y=cep[mask])
        if fund_cep:
            self._cep_marker.setValue(1.0 / fund_cep)
            self._cep_marker.setVisible(True)

        # HPS
        hps, fund_idx = self._analyzer.compute_hps(self._mag, self._hps_n.value())
        self._hps_spec.setData(x=self._freq_bins, y=self._mag)
        self._hps_curve.setData(x=self._freq_bins, y=hps)
        fund_hz_hps = self._freq_bins[fund_idx] if fund_idx < len(self._freq_bins) else 0
        self._hps_mark.setValue(fund_hz_hps); self._hps_mark.setVisible(fund_hz_hps > 0)
        self._fund_lbl.setText(
            f"Fundamental — Cep: {fund_cep:.2f} Hz" if fund_cep else
            f"Fundamental — HPS: {fund_hz_hps:.2f} Hz")
        # Store for auto-EQ
        self._hps_fund = fund_hz_hps

    def _refresh_order(self):
        if self._freq_bins is None: return
        for ch in RPM_CHANNELS:
            if ch not in self._active_chans: continue
            f_rot = self._rpm_data.get(ch, 0.0)
            orders, mag_o = self._analyzer.compute_order_spectrum(
                self._mag, self._freq_bins, f_rot)
            if len(orders): self._ord_curves[ch].setData(x=orders, y=mag_o)

    def _update_campbell(self):
        ch    = next((c for c in RPM_CHANNELS if c in self._active_chans), 'fl')
        f_rot = self._rpm_data.get(ch, 0.0)
        if f_rot <= 0: return

        mode = self._camp_mode.currentText()
        add  = (mode == "Continuous" or
                abs(self._speed_mph - self._last_camp_speed) >= self._camp_thr.value())
        if not add: return
        self._last_camp_speed = self._speed_mph

        orders, mag_o = self._analyzer.compute_order_spectrum(
            self._mag, self._freq_bins, f_rot)
        if not len(orders): return
        n = len(orders)
        if self._campbell_buf is None or self._campbell_buf.shape[1] != n:
            self._campbell_buf = np.zeros((self.CAMPBELL_ROWS, n), dtype=np.float32)
        self._campbell_buf = np.roll(self._campbell_buf, 1, axis=0)
        self._campbell_buf[0] = mag_o.astype(np.float32)

        self._camp_img.setImage(
            self._campbell_buf.T, autoLevels=False, autoDownsample=True)
        self._camp_img.setRect(pg.QtCore.QRectF(0, 0, orders[-1], self.CAMPBELL_ROWS))

    def _refresh_damping(self):
        if self._signal_buf is None or len(self._signal_buf) < 32: return
        acf, lags = self._analyzer.compute_acf(np.array(self._signal_buf))
        self._acf_curve.setData(x=lags[:len(lags)//4], y=acf[:len(acf)//4])

        # For each detected HPS fundamental estimate damping
        if not hasattr(self, '_hps_fund') or not self._hps_fund: return
        zeta = self._analyzer.estimate_damping_from_acf(acf, self._hps_fund)
        if zeta:
            q_sug = 1.0 / (2.0 * zeta)
            self._damp_tbl.setRowCount(1)
            for col, val in enumerate([
                    f"{self._hps_fund:.2f}", f"{zeta:.4f}",
                    f"{q_sug:.1f}", "← use for EQ Q"]):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._damp_tbl.setItem(0, col, item)

    def _refresh_kurtogram(self):
        if len(self._sk_frames) < 8 or self._freq_bins is None: return
        frames = np.array(list(self._sk_frames))
        sk = self._analyzer.compute_spectral_kurtosis(frames)
        self._kurt_spec.setData(x=self._freq_bins, y=self._mag / (self._mag.max()+1e-10))
        self._kurt_curve.setData(x=self._freq_bins, y=sk)
        # Recommend slew limit from highest-SK band
        high_mask = sk > 3.0
        if high_mask.any():
            worst_f = self._freq_bins[np.argmax(sk * high_mask)]
            # Rough heuristic: max usable delta ≈ 1 Nm per kHz of worst band
            rec = max(0.1, round(20.0 / (worst_f + 1.0), 2))
            self._slew_rec.setText(f"Recommended slew limit: {rec:.2f} Nm/sample  "
                                   f"(worst impulsive band: {worst_f:.1f} Hz)")
        else:
            self._slew_rec.setText("Spectral Kurtosis < 3 everywhere — no impulsive content detected")

    # ── SSA (background thread) ────────────────────────────────────────────────
    def _run_ssa(self):
        if self._signal_buf is None: return
        self._ssa_btn.setEnabled(False)
        self._ssa_st.setText("Running…")
        sig = np.array(self._signal_buf)
        self._ssa_worker.set_params(sig, self._ssa_win.value(), self._ssa_n.value())
        # Invoke run() on the worker's thread
        from PyQt6.QtCore import QMetaObject, Qt as _Qt
        QMetaObject.invokeMethod(self._ssa_worker, "run", _Qt.ConnectionType.QueuedConnection)

    def _on_ssa_done(self, components: List[dict]):
        self._ssa_btn.setEnabled(True)
        self._ssa_st.setText(f"Done — {len(components)} components")
        t_axis = np.arange(len(self._signal_buf)) / SAMPLE_RATE if self._signal_buf else []
        self._ssa_tbl.setRowCount(0)
        for i, (comp, curve) in enumerate(zip(components, self._ssa_curves)):
            curve.setData(x=t_axis, y=comp['signal']); curve.setVisible(True)
            r = self._ssa_tbl.rowCount(); self._ssa_tbl.insertRow(r)
            for col, val in enumerate([f"#{i+1}", f"{comp['frequency']:.2f}",
                                        f"{comp['strength']:.1f}"]):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._ssa_tbl.setItem(r, col, item)
        for curve in self._ssa_curves[len(components):]:
            curve.setVisible(False)

    # ── Auto-EQ ───────────────────────────────────────────────────────────────
    def _auto_eq_from_hps(self):
        if not hasattr(self, '_hps_fund') or not self._hps_fund: return
        if self._freq_bins is None: return
        bands = []
        f0 = self._hps_fund
        nyquist = self._freq_bins[-1]
        h = 1
        while f0 * h <= nyquist:
            fh = f0 * h
            result = self._analyzer.compute_half_power_q(self._mag, self._freq_bins, fh)
            q = result['q'] if result else 4.0
            bands.append((fh, min(max(q, 0.5), 50.0)))
            h += 1
        self.auto_eq_requested.emit(bands)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _on_chan_toggle(self, ch: str, checked: bool):
        if checked: self._active_chans.add(ch)
        else:       self._active_chans.discard(ch)
        if ch in self._ord_curves:
            self._ord_curves[ch].setVisible(checked)

    @staticmethod
    def _make_plot(xlabel, ylabel, min_h=150):
        p = pg.PlotWidget()
        p.setLabel("bottom", xlabel); p.setLabel("left", ylabel)
        p.showGrid(x=True, y=True, alpha=0.2)
        p.getAxis("left").setWidth(45)
        p.setMinimumHeight(min_h)
        return p

    @staticmethod
    def _lbl(text, color='#888'):
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{color};font-size:10px;font-family:Consolas;")
        return lb

    @staticmethod
    def _add_sep(layout):
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#333;"); layout.addWidget(sep)

    @staticmethod
    def _style_table(t: QTableWidget):
        t.setStyleSheet(
            "QTableWidget{background:#1e1e1e;color:#ddd;"
            "font-family:Consolas;font-size:10px;}"
            "QHeaderView::section{background:#252525;color:#aaa;"
            "border:1px solid #333;font-size:10px;padding:2px;}")

    def closeEvent(self, ev):
        self._ssa_thread.quit(); self._ssa_thread.wait(2000)
        super().closeEvent(ev)
