"""
dinput_output.py — Python interface to dinput_bridge.dll
═══════════════════════════════════════════════════════════════════════════════

Provides DirectInputFFBOutput, a thread-safe class that:
  1. Loads dinput_bridge.dll (pure Win32 DirectInput, no MOZA SDK)
  2. Opens the first FFB-capable joystick with EXCLUSIVE | BACKGROUND
     cooperative level — this works regardless of iRacing's FFB slider
  3. Creates a constant-force effect and exposes set_torque_nm(nm)

Why this instead of moza_output.py / MOZA SDK?
  The MOZA proprietary SDK routes forces through Pit House, which obeys
  the iRacing FFB gain slider.  Direct DirectInput with exclusive access
  bypasses that routing entirely — the same technique MAIRA uses.

Prerequisites:
  • dinput_bridge.dll  — built from dinput_bridge.cpp  (see build_dinput_bridge.bat)
    No other DLL dependency (dinput8.dll is a system DLL).
  • MOZA Pit House     — must be running (it loads the USB driver for the wheel)
  • iRacing FFB        — set to 0% in-game so it doesn't fight our effect

Usage:
    output = DirectInputFFBOutput(max_torque_nm=12.0, target_name="MOZA")
    output.start(hwnd=int(qt_window.winId()))
    output.set_torque_nm(processed_nm)   # call at 360 Hz
    output.stop()
"""

import ctypes
import os
from pathlib import Path
from typing import Optional


_DI_MAX = 10000   # DI_FFNOMINALMAX


class DirectInputFFBOutput:
    """Drives an FFB wheel via a constant-force DirectInput effect (exclusive access)."""

    def __init__(self, max_torque_nm: float = 12.0, target_name: str = ""):
        """
        Parameters
        ----------
        max_torque_nm : float
            Torque value (Nm) that maps to DI_FFNOMINALMAX (10 000).
            MOZA R12 V2 = 12.0, R9 = 9.0, R5 = 5.5, etc.
        target_name : str
            Substring of the device product name to prefer (e.g. "MOZA").
            Empty string = use first FFB device found.
        """
        self.max_torque_nm = max_torque_nm
        self.target_name   = target_name
        self._dll: Optional[ctypes.CDLL] = None
        self._active       = False
        self._last_error:  Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def load_dll(self) -> bool:
        """Load dinput_bridge.dll.  Returns True on success."""
        if self._dll is not None:
            return True

        dll_path = Path(__file__).parent / "dinput_bridge.dll"
        if not dll_path.exists():
            self._last_error = (
                f"dinput_bridge.dll not found at {dll_path}\n"
                "Run build_dinput_bridge.bat from a VS 2022 x64 command prompt."
            )
            return False

        try:
            os.add_dll_directory(str(dll_path.parent))
            self._dll = ctypes.CDLL(str(dll_path))
            self._setup_signatures()
            return True
        except OSError as e:
            self._last_error = f"Failed to load dinput_bridge.dll: {e}"
            self._dll = None
            return False

    def _setup_signatures(self):
        d = self._dll

        d.dinput_set_target_name.argtypes = [ctypes.c_wchar_p]
        d.dinput_set_target_name.restype  = None

        d.dinput_init.argtypes = [ctypes.c_void_p]
        d.dinput_init.restype  = ctypes.c_int

        d.dinput_set_magnitude.argtypes = [ctypes.c_int32]
        d.dinput_set_magnitude.restype  = None

        d.dinput_stop.argtypes  = []
        d.dinput_stop.restype   = None

        d.dinput_cleanup.argtypes = []
        d.dinput_cleanup.restype  = None

        d.dinput_is_active.argtypes = []
        d.dinput_is_active.restype  = ctypes.c_int

        d.dinput_enumerate_devices.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.dinput_enumerate_devices.restype  = ctypes.c_int

    def start(self, hwnd: int) -> bool:
        """
        Enumerate FFB devices, acquire exclusive access, and start the effect.

        Parameters
        ----------
        hwnd : int
            Win32 window handle (PyQt6: int(widget.winId())).

        Returns True on success.  Check last_error on failure.
        """
        if not self.load_dll():
            return False

        # Set device name filter
        self._dll.dinput_set_target_name(self.target_name)

        rc = self._dll.dinput_init(ctypes.c_void_p(hwnd))
        if rc != 0:
            errors = {
                -1: "DirectInput8Create failed",
                -2: "No FFB device found (is Pit House running?)",
                -3: "SetCooperativeLevel/Acquire failed — another app may have exclusive access",
                -4: "Device does not support ConstantForce effect",
                -5: "CreateEffect failed",
            }
            self._last_error = errors.get(rc, f"dinput_init error code {rc}")
            self._active = False
            return False

        self._active = True
        self._last_error = None
        return True

    def stop(self):
        """Zero force, stop effect, release device."""
        if self._dll is not None:
            try:
                self._dll.dinput_cleanup()
            except Exception:
                pass
        self._active = False

    # ── Real-time torque output ───────────────────────────────────────────────

    def set_torque_nm(self, nm: float):
        """
        Set wheel torque in Newton-metres.
        Safe to call at 360 Hz (ctypes releases the GIL).
        """
        if not self._active:
            return
        mag = int(nm / self.max_torque_nm * _DI_MAX)
        mag = max(-_DI_MAX, min(_DI_MAX, mag))
        self._dll.dinput_set_magnitude(ctypes.c_int32(mag))

    def set_torque_magnitude(self, mag: int):
        """Set raw DirectInput magnitude (-10000 … +10000). Faster — skips Nm conversion."""
        if not self._active:
            return
        self._dll.dinput_set_magnitude(ctypes.c_int32(mag))

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        if self._dll is not None:
            try:
                return bool(self._dll.dinput_is_active())
            except Exception:
                pass
        return False

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def available(self) -> bool:
        """True if dinput_bridge.dll exists on disk."""
        return (Path(__file__).parent / "dinput_bridge.dll").exists()

    def list_devices(self) -> list[str]:
        """Return names of all attached DirectInput FFB devices."""
        if not self.load_dll():
            return []
        max_n = 16
        buf = (ctypes.c_wchar * (max_n * 256))()
        count = self._dll.dinput_enumerate_devices(buf, max_n)
        names = []
        for i in range(min(count, max_n)):
            name = buf[i * 256: i * 256 + 256]
            names.append("".join(name).rstrip("\x00"))
        return names
