# iRacing FFB Frequency Analyzer

A real-time force-feedback (FFB) frequency analyzer and DSP tool for
iRacing, built as a companion add-on to
[MarvinsAIRARefactored](https://github.com/kennethbaird2014-hash/MarvinsAIRARefactored)
(MAIRA). It reads live steering wheel torque telemetry from iRacing,
displays it as an FFT spectrum and waterfall, and applies a configurable
EQ / compressor / limiter chain to the FFB signal before sending it back
out to a wheelbase.

## Status

Current version: **v2.0** (`ffb_analyzer.py`). See
[`CHANGELOG.md`](CHANGELOG.md) for the full version history — older
snapshots (v1.1 through v1.3) are kept under [`versions/`](versions/) for
reference and are not maintained.

## Getting started

1. Run `install.bat` once (installs Python dependencies from
   `requirements.txt`).
2. Run `launch.bat` to start the analyzer. iRacing does not need to be
   running first — the app waits and auto-connects when you join a
   session.
3. `launch_v1.1.bat` runs the archived v1.1 snapshot as a fallback (no
   MOZA output, no wheel button bindings).

## Layout

| Path | Purpose |
|---|---|
| `ffb_analyzer.py` | Main application (UI, FFT/waterfall, DSP chain, EQ) |
| `modal_analyzer.py`, `modal_panel.py` | Modal analysis (SSA) tab, independent of the main DSP chain |
| `virtual_lab.py`, `virtual_wheel.png` | "MOZA Virtual Lab" tab — animated on-screen wheel for testing without a physical wheelbase |
| `null_output.py` | FFB output stub that discards torque and logs stats, for hardware-free testing |
| `moza_output.py`, `moza_bridge.cpp/.dll` | Output to a MOZA wheelbase via the MOZA SDK |
| `dinput_output.py`, `dinput_bridge.cpp/.dll` | Output to any DirectInput-capable wheelbase |
| `convert_ibt.py` | Converts iRacing `.ibt` telemetry files for offline use |
| `presets/` | Saved EQ presets, one per car |
| `versions/` | Archived earlier snapshots (v1.1–v1.3) and one-off patch scripts, kept for history — see `CHANGELOG.md` |
| `scratch/` | Ad hoc test/debug scripts, not part of the app |

## Requirements

See `requirements.txt`. Building `dinput_bridge.dll` / `moza_bridge.dll`
from source requires a Windows toolchain — see `build_dinput_bridge.bat`
and `build_bridge.bat`.
