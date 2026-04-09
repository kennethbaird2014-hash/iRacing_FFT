"""
moza_output.py — Python interface to the MOZA wheelbase via moza_bridge.dll
═══════════════════════════════════════════════════════════════════════════════

Provides MozaFFBOutput, a thread-safe class that:
  1. Loads moza_bridge.dll (C wrapper around MOZA SDK)
  2. Creates a DirectInput constant-force effect on the wheelbase
  3. Exposes set_torque_nm(nm) to drive the wheel at up to 360 Hz

Prerequisites:
  • moza_bridge.dll  — built from moza_bridge.cpp  (see build_bridge.bat)
  • MOZA_SDK.dll     — from the Pit House SDK, same directory
  • MOZA Pit House   — must be running (handles USB enumeration)
  • iRacing FFB      — set to 0 in-game so it doesn't double-drive the wheel

Usage:
    output = MozaFFBOutput(max_torque_nm=12.0)
    output.start(hwnd=int(qt_window.winId()))
    # In the DSP loop:
    output.set_torque_nm(processed_nm)
    # On shutdown:
    output.stop()
"""

import ctypes
import os
import sys
from pathlib import Path
from typing import Optional


# DI_FFNOMINALMAX for DirectInput magnitude scale
_DI_MAX = 10000


class MozaFFBOutput:
    """Drives a MOZA wheelbase via a constant-force DirectInput effect."""

    def __init__(self, max_torque_nm: float = 12.0):
        """
        Parameters
        ----------
        max_torque_nm : float
            The torque (in Nm) that maps to DI_FFNOMINALMAX (10 000).
            For R12 V2 this is 12.0 Nm.  For R5 use 5.5, R9 use 9.0, etc.
        """
        self.max_torque_nm = max_torque_nm
        # Output scale 0.0–1.0 applied before magnitude conversion.
        # Default 0.25 (25 %) so the first test is safe.
        # Use the "ET Output" slider in the UI to raise it once confirmed working.
        self.output_scale: float = 0.25
        self._dll: Optional[ctypes.CDLL] = None
        self._active = False
        self._last_error: Optional[str] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def load_dll(self) -> bool:
        """Load moza_bridge.dll.  Returns True on success."""
        if self._dll is not None:
            return True

        dll_path = Path(__file__).parent / "moza_bridge.dll"
        sdk_path = Path(__file__).parent / "MOZA_SDK.dll"

        if not dll_path.exists():
            self._last_error = (
                f"moza_bridge.dll not found at {dll_path}\n"
                "Run build_bridge.bat from a VS 2022 x64 command prompt."
            )
            return False
        if not sdk_path.exists():
            self._last_error = (
                f"MOZA_SDK.dll not found at {sdk_path}\n"
                "Copy it from MOZA_SDK/1.0.1.8/MSVC2022-64/bin/"
            )
            return False

        try:
            # Ensure the SDK DLL directory is on PATH so loader finds it
            os.add_dll_directory(str(dll_path.parent))
            self._dll = ctypes.CDLL(str(dll_path))
            self._setup_signatures()
            return True
        except OSError as e:
            self._last_error = f"Failed to load moza_bridge.dll: {e}"
            self._dll = None
            return False

    # Band frequencies and limits (indices 0-5)
    EQ_FREQS = [10.0, 15.0, 25.0, 40.0, 60.0, 100.0]
    EQ_MAX   = [500,  500,  500,  500,  500,   100  ]
    EQ_UNITY = 100   # value that equals 0 dB (unity gain)

    def _setup_signatures(self):
        """Declare ctypes argtypes / restype for each exported function."""
        d = self._dll

        d.moza_sdk_init.argtypes = []
        d.moza_sdk_init.restype  = ctypes.c_int

        d.moza_sdk_cleanup.argtypes = []
        d.moza_sdk_cleanup.restype  = None

        d.moza_init.argtypes = [ctypes.c_void_p]
        d.moza_init.restype  = ctypes.c_int

        d.moza_set_magnitude.argtypes = [ctypes.c_int32]
        d.moza_set_magnitude.restype  = None

        d.moza_stop.argtypes = []
        d.moza_stop.restype  = None

        d.moza_cleanup.argtypes = []
        d.moza_cleanup.restype  = None

        d.moza_is_active.argtypes = []
        d.moza_is_active.restype  = ctypes.c_int

        d.moza_get_equalizer.argtypes = [ctypes.POINTER(ctypes.c_int)]
        d.moza_get_equalizer.restype  = ctypes.c_int

        d.moza_set_equalizer.argtypes = [ctypes.POINTER(ctypes.c_int)]
        d.moza_set_equalizer.restype  = ctypes.c_int

    def start(self, hwnd: int) -> bool:
        """
        Initialise the SDK and start the constant-force effect.

        Parameters
        ----------
        hwnd : int
            Win32 window handle.  From PyQt6: ``int(widget.winId())``.

        Returns True on success.  On failure, check ``last_error``.
        """
        if not self.load_dll():
            return False

        rc = self._dll.moza_init(ctypes.c_void_p(hwnd))
        if rc != 0:
            err_names = {
                1: "SDK not installed",
                2: "No devices found",
                3: "Out of range",
                4: "Parameter error",
                6: "Create effect error",
                8: "FFB error",
                9: "Firmware too old",
                10: "Pit House not ready",
            }
            self._last_error = (
                f"moza_init failed: {err_names.get(rc, f'code {rc}')}"
            )
            self._active = False
            return False

        self._active = True
        self._last_error = None
        return True

    def stop(self):
        """Zero force, stop the effect, and tear down the SDK."""
        if self._dll is not None:
            try:
                self._dll.moza_cleanup()
            except Exception:
                pass
        self._active = False

    # ── real-time torque output ───────────────────────────────────────────

    def set_torque_nm(self, nm: float):
        """
        Set the wheel torque in Newton-metres.

        Converts to DirectInput magnitude (-10000 … +10000) based on
        ``max_torque_nm`` (the wheelbase physical max, e.g. 12 for R12 V2)
        and ``output_scale`` (a 0.0–1.0 safety gain applied first).

        Safe to call at 360 Hz from a worker thread (no Python lock needed;
        the ctypes call releases the GIL).
        """
        if not self._active:
            return
        nm_scaled = nm * max(0.0, min(1.0, self.output_scale))
        mag = int(nm_scaled / self.max_torque_nm * _DI_MAX)
        mag = max(-_DI_MAX, min(_DI_MAX, mag))
        self._dll.moza_set_magnitude(ctypes.c_int32(mag))

    def set_torque_magnitude(self, mag: int):
        """
        Set the wheel torque in raw DirectInput units (-10000 … +10000).
        Faster than set_torque_nm() — skips the Nm conversion.
        """
        if not self._active:
            return
        self._dll.moza_set_magnitude(ctypes.c_int32(mag))

    # ── queries ───────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        if self._dll is not None:
            try:
                return bool(self._dll.moza_is_active())
            except Exception:
                pass
        return False

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def available(self) -> bool:
        """True if moza_bridge.dll and MOZA_SDK.dll both exist on disk."""
        return (
            (Path(__file__).parent / "moza_bridge.dll").exists()
            and (Path(__file__).parent / "MOZA_SDK.dll").exists()
        )

    # ── Pit House EQ control (no device ownership needed) ─────────────────────

    def sdk_init(self) -> bool:
        """Initialise SDK only (no ET effect). Required before EQ calls work.
        Safe to call even when iRacing is running — no device conflict."""
        if not self.load_dll():
            return False
        rc = self._dll.moza_sdk_init()
        if rc != 0:
            self._last_error = f"moza_sdk_init failed: code {rc}"
            return False
        self._last_error = None
        return True

    def get_equalizer(self) -> Optional[list]:
        """Read the current Pit House 6-band motor EQ.
        Returns list of 6 ints [7.5, 13, 22.5, 39, 55, 100 Hz] or None."""
        if not self.load_dll():
            return None
        buf = (ctypes.c_int * 6)()
        rc = self._dll.moza_get_equalizer(buf)
        if rc != 0:
            self._last_error = f"moza_get_equalizer failed: code {rc}"
            return None
        return list(buf)

    def set_equalizer(self, bands: list) -> bool:
        """Push 6 EQ band values to Pit House motor.
        bands: list of 6 ints, indices [7.5, 13, 22.5, 39, 55, 100 Hz].
        100 = unity (0 dB).  Clamps to valid range automatically.
        Returns True on success."""
        if not self.load_dll():
            return False
        buf = (ctypes.c_int * 6)(*[int(v) for v in bands[:6]])
        rc = self._dll.moza_set_equalizer(buf)
        if rc != 0:
            self._last_error = f"moza_set_equalizer failed: code {rc}"
            return False
        return True

    @staticmethod
    def eq_db_to_moza(db_gain: float, band_idx: int) -> int:
        """Convert dB gain to MOZA EQ amplitude unit.
        Assumes 100 = unity (0 dB). Positive db_gain → value > 100 (boost).
        Negative db_gain → value < 100 (cut)."""
        import math
        linear = 10.0 ** (db_gain / 20.0)
        val = int(round(linear * MozaFFBOutput.EQ_UNITY))
        return max(0, min(MozaFFBOutput.EQ_MAX[band_idx], val))
