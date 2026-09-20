from datetime import datetime
import math
import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QFrame, QSizePolicy, QProgressBar, QComboBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRectF, QPointF
from PyQt6.QtGui import QPainter, QPixmap, QColor, QPen, QTransform, QLinearGradient

class VirtualWheel(QWidget):
    """A high-fidelity animated steering wheel that responds to torque and angle."""
    
    def __init__(self, asset_path):
        super().__init__()
        self.setMinimumSize(400, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # Load the generated wheel image
        self._pixmap = QPixmap(asset_path)
        
        self._torque = 0.0      # Current torque in Nm
        self._angle = 0.0       # Current angle in degrees
        self._max_torque = 12.0 # Current wheelbase limit
        self._clipping = False
        
        # Vibration/Jitter state
        self._jitter_x = 0.0
        self._jitter_y = 0.0

    def update_state(self, torque_nm, angle_deg, max_torque):
        self._torque = torque_nm
        self._angle = angle_deg
        self._max_torque = max_torque
        self._clipping = abs(torque_nm) >= max_torque
        
        # Calculate jitter based on high-frequency torque components
        # (Simplified: just use absolute torque for the "stunning" effect)
        intensity = min(1.0, abs(torque_nm) / max_torque)
        vibe = intensity * 4.0 # max 4 pixel jitter
        self._jitter_x = (np.random.random() - 0.5) * vibe
        self._jitter_y = (np.random.random() - 0.5) * vibe
        
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        
        # 1. Background Glow
        intensity = min(1.0, abs(self._torque) / self._max_torque)
        glow_color = QColor(0, 255, 136, int(100 * intensity)) if not self._clipping else QColor(255, 68, 68, int(150 * intensity))
        
        grad = QLinearGradient(cx, cy - 200, cx, cy + 200)
        grad.setColorAt(0.5, glow_color)
        grad.setColorAt(0.0, QColor(0,0,0,0))
        grad.setColorAt(1.0, QColor(0,0,0,0))
        p.fillRect(0, 0, w, h, grad)

        # 2. Draw the Wheel
        if not self._pixmap.isNull():
            # Scale pixmap to fit
            side = min(w, h) * 0.8
            scaled_pix = self._pixmap.scaled(int(side), int(side), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
            # Apply Rotation and Jitter
            t = QTransform()
            t.translate(cx + self._jitter_x, cy + self._jitter_y)
            t.rotate(self._angle)
            t.translate(-scaled_pix.width() / 2, -scaled_pix.height() / 2)
            
            p.setTransform(t)
            p.drawPixmap(0, 0, scaled_pix)
            p.resetTransform()

        # 3. Stress Meter Ring
        p.setPen(QPen(QColor(40, 40, 40), 10))
        rect = QRectF(cx - 190, cy - 190, 380, 380)
        p.drawArc(rect, 45 * 16, 270 * 16)
        
        span = int(intensity * 270 * 16)
        color = QColor(0, 255, 136) if not self._clipping else QColor(255, 68, 68)
        p.setPen(QPen(color, 12))
        p.drawArc(rect, (135 + (270 - intensity * 270)) * 16, span)

        # 4. Digital Readout
        p.setPen(QColor(220, 220, 220))
        p.setFont(p.font()) # Use default QFont
        p.drawText(int(cx - 50), int(cy + 150), 100, 40, Qt.AlignmentFlag.AlignCenter, f"{abs(self._torque):.1f} Nm")
        
        if self._clipping:
            p.setPen(QColor(255, 68, 68))
            p.drawText(int(cx - 50), int(cy - 160), 100, 40, Qt.AlignmentFlag.AlignCenter, "CLIPPING")
            
        p.end()

class MockMozaSDK(QFrame):
    """A simulated Pit House interface for testing EQ and SDK interactions."""
    
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background: #111; border: 1px solid #333; border-radius: 8px;")
        self.setMinimumHeight(200)
        
        layout = QVBoxLayout(self)
        title = QLabel("MOZA PIT HOUSE SIMULATOR (HEADLESS MODE)")
        title.setStyleSheet("color: #00ff88; font-family: Consolas; font-weight: bold;")
        layout.addWidget(title)
        
        self.eq_bars = []
        bar_layout = QHBoxLayout()
        freqs = [10, 15, 25, 40, 60, 100]
        for f in freqs:
            v_box = QVBoxLayout()
            bar = QProgressBar()
            bar.setOrientation(Qt.Orientation.Vertical)
            bar.setRange(0, 500)
            bar.setValue(100)
            bar.setTextVisible(False)
            bar.setFixedWidth(20)
            bar.setStyleSheet("""
                QProgressBar { background: #222; border: 1px solid #444; }
                QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #00c8ff, stop:1 #00ff88); }
            """)
            v_box.addWidget(bar, 1, Qt.AlignmentFlag.AlignCenter)
            lbl = QLabel(f"{f}Hz")
            lbl.setStyleSheet("font-size: 9px; color: #888;")
            v_box.addWidget(lbl, 0, Qt.AlignmentFlag.AlignCenter)
            bar_layout.addLayout(v_box)
            self.eq_bars.append(bar)
        
        layout.addLayout(bar_layout)
        
        self.status = QLabel("Ready for connection...")
        self.status.setStyleSheet("color: #888; font-family: Consolas; font-size: 10px;")
        layout.addWidget(self.status)

    def set_eq_values(self, values):
        """Update the visual EQ bars."""
        for i, v in enumerate(values):
            if i < len(self.eq_bars):
                self.eq_bars[i].setValue(v)
        self.status.setText(f"Last Push: {datetime.now().strftime('%H:%M:%S')}")

class VirtualLabTab(QWidget):
    """The main container for the Virtual Lab simulation."""
    
    def __init__(self, asset_path):
        super().__init__()
        layout = QHBoxLayout(self)
        
        # Left side: The Wheel
        self.wheel = VirtualWheel(asset_path)
        layout.addWidget(self.wheel, 2)
        
        # Right side: Settings & SDK Simulation
        right_panel = QVBoxLayout()
        
        # Wheelbase selector
        hw_box = QFrame()
        hw_box.setStyleSheet("background: #1e1e1e; border-radius: 6px;")
        hw_lay = QVBoxLayout(hw_box)
        hw_lay.addWidget(QLabel("Simulated Wheelbase:"))
        self.hw_select = QComboBox()
        self.hw_select.addItems(["MOZA R5 (5.5 Nm)", "MOZA R9 (9.0 Nm)", "MOZA R12 (12.0 Nm)", "MOZA R16 (16.0 Nm)", "MOZA R21 (21.0 Nm)"])
        self.hw_select.setCurrentIndex(2) # Default R12
        hw_lay.addWidget(self.hw_select)
        right_panel.addWidget(hw_box)
        
        # Mock SDK
        self.sdk_sim = MockMozaSDK()
        right_panel.addWidget(self.sdk_sim)
        
        right_panel.addStretch()
        layout.addLayout(right_panel, 1)

    def get_max_torque(self):
        text = self.hw_select.currentText()
        if "5.5" in text: return 5.5
        if "9.0" in text: return 9.0
        if "12.0" in text: return 12.0
        if "16.0" in text: return 16.0
        if "21.0" in text: return 21.0
        return 12.0

