import sys

def main():
    target_file = 'ffb_analyzer.py'
    with open(target_file, 'r', encoding='utf-8') as f:
        code = f.read()

    # Create the new methods string
    new_methods = """    def _sync_dsp(self):
        if hasattr(self, '_proc') and hasattr(self._proc, 'dsp'):
            self._proc.dsp.master_gain_db = self._master_gain.value()
            self._proc.dsp.comp_threshold_db = self._comp_thr.value()
            self._proc.dsp.comp_ratio = self._comp_ratio.value()
            self._proc.dsp.comp_attack_ms = self._comp_att.value()
            self._proc.dsp.comp_release_ms = self._comp_rel.value()
            self._proc.dsp.comp_makeup_db = self._comp_make.value()
            self._proc.dsp.octaver_blend = self._octaver_blend.value()
            self._proc.set_bands([b for b in self._eq_bands])

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
        band.q = max(0.1, min(10.0, band.q + d_q))
        self._eq_rows[idx]._q.setValue(band.q)

    def _refresh_eq_curves(self, freqs: np.ndarray):"""

    # We replace the original `def _refresh_eq_curves`
    old_method = "    def _refresh_eq_curves(self, freqs: np.ndarray):"
    
    code = code.replace(old_method, new_methods, 1)

    # Next we fix the body of `_refresh_eq_curves` to correctly draw the scatter points
    # It starts originally at `        active = [b for b in self._eq_bands if b.enabled]`
    # And ends at `            self._eq_sum_curve.setData([], [])`

    old_body = """        active = [b for b in self._eq_bands if b.enabled]
        h_sum  = np.ones(len(freqs), dtype=complex)
        for i, band in enumerate(self._eq_bands):
            curve = self._eq_curves[i]
            if not band.enabled or abs(band.gain_db) < 0.01:
                curve.setData([], [])
                continue
            try:
                sos = band.get_sos(SAMPLE_RATE)
                _, h = scipy_signal.sosfreqz(sos, worN=freqs, fs=SAMPLE_RATE)
                curve.setData(freqs, 20*np.log10(np.abs(h)+1e-12) - 40)
                if band.enabled:
                    h_sum *= h
            except Exception:
                curve.setData([], [])
        self._current_eq_gain_db = 20*np.log10(np.abs(h_sum)+1e-12)
        if active:
            self._eq_sum_curve.setData(freqs, self._current_eq_gain_db - 40)
        else:
            self._eq_sum_curve.setData([], [])"""

    new_body = """        active = [b for b in self._eq_bands if b.enabled]
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
        self._sync_dsp()"""

    code = code.replace(old_body, new_body, 1)

    with open(target_file, 'w', encoding='utf-8') as f:
        f.write(code)

if __name__ == '__main__':
    main()
