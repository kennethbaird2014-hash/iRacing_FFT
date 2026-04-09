import sys

def main():
    with open('ffb_analyzer.py', 'r', encoding='utf-8') as f:
        code = f.read()

    # 1. Modify EQBand 
    old_eqband = """class EQBand:
    freq:        float = 5.0
    gain_db:     float = 0.0
    q:           float = 1.4
    filter_type: str   = "peak"
    enabled:     bool  = True
    group:       int   = 0     # 0 = none"""
        
    new_eqband = """class EQBand:
    freq:        float = 5.0
    gain_db:     float = 0.0
    q:           float = 1.4
    filter_type: str   = "peak"
    enabled:     bool  = True
    group:       int   = 0     # 0 = none
    octaver:     bool  = False"""
    code = code.replace(old_eqband, new_eqband, 1)

    # 2. Add DraggableEQScatter and RealtimeDSP before class PresetManager
    dsp_code = """
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
            sos = band.get_sos(360.0) # SAMPLE_RATE
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
            attack_coef = math.exp(-1.0 / (360.0 * (self.comp_attack_ms / 1000.0)))
            release_coef = math.exp(-1.0 / (360.0 * (self.comp_release_ms / 1000.0)))
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
"""
    code = code.replace("# ── Preset manager ────────────────────────────────────────────────────────────\n", dsp_code, 1)

    # 3. FFTProcessor modifications
    old_fft = """class FFTProcessor(QObject):
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
            self.freq_bins, self._smooth.copy(), float(np.sqrt(np.mean(data**2))))"""

    new_fft = """class FFTProcessor(QObject):
    spectrum_ready = pyqtSignal(object, object, object, float)

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
        
        self.spectrum_ready.emit(self.freq_bins, self._smooth_raw.copy(), self._smooth_fx.copy(), float(np.sqrt(np.mean(data_raw**2))))"""

    code = code.replace(old_fft, new_fft, 1)

    with open('ffb_analyzer.py', 'w', encoding='utf-8') as f:
        f.write(code)

if __name__ == '__main__':
    main()
