#!/usr/bin/env python3
"""
FFB Frequency Analyzer  v1.3
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
import pygame
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
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QRectF, QTimer
from PyQt6.QtGui import QPainter, QLinearGradient, QPen, QColor, QFont, QPalette

try:
    import irsdk
    IRSDK_AVAILABLE = True
except ImportError:
    IRSDK_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE      = 360      # Hz  (irsdkLog360Hz=1 in app.ini recommended)
FFT_SIZE         = 1024     # samples per window  (~2.8 s at 360 Hz)
UPDATE_INTERVAL  = 12        # new FFT every N samples
MAX_FREQ         = 100.0    # Hz display ceiling
WATERFALL_ROWS   = 500
EMA_ALPHA        = 0.20
MAX_TORQUE_NM    = 20.0
NYQUIST_60HZ     = 30.0     # marker: SDK default is 60 Hz → 30 Hz Nyquist

PRESET_DIR = Path(__file__).parent / "presets"


# ── EQ Band ───────────────────────────────────────────────────────────────────
@dataclass
class EQBand:
    freq:        float = 5.0
    gain_db:     float = 0.0
    q:           float = 1.4
    filter_type: str   = "peak"
    enabled:     bool  = True
    group:       int   = 0     # 0 = none
    octaver:     bool  = False

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



# ── Interactive EQ Node class ────────────────────────────────────────────────
class DraggableEQScatter(pg.ScatterPlotItem):
    dragged = pyqtSignal(int, float, float)
    wheeled = pyqtSignal(int, float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dragging_idx = None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            pts = self.pointsAt(ev.pos())
            if len(pts) > 0:
                self._dragging_idx = pts[0].data()
                ev.accept()
                return
        ev.ignore()

    def mouseMoveEvent(self, ev):
        if self._dragging_idx is not None:
            pos = self.getViewBox().mapSceneToView(ev.scenePos())
            self.dragged.emit(self._dragging_idx, float(pos.x()), float(pos.y()))
            ev.accept()
        else:
            ev.ignore()

    def mouseReleaseEvent(self, ev):
        if self._dragging_idx is not None:
            self._dragging_idx = None
            ev.accept()
        else:
            ev.ignore()

    def wheelEvent(self, ev):
        pts = self.pointsAt(ev.pos())
        if len(pts) > 0:
            idx = pts[0].data()
            try:
                delta = float(ev.delta())
            except AttributeError:
                try:
                    delta = float(ev.angleDelta().y())
                except AttributeError:
                    delta = 120.0
            self.wheeled.emit(idx, delta)
            ev.accept()
        else:
            ev.ignore()


# ── DSP Engine ───────────────────────────────────────────────────────────────
class RealtimeDSP:
    def __init__(self):
        self.master_gain_db = 0.0
        self.comp_threshold_db = 0.0
        self.comp_ratio = 1.0
        self.comp_attack_ms = 10.0
        self.comp_release_ms = 50.0
        self.comp_makeup_db = 0.0
        self.octaver_blend = 0.0
        
        self._zi_eq = {}
        self._env = 0.0

    def process(self, chunk, bands):
        if len(chunk) == 0:
            return chunk
            
        out = chunk.copy()
        oct_sig = np.zeros_like(chunk)
        octaver_active = self.octaver_blend > 0.0 and any(b.octaver and b.enabled for b in bands)
        
        for band in bands:
            if not band.enabled:
                continue
            sos = band.get_sos(SAMPLE_RATE)
            bid = id(band)
            if bid not in self._zi_eq:
                self._zi_eq[bid] = scipy_signal.sosfilt_zi(sos) * out[0]
            
            # Apply EQ
            filtered, self._zi_eq[bid] = scipy_signal.sosfilt(sos, out, zi=self._zi_eq[bid])
            out = filtered
            
            # Octaver extraction
            if octaver_active and band.octaver:
                oct_sig += np.abs(filtered) - np.mean(np.abs(filtered))
        
        if octaver_active:
            # Mix the octaver back
            out = out + self.octaver_blend * oct_sig
            
        # Global FX
        if self.master_gain_db != 0.0:
            out *= (10.0 ** (self.master_gain_db / 20.0))
            
        # Compressor
        if self.comp_ratio > 1.0:
            attack_coef = math.exp(-1.0 / (SAMPLE_RATE * (self.comp_attack_ms / 1000.0)))
            release_coef = math.exp(-1.0 / (SAMPLE_RATE * (self.comp_release_ms / 1000.0)))
            thr_lin = 10.0 ** (self.comp_threshold_db / 20.0)
            
            for i in range(len(out)):
                abs_v = abs(out[i])
                if abs_v > self._env:
                    self._env = attack_coef * self._env + (1.0 - attack_coef) * abs_v
                else:
                    self._env = release_coef * self._env + (1.0 - release_coef) * abs_v
                    
                if self._env > thr_lin:
                    env_db = 20.0 * math.log10(self._env + 1e-9)
                    over_db = env_db - self.comp_threshold_db
                    gr_db = over_db * (1.0 - 1.0 / self.comp_ratio)
                    out[i] *= 10.0 ** (-gr_db / 20.0)
                    
            if self.comp_makeup_db != 0.0:
                out *= (10.0 ** (self.comp_makeup_db / 20.0))
                
        return out

# ── Preset manager ────────────────────────────────────────────────────────────
class PresetManager:
    def __init__(self):
        PRESET_DIR.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, bands: List[EQBand]):
        data = {
            "name": name,
            "created": datetime.now().isoformat(),
            "bands": [{"freq": b.freq, "gain_db": b.gain_db, "q": b.q,
                       "filter_type": b.filter_type, "enabled": b.enabled,
                       "group": getattr(b, "group", 0)}
                      for b in bands],
        }
        (PRESET_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))

    def list_presets(self) -> List[str]:
        return sorted(p.stem for p in PRESET_DIR.glob("*.json"))

    def load(self, name: str) -> List[EQBand]:
        data = json.loads((PRESET_DIR / f"{name}.json").read_text())
        return [EQBand(freq=b["freq"], gain_db=b["gain_db"], q=b["q"], 
                       filter_type=b["filter_type"], enabled=b["enabled"],
                       group=b.get("group", 0)) for b in data["bands"]]

    def delete(self, name: str):
        p = PRESET_DIR / f"{name}.json"
        if p.exists():
            p.unlink()


# ── Joystick Thread ─────────────────────────────────────────────────────────────
class JoystickThread(QThread):
    button_pressed = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self._running = True

    def run(self):
        pygame.init()
        pygame.joystick.init()
        joys = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        for j in joys:
            j.init()

        clock = pygame.time.Clock()
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.JOYBUTTONDOWN:
                    self.button_pressed.emit(event.joy, event.button)
            clock.tick(30)

    def stop(self):
        self._running = False
        pygame.quit()
        self.wait(1000)

# ── iRacing / demo thread ─────────────────────────────────────────────────────
class IRacingThread(QThread):
    sample_ready   = pyqtSignal(float)
    status_changed = pyqtSignal(str)
    car_detected   = pyqtSignal(str)

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
        
        last_tick_time = time.time()
        fade_chunk = [0.0] * 6
        last_conn_check = 0.0

        self._sent_car = False
        while self._running:
            now = time.time()
            if not connected:
                if now - last_conn_check > 0.5:
                    last_conn_check = now
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
                    except Exception:
                        pass
            else:
                try:
                    if not ir.is_connected:
                        connected = False; last_tick = -1; self._sent_car = False
                        self.status_changed.emit("iRacing disconnected")
                        ir.shutdown(); ir = irsdk.IRSDK()
                        continue

                    ir.freeze_var_buffer_latest()

                    if connected and not self._sent_car:
                        try:
                            si = ir['SessionInfo']
                            if not si and hasattr(ir, 'get_session_info_dict'):
                                si = ir.get_session_info_dict()
                            if isinstance(si, dict):
                                driver_info = si.get('DriverInfo')
                                if driver_info and 'Drivers' in driver_info:
                                    try:
                                        player_idx = ir['PlayerCarIdx']
                                    except Exception:
                                        player_idx = None
                                    if player_idx is not None:
                                        for d in driver_info['Drivers']:
                                            if d.get('CarIdx') == player_idx:
                                                car_name = d.get('CarScreenName') or d.get('CarPath') or d.get('CarScreenNameShort')
                                                if car_name:
                                                    self.car_detected.emit(str(car_name))
                                                    self._sent_car = True
                                                break
                        except Exception:
                            pass

                    tick = ir["SessionTick"]
                    if tick is not None and tick != last_tick:
                        last_tick = tick
                        last_tick_time = time.time()
                        
                        try:
                            torque_st = ir["SteeringWheelTorque_ST"]
                        except Exception:
                            torque_st = None

                        if torque_st is not None:
                            fade_chunk = [float(v) for v in torque_st]
                        else:
                            torque = ir["SteeringWheelTorque"]
                            if torque is not None:
                                if not getattr(self, '_st_warned', False):
                                    self.status_changed.emit("Connected   (Fallback 60 Hz mode)")
                                    self._st_warned = True
                                fade_chunk = [float(torque)] * 6
                            else:
                                fade_chunk = [0.0] * 6
                                
                        for v in fade_chunk:
                            self.sample_ready.emit(v)

                except Exception as exc:
                    connected = False; last_tick = -1
                    self.status_changed.emit(f"Error: {exc}")
                    
            now = time.time()
            if now - last_tick_time >= (1.5 / 60.0):
                last_tick_time = now - (0.5 / 60.0)
                for i in range(6):
                    fade_chunk[i] *= 0.92
                    self.sample_ready.emit(fade_chunk[i])
                    
            time.sleep(0.001)

    def _run_demo(self):
        self.status_changed.emit("DEMO  —  install pyirsdk for live data")
        dt  = 1.0 / SAMPLE_RATE
        rng = np.random.default_rng(42)
        t   = 0.0
        while self._running:
            for _ in range(6):
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
            time.sleep(dt * 6.0)

    def stop(self):
        self._running = False
        self.wait(2000)


# ── FFT processor ─────────────────────────────────────────────────────────────
class FFTProcessor(QObject):
    spectrum_ready = pyqtSignal(object, object, object, float)
    envelope_ready = pyqtSignal(float, float)

    def __init__(self):
        super().__init__()
        self._raw_buf   = deque(maxlen=FFT_SIZE)
        self._fx_buf    = deque(maxlen=FFT_SIZE)
        self._in_chunk  = []
        self._window    = np.hanning(FFT_SIZE)
        self._smooth_raw = None
        self._smooth_fx  = None
        self.freq_bins  = np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE)
        self.dsp        = RealtimeDSP()
        self.active_bands = []

    def set_bands(self, bands):
        self.active_bands = bands

    def add_sample(self, v: float):
        self._in_chunk.append(v)
        if len(self._in_chunk) >= UPDATE_INTERVAL:
            chunk = np.array(self._in_chunk)
            self._in_chunk.clear()
            
            self._raw_buf.extend(chunk)
            fx_chunk = self.dsp.process(chunk, self.active_bands)
            self._fx_buf.extend(fx_chunk)
            
            peak_in = float(np.max(np.abs(chunk)))
            peak_out = float(np.max(np.abs(fx_chunk)))
            self.envelope_ready.emit(peak_in, peak_out)
            
            if len(self._raw_buf) >= FFT_SIZE:
                self._compute()

    def _compute(self):
        # Raw
        data_raw = np.array(self._raw_buf) - np.mean(self._raw_buf)
        mag_raw  = np.abs(np.fft.rfft(data_raw * self._window)) / (FFT_SIZE / 2)
        raw_db   = 20.0 * np.log10(np.maximum(mag_raw, 1e-10))
        self._smooth_raw = (raw_db if self._smooth_raw is None else EMA_ALPHA * raw_db + (1 - EMA_ALPHA) * self._smooth_raw)
        
        # FX
        data_fx = np.array(self._fx_buf) - np.mean(self._fx_buf)
        mag_fx  = np.abs(np.fft.rfft(data_fx * self._window)) / (FFT_SIZE / 2)
        fx_db   = 20.0 * np.log10(np.maximum(mag_fx, 1e-10))
        self._smooth_fx = (fx_db if self._smooth_fx is None else EMA_ALPHA * fx_db + (1 - EMA_ALPHA) * self._smooth_fx)
        
        self.spectrum_ready.emit(self.freq_bins, self._smooth_raw.copy(), self._smooth_fx.copy(), float(np.sqrt(np.mean(data_raw**2))))


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

        specs = [("Group", 0,   20.0,  1.0, 0, "",    40, self.band.group),
                 ("Freq",  0.3, 180.0, 0.5, 1, " Hz", 78, self.band.freq),
                 ("Gain", -24,  24.0,  0.5, 1, " dB", 78, self.band.gain_db),
                 ("Q",    0.1,  50.0,  0.1, 2, "",    65, self.band.q)]
        self._group = self._freq = self._gain = self._q = None
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
        self._group, self._freq, self._gain, self._q = spinboxes

        self._oct = QCheckBox("Oct")
        self._oct.setChecked(getattr(self.band, "octaver", False))
        self._oct.toggled.connect(self._sync)
        row.addWidget(self._oct)

        row.addStretch()
        
        up_btn = QPushButton("▲"); up_btn.setFixedSize(18, 18)
        up_btn.clicked.connect(lambda: getattr(self.window(), "_move_band")(self, -1))
        row.addWidget(up_btn)
        
        dn_btn = QPushButton("▼"); dn_btn.setFixedSize(18, 18)
        dn_btn.clicked.connect(lambda: getattr(self.window(), "_move_band")(self, 1))
        row.addWidget(dn_btn)
        
        rem = QPushButton("✕"); rem.setFixedSize(22, 22)
        rem.setToolTip("Remove band")
        rem.clicked.connect(lambda: self.remove_requested.emit(self))
        row.addWidget(rem)

    @staticmethod
    def _lbl(t):
        lb = QLabel(t); lb.setStyleSheet("color:#888;font-size:10px;"); return lb

    def _sync(self):
        self.band.enabled     = self._en.isChecked()
        self.band.filter_type = self._TK[self._type.currentIndex()]
        self.band.group       = int(self._group.value())
        self.band.freq        = self._freq.value()
        self.band.gain_db     = self._gain.value()
        self.band.q           = self._q.value()
        self.band.octaver     = getattr(self, '_oct', None) is not None and self._oct.isChecked()
        self.changed.emit()


# ── Lap Review Window ─────────────────────────────────────────────────────────
class ReviewWindow(QWidget):
    def __init__(self, freqs: np.ndarray, frames: List[np.ndarray]):
        super().__init__()
        self.setWindowTitle("Lap Review")
        self.setMinimumSize(900, 700)
        self._freqs = freqs
        self._frames = np.array(frames)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        
        title = QLabel("RECORDED LAP REVIEW")
        title.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        title.setStyleSheet("color:#00ff88;")
        layout.addWidget(title)
        
        # Spectrum
        self._sp = pg.PlotWidget()
        self._sp.setLabel("left", "Level (dBFS)")
        self._sp.setLabel("bottom", "Frequency (Hz)")
        self._sp.setXRange(0, MAX_FREQ)
        self._sp.setYRange(-80, 5)
        self._sp.showGrid(x=True, y=True, alpha=0.25)
        self._sp.setMinimumHeight(250)
        self._sp.getAxis("left").setWidth(45)
        self._sp.getAxis("right").setWidth(10)
        self._sp.setMenuEnabled(False)
        
        self._sp.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(color="#333", style=Qt.PenStyle.DashLine)))
        self._sp.addItem(pg.InfiniteLine(pos=NYQUIST_60HZ, angle=90, pen=pg.mkPen(color="#ff4444", width=1, style=Qt.PenStyle.DashLine), label="Nyquist\n(60 Hz src)", labelOpts={"color": "#ff6666", "fill": (30,0,0,120), "position": 0.90}))
        
        self._sp_curve = self._sp.plot(pen=pg.mkPen(color=(0, 190, 100, 255), width=1.2), brush=pg.mkBrush(0, 190, 100, 70), fillLevel=-80.0)
        layout.addWidget(self._sp)
        
        # Waterfall
        self._wf_plot = pg.PlotWidget()
        self._wf_plot.setLabel("bottom", "Frequency (Hz)")
        self._wf_plot.setLabel("left", "Time")
        self._wf_plot.setXRange(0, MAX_FREQ)
        self._wf_plot.getAxis("left").setWidth(45)
        self._wf_plot.getAxis("right").setWidth(10)
        self._wf_plot.setMenuEnabled(False)
        self._wf_plot.getAxis("left").setTicks([])
        
        self._wf_img = pg.ImageItem()
        self._wf_img.setImage(np.ascontiguousarray(self._frames.T), autoLevels=False, autoDownsample=True)
        self._wf_img.setColorMap(pg.colormap.get("plasma"))
        nyquist = freqs[-1] if len(freqs) > 0 else MAX_FREQ
        self._wf_img.setRect(QRectF(0, 0, nyquist, len(self._frames)))
        self._wf_img.setLevels([-70, -5])
        self._wf_plot.addItem(self._wf_img)
        
        self._line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(color="#00ff88", width=2), movable=True)
        self._wf_plot.addItem(self._line)
        self._line.sigPositionChanged.connect(self._update_sp)
        
        # Link X axis
        self._wf_plot.setXLink(self._sp)
        
        layout.addWidget(self._wf_plot, 1)
        self._update_sp()
        
    def _update_sp(self):
        y = int(self._line.value())
        y = max(0, min(y, len(self._frames)-1))
        self._sp_curve.setData(x=self._freqs, y=self._frames[y])

# ── Mini Overlay Window ───────────────────────────────────────────────────────
class MiniOverlayWindow(QWidget):
    gain_changed = pyqtSignal(float)
    thresh_changed = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFB Mini Overlay")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(280, 420)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        
        # Controls
        ctrl_box = QHBoxLayout()
        ctrl_box.addWidget(QLabel("Gain:"))
        self._spin_gain = QDoubleSpinBox()
        self._spin_gain.setRange(-48.0, 24.0); self._spin_gain.setSingleStep(0.1); self._spin_gain.setSuffix(" dB")
        self._spin_gain.valueChanged.connect(self.gain_changed.emit)
        ctrl_box.addWidget(self._spin_gain)
        
        ctrl_box.addSpacing(6)
        
        ctrl_box.addWidget(QLabel("Thresh:"))
        self._spin_thr = QDoubleSpinBox()
        self._spin_thr.setRange(-80.0, 0.0); self._spin_thr.setSingleStep(1.0); self._spin_thr.setSuffix(" dB")
        self._spin_thr.valueChanged.connect(self.thresh_changed.emit)
        ctrl_box.addWidget(self._spin_thr)
        layout.addLayout(ctrl_box)
        
        # Peak Nm Labels
        pk_box = QHBoxLayout()
        pk_box.addWidget(QLabel("In Peak:"))
        self._lbl_in = QLabel("0.00 Nm")
        self._lbl_in.setStyleSheet("color:#00ff88; font-weight:bold; font-size:11px;")
        pk_box.addWidget(self._lbl_in)
        pk_box.addStretch()
        
        pk_box.addWidget(QLabel("Out:"))
        self._lbl_out = QLabel("0.00 Nm")
        self._lbl_out.setStyleSheet("color:#00c8ff; font-weight:bold; font-size:11px;")
        pk_box.addWidget(self._lbl_out)
        layout.addLayout(pk_box)
        
        # Mini Spectrum
        self._sp = pg.PlotWidget()
        self._sp.setLabel("left", "dB")
        self._sp.setXRange(0, MAX_FREQ)
        self._sp.setYRange(-80, 5)
        self._sp.showGrid(x=True, y=True, alpha=0.25)
        self._sp.setMinimumHeight(120)
        self._sp.getAxis("left").setWidth(30)
        self._sp.getAxis("bottom").setHeight(20)
        self._sp.setMenuEnabled(False)
        self._sp_curve = self._sp.plot(pen=pg.mkPen(color=(0, 190, 100, 255), width=1.2), fillLevel=-80.0, brush=pg.mkBrush(0, 190, 100, 70))
        self._fx_curve = self._sp.plot(pen=pg.mkPen(color=(0, 200, 255, 180), width=1.5))
        layout.addWidget(self._sp)
        
        # Mini Waterfall
        self._wf = pg.PlotWidget()
        self._wf.setXRange(0, MAX_FREQ)
        self._wf.setYRange(0, WATERFALL_ROWS)
        self._wf.getAxis("left").setTicks([])
        self._wf.getAxis("left").setWidth(30)
        self._wf.getAxis("bottom").setHeight(20)
        self._wf.setMenuEnabled(False)
        self._wf_img = pg.ImageItem()
        self._wf_img.setColorMap(pg.colormap.get("plasma"))
        self._wf_img.setLevels([-70, -5])
        self._wf.addItem(self._wf_img)
        layout.addWidget(self._wf, 1)

    def set_envelope(self, pin: float, pout: float):
        self._lbl_in.setText(f"{pin:.2f} Nm")
        self._lbl_out.setText(f"{pout:.2f} Nm")

    def update_plots(self, freqs, raw_db, fx_db, wf_buf):
        self._sp_curve.setData(x=freqs, y=raw_db)
        self._fx_curve.setData(x=freqs, y=fx_db)
        self._wf_img.setImage(wf_buf.T, autoLevels=False, autoDownsample=True)
        nyquist = freqs[-1] if len(freqs) > 0 else MAX_FREQ
        self._wf_img.setRect(QRectF(0, 0, nyquist, WATERFALL_ROWS))


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
        self.setWindowTitle("FFB Frequency Analyzer  v1.3")
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
        self._listen_mode      = False
        self._listen_peak_db   = None
        self._assigned_button  = None
        self._gain_up_button   = None
        self._gain_down_button = None
        self._assigning_mode   = None
        self._last_joy_tap     = 0.0
        
        self._record_mode      = False
        self._recorded_frames  = []
        self._review_win       = None
        self._current_eq_gain_db = None

        self._overlay_win      = MiniOverlayWindow()
        self._overlay_win.gain_changed.connect(self._on_overlay_gain)
        self._overlay_win.thresh_changed.connect(self._on_overlay_thresh)

        n_bins = FFT_SIZE // 2 + 1
        self._wf_buf = np.full((WATERFALL_ROWS, n_bins), -80.0, dtype=np.float32)

        self._build_ui()
        self._load_settings()
        self._start_pipeline()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root  = QWidget(); self.setCentralWidget(root)
        vmain = QVBoxLayout(root)
        vmain.setSpacing(4); vmain.setContentsMargins(8, 8, 8, 8)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("FFB FREQUENCY ANALYZER")
        title.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        title.setStyleSheet("color:#00ff88;")
        hdr.addWidget(title); hdr.addStretch()
        self._status = QLabel("Initialising...")
        self._status.setFont(QFont("Consolas", 9))
        self._status.setStyleSheet("color:#666;")
        hdr.addWidget(self._status)
        
        hdr.addSpacing(15)
        ov_btn = QPushButton("Overlay")
        ov_btn.setFixedWidth(65)
        ov_btn.clicked.connect(self._toggle_overlay)
        hdr.addWidget(ov_btn)
        
        vmain.addLayout(hdr)

        vsplit = QSplitter(Qt.Orientation.Vertical)
        vmain.addWidget(vsplit, 1)

        # ── Top Section: Graphs & RMS ─────────────────────────────────────────
        top_wg = QWidget()
        top_h  = QHBoxLayout(top_wg)
        top_h.setContentsMargins(0,0,0,0); top_h.setSpacing(6)

        left_split = QSplitter(Qt.Orientation.Vertical)
        top_h.addWidget(left_split, 1)

        # Spectrum
        sp_box = QWidget()
        sp_vb  = QVBoxLayout(sp_box); sp_vb.setContentsMargins(4,0,4,0); sp_vb.setSpacing(2)

        self._sp = pg.PlotWidget()
        self._sp.setLabel("left",   "Level (dBFS)")
        self._sp.setLabel("bottom", "Frequency (Hz)")
        self._sp.setXRange(0, MAX_FREQ)
        self._sp.setYRange(-80, 5)
        self._sp.showGrid(x=True, y=True, alpha=0.25)
        self._sp.setMinimumHeight(210)
        self._sp.getAxis("left").setWidth(45)
        self._sp.getAxis("right").setWidth(10)

        # 0 dB reference
        self._sp.addItem(pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(color="#333", style=Qt.PenStyle.DashLine)))

        # Nyquist limit marker (30 Hz @ 60 Hz source)
        self._nyq_line = pg.InfiniteLine(
            pos=NYQUIST_60HZ, angle=90,
            pen=pg.mkPen(color="#ff4444", width=1, style=Qt.PenStyle.DashLine),
            label="Nyquist\n(60 Hz src)",
            labelOpts={"color": "#ff6666", "fill": (30,0,0,120),
                       "position": 0.90, "movable": False},
        )
        self._sp.addItem(self._nyq_line)

        # Spectrum — filled curve
        freqs  = np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE)
        n_bins = len(freqs)
        self._spectrum_curve = pg.PlotCurveItem(
            x=freqs, y=np.full(n_bins, -80.0),
            pen=pg.mkPen(color=(0, 190, 100, 255), width=1.2),
            brush=pg.mkBrush(0, 190, 100, 70),
            fillLevel=-80.0,
        )
        self._sp.addItem(self._spectrum_curve)

        # Post-EQ curve
        self._post_eq_curve = self._sp.plot(
            pen=pg.mkPen(color=(0, 200, 255, 180), width=1.5))

        # EQ sum curve
        self._eq_sum_curve = self._sp.plot(
            pen=pg.mkPen(color="#ffffff", width=2, style=Qt.PenStyle.DashLine))

        sp_toolbar = QHBoxLayout()
        self._log_cb = QCheckBox("Log scale")
        self._log_cb.setStyleSheet("color:#aaa; font-size:10px;")
        self._log_cb.toggled.connect(self._toggle_log)
        sp_toolbar.addStretch(); sp_toolbar.addWidget(self._log_cb)

        sp_vb.addLayout(sp_toolbar)
        sp_vb.addWidget(self._sp, 1)
        left_split.addWidget(sp_box)
        self._sp.getViewBox().sigXRangeChanged.connect(self._on_sp_range_changed)

        # Waterfall
        wf_box = QGroupBox("Waterfall  (newest at bottom  —  X-axis locked to spectrum)")
        wf_vb  = QVBoxLayout(wf_box); wf_vb.setContentsMargins(4,14,4,4)

        self._wf_plot = pg.PlotWidget()
        self._wf_plot.setLabel("bottom", "Frequency (Hz)")
        self._wf_plot.setLabel("left",   "Time  (older at top)")
        self._wf_plot.setMinimumHeight(150)
        self._wf_plot.setXRange(0, MAX_FREQ)
        self._wf_plot.setYRange(0, WATERFALL_ROWS)
        self._wf_plot.getAxis("left").setTicks([])
        self._wf_plot.getAxis("left").setWidth(45)
        self._wf_plot.getAxis("right").setWidth(10)

        self._wf_img = pg.ImageItem()
        self._wf_img.setImage(self._wf_buf.T, autoLevels=False)
        self._wf_plot.addItem(self._wf_img)
        nyquist = SAMPLE_RATE / 2.0
        self._wf_img.setColorMap(pg.colormap.get("plasma"))
        self._wf_img.setRect(QRectF(0, 0, nyquist, WATERFALL_ROWS))
        self._wf_img.setLevels([-70, -5])

        self._wf_plot.getViewBox().sigXRangeChanged.connect(self._on_wf_range_changed)
        wf_vb.addWidget(self._wf_plot)
        left_split.addWidget(wf_box)

        # RMS meter (spans vertically alongside spectrum + waterfall)
        sm_box = QGroupBox("RMS")
        sm_vb  = QVBoxLayout(sm_box); sm_vb.setContentsMargins(4,14,4,6)
        self._meter   = StrengthMeter()
        self._rms_lbl = QLabel("0.0 Nm")
        self._rms_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rms_lbl.setFont(QFont("Consolas", 10))
        self._rms_lbl.setStyleSheet("color:#00ff88;")
        sm_vb.addWidget(self._meter, 1); sm_vb.addWidget(self._rms_lbl)
        sm_box.setFixedWidth(78)
        top_h.addWidget(sm_box)

        vsplit.addWidget(top_wg)

        # ── Row 3: EQ and FX ─────────────────────────────────────────────────
        bot_split = QSplitter(Qt.Orientation.Horizontal)

        eq_box = QGroupBox("Parametric EQ  —  frequency response overlay")
        eq_vb  = QVBoxLayout(eq_box); eq_vb.setContentsMargins(6,14,6,6); eq_vb.setSpacing(4)

        # Preset toolbar
        pre_row = QHBoxLayout()
        pre_row.addWidget(QLabel("Preset:"))
        self._preset_name = QLineEdit()
        self._preset_name.setPlaceholderText("name…")
        self._preset_name.setFixedWidth(120)
        pre_row.addWidget(self._preset_name)

        new_btn = QPushButton("New")
        new_btn.setFixedWidth(44); new_btn.clicked.connect(self._new_preset)
        pre_row.addWidget(new_btn)

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

        # Band toolbar
        band_row = QHBoxLayout()
        add_btn = QPushButton("＋  Add Band")
        add_btn.setFixedWidth(110); add_btn.clicked.connect(self._add_band)
        band_row.addWidget(add_btn)

        sort_btn = QPushButton("⇕  Sort")
        sort_btn.setFixedWidth(70); sort_btn.clicked.connect(self._sort_bands)
        band_row.addWidget(sort_btn)

        self._detect_btn = QPushButton("⬡  Detect Peaks")
        self._detect_btn.setFixedWidth(130)
        self._detect_btn.setToolTip(
            "Click to start listening. Or single-tap your bound wheel button."
        )

        self._record_btn = QPushButton("⏺ Record Lap")
        self._record_btn.setFixedWidth(110)
        self._record_btn.clicked.connect(self._toggle_record)
        self._record_btn.setToolTip("Double-tap bound wheel button to toggle recording.")
        band_row.addWidget(self._record_btn)
        self._detect_btn.clicked.connect(self._toggle_listen)
        self._detect_btn.setStyleSheet(
            "QPushButton{color:#ffcc44;border-color:#665500;}"
            "QPushButton:hover{border-color:#ffcc44;}")
        band_row.addWidget(self._detect_btn)
        
        self._assign_btn = QPushButton("Bind Wheel Button")
        self._assign_btn.setFixedWidth(130)
        self._assign_btn.clicked.connect(self._start_assign)
        band_row.addWidget(self._assign_btn)

        note = QLabel(
            "Dotted = individual bands  |  Dashed white = combined response  |  "
            "Direct MOZA hardware injection planned for v2."
        )
        note.setStyleSheet("color:#555; font-size:10px; font-style:italic;")
        note.setWordWrap(True)
        band_row.addWidget(note, 1)
        eq_vb.addLayout(band_row)

        # Band list
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

        bot_split.addWidget(eq_box)

        from PyQt6.QtWidgets import QFormLayout
        fx_box = QGroupBox("Global FX")
        fx_vb = QVBoxLayout(fx_box)
        fx_vb.setContentsMargins(6,14,6,6)
        
        form = QFormLayout()
        
        gn_box = QHBoxLayout()
        self._master_gain = QDoubleSpinBox()
        self._master_gain.setRange(-48.0, 24.0); self._master_gain.setSingleStep(0.1); self._master_gain.setSuffix(" dB")
        self._master_gain.valueChanged.connect(self._sync_dsp)
        gn_box.addWidget(self._master_gain)
        
        self._gain_up_btn = QPushButton("Bind Up")
        self._gain_up_btn.clicked.connect(self._start_assign_gain_up)
        gn_box.addWidget(self._gain_up_btn)
        
        self._gain_down_btn = QPushButton("Bind Dn")
        self._gain_down_btn.clicked.connect(self._start_assign_gain_down)
        gn_box.addWidget(self._gain_down_btn)
        
        form.addRow("Master Gain:", gn_box)
        
        self._wf_transform = QComboBox()
        self._wf_transform.addItems(["Normal", "dy/dx (Time Diff)"])
        self._wf_transform.setToolTip("Computes delta of spectrum over time for waterfall (highlights changes)")
        self._wf_transform.currentIndexChanged.connect(self._sync_dsp)
        form.addRow("Waterfall:", self._wf_transform)
        
        fline = QFrame(); fline.setFrameShape(QFrame.Shape.HLine); fline.setStyleSheet("color:#444;")
        form.addRow(fline)
        
        self._comp_thr = QDoubleSpinBox()
        self._comp_thr.setRange(-80.0, 0.0); self._comp_thr.setSingleStep(1.0); self._comp_thr.setSuffix(" dB"); self._comp_thr.setValue(0.0)
        self._comp_thr.valueChanged.connect(self._sync_dsp)
        form.addRow("Comp Thresh:", self._comp_thr)
        
        self._comp_ratio = QDoubleSpinBox()
        self._comp_ratio.setRange(1.0, 20.0); self._comp_ratio.setSingleStep(0.5); self._comp_ratio.setValue(1.0)
        self._comp_ratio.valueChanged.connect(self._sync_dsp)
        form.addRow("Comp Ratio:", self._comp_ratio)
        
        self._comp_att = QDoubleSpinBox()
        self._comp_att.setRange(1.0, 500.0); self._comp_att.setSingleStep(10); self._comp_att.setValue(10); self._comp_att.setSuffix(" ms")
        self._comp_att.valueChanged.connect(self._sync_dsp)
        form.addRow("Attack:", self._comp_att)
        
        self._comp_rel = QDoubleSpinBox()
        self._comp_rel.setRange(10.0, 2000.0); self._comp_rel.setSingleStep(20); self._comp_rel.setValue(50); self._comp_rel.setSuffix(" ms")
        self._comp_rel.valueChanged.connect(self._sync_dsp)
        form.addRow("Release:", self._comp_rel)
        
        self._comp_make = QDoubleSpinBox()
        self._comp_make.setRange(-24.0, 24.0); self._comp_make.setSingleStep(0.5); self._comp_make.setSuffix(" dB")
        self._comp_make.valueChanged.connect(self._sync_dsp)
        form.addRow("Make-up Gain:", self._comp_make)
        
        fline2 = QFrame(); fline2.setFrameShape(QFrame.Shape.HLine); fline2.setStyleSheet("color:#444;")
        form.addRow(fline2)
        
        self._octaver_blend = QDoubleSpinBox()
        self._octaver_blend.setRange(0.0, 1.0); self._octaver_blend.setSingleStep(0.05); self._octaver_blend.setValue(0.0)
        self._octaver_blend.valueChanged.connect(self._sync_dsp)
        form.addRow("Octaver Blend:", self._octaver_blend)

        fx_vb.addLayout(form)
        fx_vb.addStretch()

        bot_split.addWidget(fx_box)
        bot_split.setSizes([750, 250])
        vsplit.addWidget(bot_split)
        vsplit.setSizes([280, 180, 360])

        # Stylesheet
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

    # ── Pipeline ──────────────────────────────────────────────────────────────
    def _start_pipeline(self):
        self._proc      = FFTProcessor()
        self._ir_thread = IRacingThread()
        self._joy_thread = JoystickThread()
        
        self._ir_thread.sample_ready.connect(self._proc.add_sample)
        self._ir_thread.status_changed.connect(self._on_status)
        self._ir_thread.car_detected.connect(self._on_car_detected)
        self._proc.spectrum_ready.connect(self._on_spectrum)
        self._proc.envelope_ready.connect(self._overlay_win.set_envelope)
        self._joy_thread.button_pressed.connect(self._on_joy_button)
        
        self._ir_thread.start()
        self._joy_thread.start()

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_car_detected(self, car_name: str):
        self._status.setText(self._status.text() + f"  [Car: {car_name}]")
        self._preset_name.setText(car_name)
        idx = self._preset_combo.findText(car_name)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
            self._load_preset()

    def _on_status(self, msg: str):
        color = "#00ff88" if "Connected" in msg else "#ffaa00" if "DEMO" in msg else "#ff5555"
        self._status.setText(f"⬤  {msg}")
        self._status.setStyleSheet(f"color:{color};font-family:Consolas;font-size:9px;")

    def _on_spectrum(self, freqs: np.ndarray, mag_db: np.ndarray, fx_db: np.ndarray, rms: float):
        self._last_freqs  = freqs
        self._last_mag_db = mag_db

        # Spectrum curve
        self._spectrum_curve.setData(x=freqs, y=mag_db)

        if self._listen_mode:
            if self._listen_peak_db is None:
                self._listen_peak_db = mag_db.copy()
            else:
                self._listen_peak_db = np.maximum(self._listen_peak_db, mag_db)

        # Waterfall with dy/dx logic off sidechain
        wf_line = mag_db
        if getattr(self, '_wf_transform', None) is not None and self._wf_transform.currentIndex() == 1:
            wf_line = mag_db - (self._last_mag_db if self._last_mag_db is not None else mag_db)
            
        self._wf_buf = np.roll(self._wf_buf, 1, axis=0)
        self._wf_buf[0, :] = wf_line
        self._wf_img.setImage(self._wf_buf.T, autoLevels=False, autoDownsample=True)

        if self._record_mode:
            self._recorded_frames.append(mag_db.copy())

        # Updated Post-EQ rendering uses real DSP
        self._post_eq_curve.setData(x=freqs, y=fx_db)

        # EQ overlay visual updating
        self._refresh_eq_curves(freqs)

        # Meter
        self._meter.set_value(rms)
        self._rms_lbl.setText(f"{rms:.1f} Nm")

        if self._overlay_win.isVisible():
            self._overlay_win.update_plots(freqs, mag_db, fx_db, self._wf_buf)

    # ── Log scale ─────────────────────────────────────────────────────────────
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
        # Keep waterfall in linear space (image doesn't log-transform)
        self._wf_plot.setXRange(0, MAX_FREQ, padding=0)

    # ── X-axis view lock ──────────────────────────────────────────────────────
    def _on_sp_range_changed(self, vb, _range=None):
        if self._range_syncing:
            return
        self._range_syncing = True
        xr = self._sp.viewRange()[0]
        if self._log_mode:
            # Spectrum range is in log10 — convert to linear for waterfall
            lo, hi = 10**xr[0], 10**xr[1]
        else:
            lo, hi = xr[0], xr[1]
        self._wf_plot.setXRange(lo, hi, padding=0)
        self._range_syncing = False

    def _on_wf_range_changed(self, vb, _range=None):
        if self._range_syncing:
            return
        self._range_syncing = True
        lo, hi = self._wf_plot.viewRange()[0]
        lo = max(lo, 0.01)
        if self._log_mode:
            self._sp.setXRange(np.log10(lo), np.log10(hi), padding=0)
        else:
            self._sp.setXRange(lo, hi, padding=0)
        self._range_syncing = False

    # ── EQ band management ────────────────────────────────────────────────────
    def _add_band_with(self, band: EQBand):
        """Add a pre-configured EQBand to the UI."""
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
        self._add_band_with(band)
        self._on_eq_changed()

    def _remove_band(self, row: EQBandRow):
        idx = self._eq_rows.index(row)
        self._eq_bands.pop(idx)
        self._eq_rows.pop(idx)
        curve = self._eq_curves.pop(idx)
        self._sp.removeItem(curve)
        self._bands_layout.removeWidget(row)
        row.deleteLater()
        self._on_eq_changed()

    def _move_band(self, row: EQBandRow, dir: int):
        idx = self._eq_rows.index(row)
        n_idx = idx + dir
        if 0 <= n_idx < len(self._eq_rows):
            # Swap elements
            self._eq_bands[idx], self._eq_bands[n_idx] = self._eq_bands[n_idx], self._eq_bands[idx]
            self._eq_rows[idx], self._eq_rows[n_idx]   = self._eq_rows[n_idx], self._eq_rows[idx]
            self._eq_curves[idx], self._eq_curves[n_idx] = self._eq_curves[n_idx], self._eq_curves[idx]
            
            for r in self._eq_rows:
                self._bands_layout.removeWidget(r)
            for r in self._eq_rows:
                self._bands_layout.addWidget(r)

    def _sort_bands(self):
        def get_key(tup):
            b = tup[0]
            # Unassigned group (0) goes to bottom, positive groups clustered
            g = b.group if b.group > 0 else 9999
            return (g, b.freq)
            
        combined = list(zip(self._eq_bands, self._eq_rows, self._eq_curves))
        combined.sort(key=get_key)
        
        self._eq_bands  = [c[0] for c in combined]
        self._eq_rows   = [c[1] for c in combined]
        self._eq_curves = [c[2] for c in combined]
        
        for r in self._eq_rows:
            self._bands_layout.removeWidget(r)
        for r in self._eq_rows:
            self._bands_layout.addWidget(r)

    def _on_eq_changed(self):
        self._refresh_eq_curves(np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE))

    def _sync_dsp(self):
        if hasattr(self, '_proc') and hasattr(self._proc, 'dsp'):
            self._proc.dsp.master_gain_db = self._master_gain.value()
            self._proc.dsp.comp_threshold_db = self._comp_thr.value()
            self._proc.dsp.comp_ratio = self._comp_ratio.value()
            self._proc.dsp.comp_attack_ms = self._comp_att.value()
            self._proc.dsp.comp_release_ms = self._comp_rel.value()
            self._proc.dsp.comp_makeup_db = self._comp_make.value()
            self._proc.dsp.octaver_blend = self._octaver_blend.value()
            self._proc.set_bands([b for b in self._eq_bands])
            
            # Sync Overlay Window
            self._overlay_win._spin_gain.blockSignals(True)
            self._overlay_win._spin_gain.setValue(self._master_gain.value())
            self._overlay_win._spin_gain.blockSignals(False)
            
            self._overlay_win._spin_thr.blockSignals(True)
            self._overlay_win._spin_thr.setValue(self._comp_thr.value())
            self._overlay_win._spin_thr.blockSignals(False)

    def _on_overlay_gain(self, val: float):
        self._master_gain.setValue(val)
        
    def _on_overlay_thresh(self, val: float):
        self._comp_thr.setValue(val)

    def _toggle_overlay(self):
        if self._overlay_win.isVisible():
            self._overlay_win.hide()
        else:
            self._overlay_win.show()

    def _on_eq_dragged(self, idx: int, x: float, y: float):
        if idx < 0 or idx >= len(self._eq_bands): return
        band = self._eq_bands[idx]
        band.freq = max(0.1, min(100.0, x))
        band.gain_db = max(-24.0, min(24.0, y))
        row = self._eq_rows[idx]
        row._freq.setValue(band.freq)
        row._gain.setValue(band.gain_db)

    def _on_eq_wheeled(self, idx: int, delta: float):
        if idx < 0 or idx >= len(self._eq_bands): return
        band = self._eq_bands[idx]
        d_q = 0.1 if delta > 0 else -0.1
        band.q = max(0.1, min(50.0, band.q + d_q))
        self._eq_rows[idx]._q.setValue(band.q)

    def _refresh_eq_curves(self, freqs: np.ndarray):
        active = [b for b in self._eq_bands if b.enabled]
        h_sum  = np.ones(len(freqs), dtype=complex)
        
        pts = []
        for i, band in enumerate(self._eq_bands):
            curve = self._eq_curves[i]
            color = self._BAND_COLORS[i % len(self._BAND_COLORS)]
            
            if band.enabled:
                pts.append({
                    'pos': (band.freq, band.gain_db),
                    'data': i,
                    'brush': color,
                    'pen': 'w'
                })
            
            if not band.enabled or abs(band.gain_db) < 0.01:
                curve.setData([], [])
                continue
            try:
                sos = band.get_sos(360.0)
                _, h = scipy_signal.sosfreqz(sos, worN=freqs, fs=360.0)
                curve.setData(freqs, 20*np.log10(np.abs(h)+1e-12))
                if band.enabled:
                    h_sum *= h
            except Exception:
                curve.setData([], [])
                
        self._current_eq_gain_db = 20*np.log10(np.abs(h_sum)+1e-12)
        if active:
            self._eq_sum_curve.setData(freqs, self._current_eq_gain_db)
        else:
            self._eq_sum_curve.setData([], [])
            
        if hasattr(self, '_eq_scatter'):
            self._eq_scatter.setData(pts)
        
        # Trigger DSP sync
        self._sync_dsp()

    # ── Detect Peaks (Listen mode) ────────────────────────────────────────────
    def _start_assign(self):
        self._assigning_mode = "listen"
        self._assign_btn.setText("Press wheel button...")
        self._assign_btn.setStyleSheet("color:#00ff88; border-color:#00ff88;")

    def _start_assign_gain_up(self):
        self._assigning_mode = "gain_up"
        self._gain_up_btn.setText("Press...")
        self._gain_up_btn.setStyleSheet("color:#00ff88; border-color:#00ff88;")
        
    def _start_assign_gain_down(self):
        self._assigning_mode = "gain_down"
        self._gain_down_btn.setText("Press...")
        self._gain_down_btn.setStyleSheet("color:#00ff88; border-color:#00ff88;")

    def _on_joy_button(self, joy_id: int, button_id: int):
        if self._assigning_mode:
            if self._assigning_mode == "listen":
                self._assigned_button = (joy_id, button_id)
                self._assign_btn.setText(f"Bound (J{joy_id} B{button_id})")
                self._assign_btn.setStyleSheet("")
            elif self._assigning_mode == "gain_up":
                self._gain_up_button = (joy_id, button_id)
                self._gain_up_btn.setText(f"Up: J{joy_id} B{button_id}")
                self._gain_up_btn.setStyleSheet("")
            elif self._assigning_mode == "gain_down":
                self._gain_down_button = (joy_id, button_id)
                self._gain_down_btn.setText(f"Dn: J{joy_id} B{button_id}")
                self._gain_down_btn.setStyleSheet("")
            self._assigning_mode = None
            self._save_settings()
            return
            
        if self._assigned_button == (joy_id, button_id):
            now = time.time()
            if now - self._last_joy_tap < 0.4:
                if self._listen_mode:
                    self._toggle_listen()
                self._toggle_record()
                self._last_joy_tap = 0.0
            else:
                self._last_joy_tap = now
                self._toggle_listen()
                
        if getattr(self, '_gain_up_button', None) == (joy_id, button_id):
            self._master_gain.setValue(self._master_gain.value() + 0.1)
            
        if getattr(self, '_gain_down_button', None) == (joy_id, button_id):
            self._master_gain.setValue(self._master_gain.value() - 0.1)

    def _toggle_listen(self):
        self._listen_mode = not self._listen_mode
        if self._listen_mode:
            self._listen_peak_db = None
            self._detect_btn.setText("⬡  Listening...")
            self._detect_btn.setStyleSheet("QPushButton{color:#ff4444;border-color:#ff4444;}")
        else:
            self._detect_btn.setText("⬡  Detect Peaks")
            self._detect_btn.setStyleSheet("QPushButton{color:#ffcc44;border-color:#665500;}")
            self._run_detect_peaks()

    def _run_detect_peaks(self):
        if self._last_freqs is None:
            return

        freqs  = self._last_freqs
        mag_db = self._listen_peak_db if self._listen_peak_db is not None else self._last_mag_db
        if mag_db is None:
            return

        # Restrict to display range and positive frequencies
        mask = (freqs >= 2.0) & (freqs <= 100.0)
        f    = freqs[mask]
        m    = mag_db[mask]

        if len(m) == 0:
            return
        
        peaks, props = scipy_signal.find_peaks(
            m,
            prominence=5.0,  # lowered slightly to catch harmonics
            height=-60.0,
            distance=6,
        )

        if len(peaks) == 0:
            return

        order       = np.argsort(props["prominences"])[::-1]
        peak_idx    = peaks[order]
        prominences = props["prominences"][order]
        peak_freqs  = f[peak_idx]

        groups = []
        assigned = set()
        group_id_counter = 1
        
        for i, idx in enumerate(peak_idx):
            if i in assigned: continue
            f_base = peak_freqs[i]
            best_fundamental = f_base
            
            # 1. Check for subharmonics to find actual fundamental
            for d in range(2, 6):
                f_sub = f_base / d
                if f_sub < 2.0: continue
                diffs = np.abs(peak_freqs - f_sub)
                if len(diffs) > 0:
                    min_diff = np.min(diffs)
                    best_match = np.argmin(diffs)
                    if min_diff < f_sub * 0.08: # 8% tolerance
                        best_fundamental = peak_freqs[best_match]

            # 2. Extract out all integer harmonics of this fundamental
            harmonic_group = []
            for h in range(1, 15):
                f_harm = best_fundamental * h
                if f_harm > 100.0: break
                diffs = np.abs(peak_freqs - f_harm)
                if len(diffs) > 0:
                    min_diff = np.min(diffs)
                    best_match = np.argmin(diffs)
                    if min_diff < f_harm * 0.08:
                        if best_match not in assigned:
                            harmonic_group.append({
                                "freq": float(peak_freqs[best_match]),
                                "prominence": float(prominences[best_match])
                            })
                            assigned.add(best_match)
                            
            if len(harmonic_group) > 1:
                groups.append({"group_id": group_id_counter, "peaks": harmonic_group})
                group_id_counter += 1
            else:
                groups.append({"group_id": 0, "peaks": [{"freq": float(f_base), "prominence": float(prominences[i])}]})
                assigned.add(i)

        for row in list(self._eq_rows):
            self._remove_band(row)

        band_count = 0
        for g in groups:
            gid = g["group_id"]
            for pk in g["peaks"]:
                if band_count >= 12: break
                freq = pk["freq"]
                prom = pk["prominence"]
                if freq < 0.3: continue
                q = float(np.clip(1.0 + prom / 6.0, 0.7, 5.0))
                self._add_band_with(EQBand(freq=freq, gain_db=3.0, q=q, group=gid))
                band_count += 1

        self._sort_bands()
        self._on_eq_changed()

    # ── Record Lap ────────────────────────────────────────────────────────────
    def _toggle_record(self):
        self._record_mode = not self._record_mode
        if self._record_mode:
            self._recorded_frames = []
            self._record_btn.setText("⏹ Stop Recording")
            self._record_btn.setStyleSheet("background:#550000; border-color:#ff4444;")
            self._status.setText("⬤  Recording lap... double-tap or click Stop to review.")
        else:
            self._record_btn.setText("⏺ Record Lap")
            self._record_btn.setStyleSheet("")
            self._status.setText("⬤  Recording stopped. Opening review window.")
            if len(self._recorded_frames) > 0 and self._last_freqs is not None:
                self._review_win = ReviewWindow(self._last_freqs, self._recorded_frames)
                self._review_win.show()

    # ── Presets ───────────────────────────────────────────────────────────────
    def _new_preset(self):
        for row in list(self._eq_rows):
            self._remove_band(row)
        self._preset_name.setText("")
        self._preset_combo.setCurrentIndex(-1)
        self._on_eq_changed()

    def _refresh_presets(self):
        self._preset_combo.clear()
        for name in self._presets.list_presets():
            self._preset_combo.addItem(name)

    def _save_preset(self):
        name = self._preset_name.text().strip()
        if not name:
            return
        # Sanitise filename
        name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        if not name:
            return
        self._presets.save(name, self._eq_bands)
        self._refresh_presets()
        # Select the just-saved preset
        idx = self._preset_combo.findText(name)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)

    def _load_preset(self):
        name = self._preset_combo.currentText()
        if not name:
            return
        try:
            bands = self._presets.load(name)
        except Exception as e:
            self._on_status(f"Preset load error: {e}")
            return
        # Clear current bands
        for row in list(self._eq_rows):
            self._remove_band(row)
        # Load preset bands
        for band in bands:
            self._add_band_with(band)
        self._preset_name.setText(name)
        self._on_eq_changed()

    def _delete_preset(self):
        name = self._preset_combo.currentText()
        if not name:
            return
        self._presets.delete(name)
        self._refresh_presets()

    # ── Settings ──────────────────────────────────────────────────────────────
    def _load_settings(self):
        settings_path = Path(__file__).parent / "settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text())
                if data.get("listen_button"):
                    self._assigned_button = tuple(data["listen_button"])
                    if hasattr(self, '_assign_btn'):
                        self._assign_btn.setText(f"Bound (J{self._assigned_button[0]} B{self._assigned_button[1]})")
                if data.get("gain_up_button"):
                    self._gain_up_button = tuple(data["gain_up_button"])
                    if hasattr(self, '_gain_up_btn'):
                        self._gain_up_btn.setText(f"Up: J{self._gain_up_button[0]} B{self._gain_up_button[1]}")
                if data.get("gain_down_button"):
                    self._gain_down_button = tuple(data["gain_down_button"])
                    if hasattr(self, '_gain_down_btn'):
                        self._gain_down_btn.setText(f"Dn: J{self._gain_down_button[0]} B{self._gain_down_button[1]}")
            except Exception:
                pass

    def _save_settings(self):
        settings_path = Path(__file__).parent / "settings.json"
        data = {
            "listen_button": self._assigned_button,
            "gain_up_button": self._gain_up_button,
            "gain_down_button": self._gain_down_button
        }
        settings_path.write_text(json.dumps(data, indent=2))

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._ir_thread.stop()
        if hasattr(self, '_joy_thread'):
            self._joy_thread.stop()
        if self._overlay_win:
            self._overlay_win.close()
        event.accept()


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
    # Set pyqtgraph defaults before any PlotWidget is constructed to
    # eliminate the white flash caused by widgets painting before the
    # Qt palette is applied.
    pg.setConfigOptions(antialias=True, background="#0d0d0d", foreground="#aaaaaa")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
