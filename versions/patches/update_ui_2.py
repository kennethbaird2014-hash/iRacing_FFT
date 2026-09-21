import sys

def main():
    target_file = 'ffb_analyzer.py'
    with open(target_file, 'r', encoding='utf-8') as f:
        code = f.read()

    # Disable context menu for waterfall
    old_wf = "self._wf_plot.getAxis(\"right\").setWidth(10)"
    new_wf = "self._wf_plot.getAxis(\"right\").setWidth(10)\n        self._wf_plot.setMenuEnabled(False)"
    code = code.replace(old_wf, new_wf, 1)

    # Disable EQ context menu
    old_sp = "self._sp.getAxis(\"right\").setWidth(10)"
    new_sp = "self._sp.getAxis(\"right\").setWidth(10)\n        self._sp.setMenuEnabled(False)"
    code = code.replace(old_sp, new_sp, 1)

    # Update _build_ui Section 3 (starting at line 832 originally)
    import re
    # We will replace the whole block from "# ── Row 3: EQ" to "vsplit.addWidget(eq_box)\n        vsplit.setSizes([280, 180, 360])" 
    
    start_token = "# ── Row 3: EQ ─────────────────────────────────────────────────────────"
    end_token = "vsplit.setSizes([280, 180, 360])   # more space for EQ panel"
    
    start_idx = code.find(start_token)
    end_idx = code.find(end_token) + len(end_token)

    new_section = """# ── Row 3: EQ and FX ─────────────────────────────────────────────────
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
        
        self._master_gain = QDoubleSpinBox()
        self._master_gain.setRange(-48.0, 24.0); self._master_gain.setSingleStep(0.1); self._master_gain.setSuffix(" dB")
        self._master_gain.valueChanged.connect(self._sync_dsp)
        form.addRow("Master Gain:", self._master_gain)
        
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
        vsplit.setSizes([280, 180, 360])"""

    code = code[:start_idx] + new_section + code[end_idx:]

    with open(target_file, 'w', encoding='utf-8') as f:
        f.write(code)

if __name__ == '__main__':
    main()
