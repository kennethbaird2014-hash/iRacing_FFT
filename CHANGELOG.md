# Changelog

This project's early history was tracked as a handful of standalone script
snapshots rather than incremental commits. This file reconstructs that
history from each snapshot's own docstring plus the git log, so the
version lineage is clear to anyone new to the repo.

Older snapshots live under [`versions/`](versions/) for reference; only
`ffb_analyzer.py` at the repo root is maintained going forward.

## v2.0 — current (`ffb_analyzer.py`)

- Slew-rate limiter — caps per-sample delta to kill whiplash spikes
- Crash / curb protection — G-force triggered gain duck
- Output curve — tanh-based non-linear shaping
- Output smoothing — single-pole low-pass on the DSP output stream
- Waterfall: pre-allocated circular buffer instead of `np.roll`, removing
  per-frame heap allocations and the resulting UI stutter
- EMA smoothing done in-place, removing per-frame temp array allocations
- `set_bands()` shallow-copies each EQ band so the iRacing worker thread
  can't corrupt filter parameters mid-chunk
- Global FX panel split into DSP / Output / Tools tabs
- `NullFFBOutput` — full FFB output interface that discards torque and
  records stats instead, for hardware-free / Linux testing
- Telemetry recording and replay (`recordings/*.npz`) for offline DSP
  testing without iRacing running
- `VirtualMozaOutput` and a "MOZA Virtual Lab" tab — an on-screen animated
  wheel (`virtual_lab.py`, `virtual_wheel.png`) driven by the DSP output,
  for testing without a physical wheelbase
- Replay pause/stop controls and a sim-mode toggle, plus a failsafe check
- `dinput_bridge.cpp` switched to `DISCL_EXCLUSIVE` per its own header
  comment (was `DISCL_NONEXCLUSIVE`)
- `convert_ibt.py` — converts iRacing `.ibt` telemetry files for offline use
- 28 FFT filter presets added under `presets/`
- All v1.2 optimizations preserved

## v1.3 (`versions/ffb_analyzer_v1.3.py`)

- Racing wheel button assignment (listen / gain up / gain down), backed
  by `settings.json`
- Last version before development moved to the v2.0 line

## v1.2 (Optimized) (`versions/ffb_analyzer_v1.2_optimized.py`)

- 60 Hz UI refresh rate
- FFT output truncated to the display range, reducing CPU/GPU load
- Native-orientation waterfall buffer (zero-copy texture uploads)
- Waterfall freezes on pause for analysis
- All v1.1 features preserved

## v1.2 (`versions/ffb_analyzer_v1.2.py`)

- Locked reference checkpoint (marked "do not modify" in the original repo)
- MOZA wheelbase serial connection

## v1.1 (`versions/ffb_analyzer_v1.1.py`, `versions/ffb_analyzer_v1.1_backup.py`)

- Always-on-top window
- Frequency range extended to 100 Hz (requires 360 Hz iRacing telemetry)
- Save / load EQ presets (`presets/` subfolder)
- "Detect Peaks" — auto-generates EQ bands from prominent spectral peaks
- Log-scale X-axis toggle (spectrum and waterfall stay in sync)
- X-axis view lock between spectrum and waterfall
- Larger, resizable EQ band panel
- `versions/ffb_analyzer_v1.1_backup.py` is a divergent backup taken during
  v1.1 development; kept for reference alongside the canonical v1.1 file

## Patch scripts (`versions/patches/`)

`update_ffb.py`, `update_ui.py`, `update_ui_2.py`, `update_ui_3.py` are
one-off scripts that applied string-replacement patches to the analyzer
script during earlier development. They target older file contents and
are kept only as a historical record — they are not meant to be run
against the current `ffb_analyzer.py`.
