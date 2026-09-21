#!/usr/bin/env python3
"""
FFB Frequency Analyzer  v1.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
New in v1.1:
  • Always-on-top window
  • Frequency range extended to 100 Hz (requires 360 Hz iRacing telemetry)
  • Save / load EQ presets (stored in presets/ subfolder)
  • Detect Peaks — auto-generates EQ bands from prominent spectral peaks
  • Log-scale X axis toggle (spectrum + waterfall stay in sync)
  • X-axis view lock between spectrum and waterfall
  • Larger, resizable EQ band panel

Author: generated for Kenneth Baird
"""

import sys, time, math, json
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

from scipy import signal as scipy_signal

import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QGroupBox, QDoubleSpinBox,
    QCheckBox, QComboBox, QScrollArea,
    QSplitter, QFrame, QSizePolicy, QLineEdit,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QPainter, QLinearGradient, QPen, QColor, QFont, QPalette

try:
    import irsdk
    IRSDK_AVAILABLE = True
except ImportError:
    IRSDK_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE      = 360
FFT_SIZE         = 1024
UPDATE_INTERVAL  = 2
MAX_FREQ         = 100.0
WATERFALL_ROWS   = 200
EMA_ALPHA        = 0.20
MAX_TORQUE_NM    = 20.0
NYQUIST_60HZ     = 30.0

PRESET_DIR = Path(__file__).parent / "presets"


# ── EQ Band ───────────────────────────────────────────────────────────────────
@dataclass
class EQBand:
    freq:        float = 5.0
    gain_db:     float = 0.0
    q:           float = 1.4
    filter_type: str   = "peak"
    enabled:     bool  = True

    def get_sos(self, fs: float) -> np.ndarray:
        wc    = max(1e-4, min(2.0 * math.pi * self.freq / fs, math.pi - 1e-4))
        A     = 10.0 ** (self.gain_db / 40.0)
        sin_w = math.sin(wc);  cos_w = math.cos(wc)

        if self.filter_type == "peak":
            alpha = sin_w / (2.0 * self.q)
            b0, b1, b2 = 1+alpha*A, -2*cos_w, 1-alpha*A
            a0, a1, a2 = 1+alpha/A, -2*cos_w, 1-alpha/A

        elif self.filter_type == "lowshelf":
            alpha = sin_w / 2.0 * math.sqrt((A+1/A)*(1/self.q-1)+2)
            sq = math.sqrt(A)
            b0 = A*((A+1)-(A-1)*cos_w+2*sq*alpha)
            b1 = 2*A*((A-1)-(A+1)*cos_w)
            b2 = A*((A+1)-(A-1)*cos_w-2*sq*alpha)
            a0 = (A+1)+(A-1)*cos_w+2*sq*alpha
            a1 = -2*((A-1)+(A+1)*cos_w)
            a2 = (A+1)+(A-1)*cos_w-2*sq*alpha

        else:  # highshelf
            alpha = sin_w / 2.0 * math.sqrt((A+1/A)*(1/self.q-1)+2)
            sq = math.sqrt(A)
            b0 = A*((A+1)+(A-1)*cos_w+2*sq*alpha)
            b1 = -2*A*((A-1)+(A+1)*cos_w)
            b2 = A*((A+1)+(A-1)*cos_w-2*sq*alpha)
            a0 = (A+1)-(A-1)*cos_w+2*sq*alpha
            a1 = 2*((A-1)-(A+1)*cos_w)
            a2 = (A+1)-(A-1)*cos_w-2*sq*alpha

        b = np.array([b0/a0, b1/a0, b2/a0])
        a = np.array([1.0,   a1/a0, a2/a0])
        return np.array([[b[0], b[1], b[2], 1.0, a[1], a[2]]])


# ── Preset manager ────────────────────────────────────────────────────────────
class PresetManager:
    def __init__(self):
        PRESET_DIR.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, bands: List[EQBand]):
        data = {
            "name": name,
            "created": datetime.now().isoformat(),
            "bands": [{"freq": b.freq, "gain_db": b.gain_db, "q": b.q,
                       "filter_type": b.filter_type, "enabled": b.enabled}
                      for b in bands],
        }
        (PRESET_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))

    def list_presets(self) -> List[str]:
        return sorted(p.stem for p in PRESET_DIR.glob("*.json"))

    def load(self, name: str) -> List[EQBand]:
        data = json.loads((PRESET_DIR / f"{name}.json").read_text())
        return [EQBand(**b) for b in data["bands"]]

    def delete(self, name: str):
        p = PRESET_DIR / f"{name}.json"
        if p.exists():
            p.unlink()


# ── iRacing / demo thread ─────────────────────────────────────────────────────
class IRacingThread(QThread):
    sample_ready   = pyqtSignal(float)
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True

    def run(self):
        if not IRSDK_AVAILABLE:
            self._run_demo()
        else:
            self._run_iracing()

    def _run_iracing(self):
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        ir = irsdk.IRSDK()
        connected = False
        last_tick = -1

        while self._running:
            if not connected:
                self.status_changed.emit("Waiting for iRacing...")
                try:
                    if ir.startup():
                        connected = True
                        try:
                            ir.broadcast_msg(14, SAMPLE_RATE, 0)
                        except Exception:
                            pass
                        self.status_changed.emit(
                            f"Connected  ({SAMPLE_RATE} Hz requested)")
                    else:
                        time.sleep(0.1)
                        continue
                except Exception:
                    time.sleep(0.5)
                    continue

            try:
                if not ir.is_connected:
                    connected = False; last_tick = -1
                    self.status_changed.emit("iRacing disconnected")
                    ir.shutdown(); ir = irsdk.IRSDK()
                    continue

                ir.freeze_var_buffer_latest()
                tick = ir["SessionTick"]
                if tick is not None and tick != last_tick:
                    last_tick = tick
                    torque = ir["SteeringWheelTorque"]
                    if torque is not None:
                        try:
                            val = float(torque)
                            if math.isfinite(val):
                                self.sample_ready.emit(val)
                        except (TypeError, ValueError):
                            pass
                time.sleep(0.001)

            except Exception as exc:
                connected = False; last_tick = -1
                self.status_changed.emit(f"Error: {exc}")
                time.sleep(0.5)

    def _run_demo(self):
        self.status_changed.emit("DEMO  —  install pyirsdk for live data")
        dt  = 1.0 / SAMPLE_RATE
        rng = np.random.default_rng(42)
        t   = 0.0
        while self._running:
            sig = (
                3.5 * math.sin(2*math.pi* 1.8*t) +
                2.0 * math.sin(2*math.pi* 4.3*t) +
                1.2 * math.sin(2*math.pi* 9.7*t) +
                0.8 * math.sin(2*math.pi*17.5*t) +
                0.5 * math.sin(2*math.pi*35.0*t) +
                0.3 * math.sin(2*math.pi*62.0*t) +
                0.2 * math.sin(2*math.pi*88.0*t) +
                0.3 * float(rng.standard_normal())
            )
            if rng.random() < 0.004:
                sig += 6.0 * math.exp(-((t % 0.08) * 30.0))
            self.sample_ready.emit(float(np.clip(sig, -MAX_TORQUE_NM, MAX_TORQUE_NM)))
            t += dt
            time.sleep(dt)

    def stop(self):
        self._running = False
        self.wait(2000)


# ── FFT processor ─────────────────────────────────────────────────────────────
class FFTProcessor(QObject):
    spectrum_ready = pyqtSignal(object, object, float)

    def __init__(self):
        super().__init__()
        self._buf       = deque(maxlen=FFT_SIZE)
        self._window    = np.hanning(FFT_SIZE)
        self._hop_count = 0
        self._smooth    : Optional[np.ndarray] = None
        self.freq_bins  = np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE)

    def add_sample(self, v: float):
        self._buf.append(v)
        self._hop_count += 1
        if len(self._buf) >= FFT_SIZE and self._hop_count >= UPDATE_INTERVAL:
            self._hop_count = 0
            self._compute()

    def _compute(self):
        data   = np.array(self._buf) - np.mean(self._buf)
        mag    = np.abs(np.fft.rfft(data * self._window)) / (FFT_SIZE / 2)
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-10))
        self._smooth = (mag_db if self._smooth is None
                        else EMA_ALPHA * mag_db + (1 - EMA_ALPHA) * self._smooth)
        self.spectrum_ready.emit(
            self.freq_bins, self._smooth.copy(), float(np.sqrt(np.mean(data**2))))


# ── Strength meter ────────────────────────────────────────────────────────────
class StrengthMeter(QWidget):
    _HOLD = 60
    def __init__(self):
        super().__init__()
        self._v = 0.0; self._pk = 0.0; self._pt = 0
        self.setMinimumWidth(52); self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_value(self, rms: float):
        self._v = min(rms / MAX_TORQUE_NM, 1.0)
        if self._v >= self._pk:
            self._pk = self._v; self._pt = self._HOLD
        elif self._pt > 0:
            self._pt -= 1
        else:
            self._pk = max(self._pk - 0.008, self._v)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bx, bw = 8, w - 16
        p.fillRect(bx, 0, bw, h, QColor(25, 25, 25))
        fh = int(h * self._v)
        if fh > 0:
            g = QLinearGradient(0, h, 0, 0)
            g.setColorAt(0.00, QColor(0,   210,  60))
            g.setColorAt(0.65, QColor(210, 210,   0))
            g.setColorAt(1.00, QColor(220,  40,   0))
            p.fillRect(bx, h - fh, bw, fh, g)
        py = int(h * (1.0 - self._pk))
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawLine(bx, py, bx + bw, py)
        p.setPen(QPen(QColor(70, 70, 70), 1))
        p.drawRect(bx, 0, bw - 1, h - 1)
        p.end()


# ── EQ band row ───────────────────────────────────────────────────────────────
class EQBandRow(QWidget):
    changed          = pyqtSignal()
    remove_requested = pyqtSignal(object)
    _TL = ["Peak", "Low Shelf", "High Shelf"]
    _TK = ["peak", "lowshelf",  "highshelf"]

    def __init__(self, band: EQBand, color: QColor):
        super().__init__()
        self.band = band; self._color = color
        self._build()

    def _build(self):
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(6)
        sw = QFrame(); sw.setFixedSize(8, 22)
        sw.setStyleSheet(f"background:{self._color.name()};border-radius:2px;")
        row.addWidget(sw)
        self._en = QCheckBox()
        self._en.setChecked(self.band.enabled)
        self._en.toggled.connect(self._sync)
        row.addWidget(self._en)
        self._type = QComboBox()
        self._type.addItems(self._TL)
        self._type.setCurrentIndex(self._TK.index(self.band.filter_type))
        self._type.currentIndexChanged.connect(self._sync)
        self._type.setFixedWidth(95)
        row.addWidget(self._type)
        specs = [("Freq",  0.3, 99.0,  0.5, 1, " Hz", 78, self.band.freq),
                 ("Gain", -24,  24.0,  0.5, 1, " dB", 78, self.band.gain_db),
                 ("Q",    0.1,  10.0,  0.1, 2, "",    65, self.band.q)]
        self._freq = self._gain = self._q = None
        spinboxes = []
        for label, lo, hi, step, dec, suf, w, val in specs:
            row.addWidget(self._lbl(label))
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi); sp.setSuffix(suf)
            sp.setSingleStep(step); sp.setDecimals(dec)
            sp.setFixedWidth(w); sp.setValue(val)
            sp.valueChanged.connect(self._sync)
            spinboxes.append(sp)
            row.addWidget(sp)
        self._freq, self._gain, self._q = spinboxes
        row.addStretch()
        rem = QPushButton("✕"); rem.setFixedWidth(26)
        rem.setToolTip("Remove band")
        rem.clicked.connect(lambda: self.remove_requested.emit(self))
        row.addWidget(rem)

    @staticmethod
    def _lbl(t):
        lb = QLabel(t); lb.setStyleSheet("color:#888;font-size:10px;"); return lb

    def _sync(self):
        self.band.enabled     = self._en.isChecked()
        self.band.filter_type = self._TK[self._type.currentIndex()]
        self.band.freq        = self._freq.value()
        self.band.gain_db     = self._gain.value()
        self.band.q           = self._q.value()
        self.changed.emit()


# ── Main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    _BAND_COLORS = [
        QColor(255, 160,   0), QColor(  0, 200, 255),
        QColor(200,   0, 255), QColor(255,  60,  60),
        QColor( 60, 255, 120), QColor(255, 255,   0),
        QColor(255, 100, 180), QColor(100, 200, 255),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFB Frequency Analyzer  v1.1")
        self.setMinimumSize(1100, 820)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._eq_bands     : List[EQBand]         = []
        self._eq_rows      : List[EQBandRow]       = []
        self._eq_curves    : List[pg.PlotDataItem] = []
        self._eq_sum_curve : Optional[pg.PlotDataItem] = None
        self._presets      = PresetManager()
        self._log_mode     = False
        self._range_syncing = False
        self._last_freqs   : Optional[np.ndarray] = None
        self._last_mag_db  : Optional[np.ndarray] = None
        n_bins = FFT_SIZE // 2 + 1
        self._wf_buf = np.full((WATERFALL_ROWS, n_bins), -80.0, dtype=np.float32)
        pg.setConfigOptions(antialias=True, background="#0d0d0d")
        self._build_ui()
        self._start_pipeline()

    def _build_ui(self):
        root  = QWidget(); self.setCentralWidget(root)
        vmain = QVBoxLayout(root)
        vmain.setSpacing(4); vmain.setContentsMargins(8, 8, 8, 8)
        hdr = QHBoxLayout()
        title = QLabel("FFB FREQUENCY ANALYZER")
        title.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        title.setStyleSheet("color:#00ff88;")
        hdr.addWidget(title); hdr.addStretch()
        self._status = QLabel("Initialising...")
        self._status.setFont(QFont("Consolas", 9))
        self._status.setStyleSheet("color:#666;")
        hdr.addWidget(self._status)
        vmain.addLayout(hdr)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vmain.addWidget(vsplit, 1)
        r1  = QWidget()
        r1h = QHBoxLayout(r1); r1h.setContentsMargins(0,0,0,0); r1h.setSpacing(6)
        self._sp = pg.PlotWidget()
        self._sp.setLabel("left",   "Level (dBFS)")
        self._sp.setLabel("bottom", "Frequency (Hz)")
        self._sp.setXRange(0, MAX_FREQ)
        self._sp.setYRange(-80, 5)
        self._sp.showGrid(x=True, y=True, alpha=0.25)
        self._sp.setMinimumHeight(210)
        self._sp.addItem(pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(color="#333", style=Qt.PenStyle.DashLine)))
        self._nyq_line = pg.InfiniteLine(
            pos=NYQUIST_60HZ, angle=90,
            pen=pg.mkPen(color="#ff4444", width=1, style=Qt.PenStyle.DashLine),
            label="Nyquist\n(60 Hz src)",
            labelOpts={"color": "#ff6666", "fill": (30,0,0,120),
                       "position": 0.90, "movable": False},
        )
        self._sp.addItem(self._nyq_line)
        freqs  = np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE)
        n_bins = len(freqs)
        self._spectrum_curve = pg.PlotCurveItem(
            x=freqs, y=np.full(n_bins, -80.0),
            pen=pg.mkPen(color=(0, 190, 100, 255), width=1.2),
            brush=pg.mkBrush(0, 190, 100, 70),
            fillLevel=-80.0,
        )
        self._sp.addItem(self._spectrum_curve)
        self._eq_sum_curve = self._sp.plot(
            pen=pg.mkPen(color="#ffffff", width=2, style=Qt.PenStyle.DashLine))
        sp_toolbar = QHBoxLayout()
        self._log_cb = QCheckBox("Log scale")
        self._log_cb.setStyleSheet("color:#aaa; font-size:10px;")
        self._log_cb.toggled.connect(self._toggle_log)
        sp_toolbar.addStretch(); sp_toolbar.addWidget(self._log_cb)
        sp_box = QWidget()
        sp_vb  = QVBoxLayout(sp_box); sp_vb.setContentsMargins(0,0,0,0); sp_vb.setSpacing(2)
        sp_vb.addLayout(sp_toolbar)
        sp_vb.addWidget(self._sp, 1)
        r1h.addWidget(sp_box, 1)
        self._sp.getViewBox().sigXRangeChanged.connect(self._on_sp_range_changed)
        sm_box = QGroupBox("RMS")
        sm_vb  = QVBoxLayout(sm_box); sm_vb.setContentsMargins(4,14,4,6)
        self._meter   = StrengthMeter()
        self._rms_lbl = QLabel("0.0 Nm")
        self._rms_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rms_lbl.setFont(QFont("Consolas", 10))
        self._rms_lbl.setStyleSheet("color:#00ff88;")
        sm_vb.addWidget(self._meter, 1); sm_vb.addWidget(self._rms_lbl)
        sm_box.setFixedWidth(78)
        r1h.addWidget(sm_box)
        vsplit.addWidget(r1)
        wf_box = QGroupBox("Waterfall  (newest at bottom  —  X-axis locked to spectrum)")
        wf_vb  = QVBoxLayout(wf_box); wf_vb.setContentsMargins(4,14,4,4)
        self._wf_plot = pg.PlotWidget()
        self._wf_plot.setLabel("bottom", "Frequency (Hz)")
        self._wf_plot.setLabel("left",   "Time  (older at top)")
        self._wf_plot.setMinimumHeight(150)
        self._wf_plot.setXRange(0, MAX_FREQ)
        self._wf_plot.setYRange(0, WATERFALL_ROWS)
        self._wf_plot.getAxis("left").setTicks([])
        self._wf_img = pg.ImageItem()
        self._wf_plot.addItem(self._wf_img)
        self._wf_img.setRect(0, 0, MAX_FREQ, WATERFALL_ROWS)
        self._wf_img.setColorMap(pg.colormap.get("plasma"))
        self._wf_img.setLevels([-70, -5])
        self._wf_plot.getViewBox().sigXRangeChanged.connect(self._on_wf_range_changed)
        wf_vb.addWidget(self._wf_plot)
        vsplit.addWidget(wf_box)

        eq_box = QGroupBox("Parametric EQ  —  frequency response overlay")
        eq_vb  = QVBoxLayout(eq_box); eq_vb.setContentsMargins(6,14,6,6); eq_vb.setSpacing(4)
        pre_row = QHBoxLayout()
        pre_row.addWidget(QLabel("Preset:"))
        self._preset_name = QLineEdit()
        self._preset_name.setPlaceholderText("name…")
        self._preset_name.setFixedWidth(120)
        pre_row.addWidget(self._preset_name)
        save_btn = QPushButton("Save")
        save_btn.setFixedWidth(52); save_btn.clicked.connect(self._save_preset)
        pre_row.addWidget(save_btn)
        pre_row.addWidget(self._sep())
        self._preset_combo = QComboBox()
        self._preset_combo.setFixedWidth(140)
        self._refresh_presets()
        pre_row.addWidget(self._preset_combo)
        load_btn = QPushButton("Load")
        load_btn.setFixedWidth(52); load_btn.clicked.connect(self._load_preset)
        pre_row.addWidget(load_btn)
        del_btn = QPushButton("Delete")
        del_btn.setFixedWidth(56); del_btn.clicked.connect(self._delete_preset)
        pre_row.addWidget(del_btn)
        pre_row.addStretch()
        eq_vb.addLayout(pre_row)
        band_row = QHBoxLayout()
        add_btn = QPushButton("＋  Add Band")
        add_btn.setFixedWidth(110); add_btn.clicked.connect(self._add_band)
        band_row.addWidget(add_btn)
        detect_btn = QPushButton("⬡  Detect Peaks")
        detect_btn.setFixedWidth(130)
        detect_btn.setToolTip(
            "Analyse the current spectrum and automatically place\n"
            "EQ bands on the most prominent frequency peaks.")
        detect_btn.clicked.connect(self._detect_peaks)
        detect_btn.setStyleSheet(
            "QPushButton{color:#ffcc44;border-color:#665500;}"
            "QPushButton:hover{border-color:#ffcc44;}")
        band_row.addWidget(detect_btn)
        note = QLabel(
            "Dotted = individual bands  |  Dashed white = combined response"
        )
        note.setStyleSheet("color:#555; font-size:10px; font-style:italic;")
        note.setWordWrap(True)
        band_row.addWidget(note, 1)
        eq_vb.addLayout(band_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(120)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        self._bands_container = QWidget()
        self._bands_layout    = QVBoxLayout(self._bands_container)
        self._bands_layout.setContentsMargins(2,2,2,2); self._bands_layout.setSpacing(3)
        self._bands_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._bands_container)
        eq_vb.addWidget(scroll, 1)
        vsplit.addWidget(eq_box)
        vsplit.setSizes([280, 180, 360])
        self.setStyleSheet("""
            QMainWindow, QWidget        { background:#1a1a1a; color:#ddd; }
            QGroupBox                   { border:1px solid #383838; border-radius:4px;
                                          margin-top:8px; font:10px Consolas; color:#777; }
            QGroupBox::title            { subcontrol-origin:margin; left:8px; color:#aaa; }
            QPushButton                 { background:#252525; border:1px solid #4a4a4a;
                                          border-radius:3px; padding:3px 8px; color:#ccc; }
            QPushButton:hover           { background:#2e2e2e; border-color:#00ff88; }
            QDoubleSpinBox, QComboBox,
            QLineEdit                   { background:#202020; border:1px solid #444;
                                          color:#ddd; border-radius:2px; padding:1px 3px; }
            QDoubleSpinBox::up-button,
            QDoubleSpinBox::down-button { width:14px; }
            QCheckBox                   { color:#aaa; }
            QScrollArea                 { border:none; }
            QLabel                      { color:#ccc; }
            QSplitter::handle           { background:#2a2a2a; }
        """)

    @staticmethod
    def _sep():
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet("color:#444;"); return f

    def _start_pipeline(self):
        self._proc      = FFTProcessor()
        self._ir_thread = IRacingThread()
        self._ir_thread.sample_ready.connect(self._proc.add_sample)
        self._ir_thread.status_changed.connect(self._on_status)
        self._proc.spectrum_ready.connect(self._on_spectrum)
        self._ir_thread.start()

    def _on_status(self, msg: str):
        color = "#00ff88" if "Connected" in msg else "#ffaa00" if "DEMO" in msg else "#ff5555"
        self._status.setText(f"⬤  {msg}")
        self._status.setStyleSheet(f"color:{color};font-family:Consolas;font-size:9px;")

    def _on_spectrum(self, freqs: np.ndarray, mag_db: np.ndarray, rms: float):
        self._last_freqs  = freqs
        self._last_mag_db = mag_db
        self._spectrum_curve.setData(x=freqs, y=mag_db)
        self._wf_buf = np.roll(self._wf_buf, -1, axis=0)
        self._wf_buf[-1, :] = mag_db
        self._wf_img.setImage(self._wf_buf.T, autoLevels=False)
        self._refresh_eq_curves(freqs)
        self._meter.set_value(rms)
        self._rms_lbl.setText(f"{rms:.1f} Nm")

    def _toggle_log(self, checked: bool):
        self._log_mode = checked
        self._sp.setLogMode(x=checked, y=False)
        if checked:
            lo = max(np.log10(0.3), 0)
            self._sp.setXRange(lo, np.log10(MAX_FREQ), padding=0)
            self._sp.setLabel("bottom", "Frequency (Hz)  —  log scale")
        else:
            self._sp.setXRange(0, MAX_FREQ, padding=0)
            self._sp.setLabel("bottom", "Frequency (Hz)")
        self._wf_plot.setXRange(0, MAX_FREQ, padding=0)

    def _on_sp_range_changed(self, vb, _range=None):
        if self._range_syncing: return
        self._range_syncing = True
        xr = self._sp.viewRange()[0]
        lo, hi = (10**xr[0], 10**xr[1]) if self._log_mode else (xr[0], xr[1])
        self._wf_plot.setXRange(lo, hi, padding=0)
        self._range_syncing = False

    def _on_wf_range_changed(self, vb, _range=None):
        if self._range_syncing: return
        self._range_syncing = True
        lo, hi = self._wf_plot.viewRange()[0]
        lo = max(lo, 0.01)
        if self._log_mode:
            self._sp.setXRange(np.log10(lo), np.log10(hi), padding=0)
        else:
            self._sp.setXRange(lo, hi, padding=0)
        self._range_syncing = False

    def _add_band_with(self, band: EQBand):
        self._eq_bands.append(band)
        idx   = len(self._eq_bands) - 1
        color = self._BAND_COLORS[idx % len(self._BAND_COLORS)]
        row   = EQBandRow(band, color)
        row.changed.connect(self._on_eq_changed)
        row.remove_requested.connect(self._remove_band)
        self._eq_rows.append(row)
        self._bands_layout.addWidget(row)
        curve = self._sp.plot(
            pen=pg.mkPen(color=color, width=1.5, style=Qt.PenStyle.DotLine))
        self._eq_curves.append(curve)

    def _add_band(self):
        defaults = [2.0, 5.0, 9.0, 14.0, 20.0, 26.0, 3.5, 11.0]
        band = EQBand(freq=defaults[len(self._eq_bands) % len(defaults)])
        self._add_band_with(band); self._on_eq_changed()

    def _remove_band(self, row: EQBandRow):
        idx = self._eq_rows.index(row)
        self._eq_bands.pop(idx); self._eq_rows.pop(idx)
        curve = self._eq_curves.pop(idx)
        self._sp.removeItem(curve)
        self._bands_layout.removeWidget(row); row.deleteLater()
        self._on_eq_changed()

    def _on_eq_changed(self):
        self._refresh_eq_curves(np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE))

    def _refresh_eq_curves(self, freqs: np.ndarray):
        active = [b for b in self._eq_bands if b.enabled]
        h_sum  = np.ones(len(freqs), dtype=complex)
        for i, band in enumerate(self._eq_bands):
            curve = self._eq_curves[i]
            if not band.enabled or abs(band.gain_db) < 0.01:
                curve.setData([], []); continue
            try:
                sos = band.get_sos(SAMPLE_RATE)
                _, h = scipy_signal.sosfreqz(sos, worN=freqs, fs=SAMPLE_RATE)
                curve.setData(freqs, 20*np.log10(np.abs(h)+1e-12) - 40)
                if band.enabled: h_sum *= h
            except Exception:
                curve.setData([], [])
        if active:
            self._eq_sum_curve.setData(freqs, 20*np.log10(np.abs(h_sum)+1e-12) - 40)
        else:
            self._eq_sum_curve.setData([], [])

    def _detect_peaks(self):
        if self._last_freqs is None or self._last_mag_db is None: return
        freqs  = self._last_freqs; mag_db = self._last_mag_db
        mask = (freqs >= 0.3) & (freqs <= MAX_FREQ)
        f = freqs[mask]; m = mag_db[mask]
        peaks, props = scipy_signal.find_peaks(m, prominence=7.0, height=-58.0, distance=6)
        if len(peaks) == 0: return
        order    = np.argsort(props["prominences"])[::-1][:6]
        peak_idx = peaks[order]; prominences = props["prominences"][order]
        for row in list(self._eq_rows): self._remove_band(row)
        for pi, prom in zip(peak_idx, prominences):
            freq = float(f[pi])
            if freq < 0.3: continue
            q = float(np.clip(1.0 + prom / 6.0, 0.7, 5.0))
            self._add_band_with(EQBand(freq=freq, gain_db=3.0, q=q))
        self._on_eq_changed()

    def _refresh_presets(self):
        self._preset_combo.clear()
        for name in self._presets.list_presets():
            self._preset_combo.addItem(name)

    def _save_preset(self):
        name = self._preset_name.text().strip()
        if not name: return
        name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        if not name: return
        self._presets.save(name, self._eq_bands)
        self._refresh_presets()
        idx = self._preset_combo.findText(name)
        if idx >= 0: self._preset_combo.setCurrentIndex(idx)

    def _load_preset(self):
        name = self._preset_combo.currentText()
        if not name: return
        try:
            bands = self._presets.load(name)
        except Exception as e:
            self._on_status(f"Preset load error: {e}"); return
        for row in list(self._eq_rows): self._remove_band(row)
        for band in bands: self._add_band_with(band)
        self._preset_name.setText(name); self._on_eq_changed()

    def _delete_preset(self):
        name = self._preset_combo.currentText()
        if not name: return
        self._presets.delete(name); self._refresh_presets()

    def closeEvent(self, event):
        self._ir_thread.stop(); event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
def _dark_palette(app):
    app.setStyle("Fusion")
    p = QPalette(); C = QColor
    p.setColor(p.ColorRole.Window,          C(26,  26,  26))
    p.setColor(p.ColorRole.WindowText,      C(220, 220, 220))
    p.setColor(p.ColorRole.Base,            C(30,  30,  30))
    p.setColor(p.ColorRole.AlternateBase,   C(40,  40,  40))
    p.setColor(p.ColorRole.Text,            C(220, 220, 220))
    p.setColor(p.ColorRole.Button,          C(45,  45,  45))
    p.setColor(p.ColorRole.ButtonText,      C(220, 220, 220))
    p.setColor(p.ColorRole.Highlight,       C(0,   180, 100))
    p.setColor(p.ColorRole.HighlightedText, C(0,   0,   0))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    _dark_palette(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
