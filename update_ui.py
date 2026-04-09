import sys, re

def main():
    with open('ffb_analyzer.py', 'r', encoding='utf-8') as f:
        code = f.read()

    # Update EQBandRow sync
    old_sync = """    def _sync(self):
        self.band.enabled     = self._en.isChecked()
        self.band.filter_type = self._TK[self._type.currentIndex()]
        self.band.group       = int(self._group.value())
        self.band.freq        = self._freq.value()
        self.band.gain_db     = self._gain.value()
        self.band.q           = self._q.value()
        self.changed.emit()"""
    new_sync = """    def _sync(self):
        self.band.enabled     = self._en.isChecked()
        self.band.filter_type = self._TK[self._type.currentIndex()]
        self.band.group       = int(self._group.value())
        self.band.freq        = self._freq.value()
        self.band.gain_db     = self._gain.value()
        self.band.q           = self._q.value()
        self.band.octaver     = getattr(self, '_oct', None) is not None and self._oct.isChecked()
        self.changed.emit()"""
    code = code.replace(old_sync, new_sync, 1)

    # Add Oct checkbox to EQBandRow
    old_spinboxes = """        self._group, self._freq, self._gain, self._q = spinboxes

        row.addStretch()"""
    new_spinboxes = """        self._group, self._freq, self._gain, self._q = spinboxes

        self._oct = QCheckBox("Oct")
        self._oct.setChecked(getattr(self.band, "octaver", False))
        self._oct.toggled.connect(self._sync)
        row.addWidget(self._oct)

        row.addStretch()"""
    code = code.replace(old_spinboxes, new_spinboxes, 1)

    # _on_spectrum signature
    old_on_spec = "def _on_spectrum(self, freqs: np.ndarray, mag_db: np.ndarray, rms: float):"
    new_on_spec = "def _on_spectrum(self, freqs: np.ndarray, mag_db: np.ndarray, fx_db: np.ndarray, rms: float):"
    code = code.replace(old_on_spec, new_on_spec, 1)

    # Update _on_spectrum body for fx_db and dy/dx
    old_on_spec_body = """        # Spectrum curve
        self._spectrum_curve.setData(x=freqs, y=mag_db)

        if self._listen_mode:
            if self._listen_peak_db is None:
                self._listen_peak_db = mag_db.copy()
            else:
                self._listen_peak_db = np.maximum(self._listen_peak_db, mag_db)

        # Waterfall
        self._wf_buf = np.roll(self._wf_buf, 1, axis=0)
        self._wf_buf[0, :] = mag_db
        self._wf_img.setImage(self._wf_buf.T, autoLevels=False)

        if self._record_mode:
            self._recorded_frames.append(mag_db.copy())

        # EQ overlay
        self._refresh_eq_curves(freqs)
        
        if self._current_eq_gain_db is not None:
            self._post_eq_curve.setData(x=freqs, y=mag_db + self._current_eq_gain_db)

        # Meter
        self._meter.set_value(rms)
        self._rms_lbl.setText(f"{rms:.1f} Nm")"""
    new_on_spec_body = """        # Spectrum curve
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
        self._wf_img.setImage(self._wf_buf.T, autoLevels=False)

        if self._record_mode:
            self._recorded_frames.append(mag_db.copy())

        # Updated Post-EQ rendering uses real DSP
        self._post_eq_curve.setData(x=freqs, y=fx_db)

        # EQ overlay visual updating
        self._refresh_eq_curves(freqs)

        # Meter
        self._meter.set_value(rms)
        self._rms_lbl.setText(f"{rms:.1f} Nm")"""
    code = code.replace(old_on_spec_body, new_on_spec_body, 1)

    with open('ffb_analyzer.py', 'w', encoding='utf-8') as f:
        f.write(code)

if __name__ == '__main__':
    main()
