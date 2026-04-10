"""
null_output.py — safe no-op FFB output for testing without a physical wheelbase.

NullFFBOutput implements the same interface as MozaFFBOutput / DirectInputFFBOutput
but discards every torque command and records statistics instead.  Use it to
exercise the full DSP pipeline (EQ, compressor, slew limiter, output shaping)
safely on any machine — no wheel, no DLLs, no risk of unexpected torque spikes.

Typical workflow
----------------
1. Run the app normally — it auto-selects NullFFBOutput when no hardware bridge
   is found (on Linux or Windows without dinput_bridge.dll / moza_bridge.dll).
2. Enable "Direct Output" in the Output tab.
3. iRacing demo mode provides synthetic FFB data; the full DSP chain runs.
4. The Output status label shows live call rate and peak torque so you can
   verify the signal is actually reaching the output stage.
"""

import time
import threading
from typing import Optional


class NullFFBOutput:
    """No-op FFB driver — records torque calls without sending anything."""

    # Class attribute mirrors MozaFFBOutput so isinstance-guarded code stays safe.
    IS_NULL  = True
    EQ_FREQS = [10.0, 15.0, 25.0, 40.0, 60.0, 100.0]

    def __init__(self, max_torque_nm: float = 12.0, target_name: str = "Null"):
        self.max_torque_nm = max_torque_nm
        self.output_scale  = 0.25   # matches live driver default

        self._active     = False
        self._last_nm    = 0.0
        self._peak_nm    = 0.0
        self._call_count = 0
        self._t0         = time.monotonic()
        self._lock       = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, hwnd: int = 0) -> bool:
        with self._lock:
            self._active     = True
            self._peak_nm    = 0.0
            self._call_count = 0
            self._t0         = time.monotonic()
        return True

    def stop(self):
        with self._lock:
            self._active  = False
            self._last_nm = 0.0

    # ── real-time torque output (called at up to 360 Hz) ──────────────────

    def set_torque_nm(self, nm: float):
        """Record the torque value; do NOT send to any hardware."""
        if not self._active:
            return
        nm_out = nm * max(0.0, min(1.0, self.output_scale))
        with self._lock:
            self._last_nm = nm_out
            if abs(nm_out) > self._peak_nm:
                self._peak_nm = abs(nm_out)
            self._call_count += 1

    # ── properties ────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def available(self) -> bool:
        return True     # always available — no DLL required

    @property
    def last_error(self) -> Optional[str]:
        return None

    @property
    def last_nm(self) -> float:
        with self._lock:
            return self._last_nm

    @property
    def peak_nm(self) -> float:
        with self._lock:
            return self._peak_nm

    @property
    def call_rate_hz(self) -> float:
        """Approximate set_torque_nm() call rate in Hz since start() or reset."""
        with self._lock:
            elapsed = time.monotonic() - self._t0
            return self._call_count / elapsed if elapsed > 0.1 else 0.0

    def reset_stats(self):
        with self._lock:
            self._peak_nm    = 0.0
            self._call_count = 0
            self._t0         = time.monotonic()

    # ── EQ stubs — not supported, but present for API completeness ─────────

    def sdk_init(self) -> bool:
        return False

    def get_equalizer(self):
        return None

    def set_equalizer(self, bands) -> bool:
        return False
