// moza_bridge.cpp — Thin C wrapper around MOZA SDK for Python ctypes
//
// The MOZA SDK exports C++ classes (std::shared_ptr, etc.) which cannot be
// called directly from Python.  This tiny DLL re-exports the handful of
// functions we need as plain C / __cdecl so that ctypes.CDLL can load them.
//
// Build:  see build_bridge.bat
// Requires: MOZA Pit House running, wheelbase connected via USB.

#include "mozaAPI.h"
#include <memory>
#include <mutex>
#include <cstdint>
#include <windows.h>

// ── state ────────────────────────────────────────────────────────────────────
static std::shared_ptr<RS21::direct_input::ETConstantForce> g_cf;
static std::mutex  g_mutex;
static bool        g_active        = false;
static bool        g_sdk_installed = false;   // SDK is a process-lifetime singleton

// Note on DirectInput access:
// Pit House holds EXCLUSIVE DirectInput access to the wheel at all times.
// Any attempt to also acquire the device via DI will return DIERR_OTHERAPPHASPRIO
// ("Access is denied") regardless of which HWND we pass.  This is expected —
// the ET constant-force channel communicates with the wheel through Pit House's
// IPC layer, not directly through DirectInput.  The HWND passed to
// createWheelbaseETConstantForce is used only for SDK internal bookkeeping;
// DI acquisition failure does not prevent the ET channel from delivering force.

// ── internal helpers ─────────────────────────────────────────────────────────
static void _stop_unlocked()
{
    if (g_active && g_cf) {
        try { g_cf->setMagnitude(0); g_cf->stop(); } catch (...) {}
    }
}

// ── exported C API ───────────────────────────────────────────────────────────
extern "C" {

// Initialise MOZA SDK and create a constant-force effect.
// Returns 0 on success, MOZA ERRORCODE (>0) or -1 on failure.
// `hwnd` is accepted for API compatibility but the hidden background window
// is used instead — this decouples the effect from Qt window focus.
__declspec(dllexport) int __cdecl moza_init(void* hwnd)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_active) return 0;           // effect already running

    // The SDK is a process-lifetime singleton.  Installing it multiple times
    // (install / remove / install) leaves ghost ET effects that stack forces on
    // the wheel.  Install once and never remove until moza_sdk_cleanup().
    if (!g_sdk_installed) {
        moza::installMozaSDK();
        Sleep(2500);                  // SDK needs time to discover devices
        g_sdk_installed = true;
    }

    // Always destroy any stale effect object before creating a new one.
    // Calling g_cf.reset() triggers the ETConstantForce destructor which
    // de-registers it from the SDK's internal pool — prevents ghost effects.
    if (g_cf) {
        try { g_cf->setMagnitude(0); g_cf->stop(); } catch (...) {}
        g_cf.reset();
    }

    ERRORCODE err = NORMAL;
    g_cf = moza::createWheelbaseETConstantForce((HWND)hwnd, err);
    if (err != NORMAL || !g_cf) {
        return err != NORMAL ? (int)err : -1;
    }

    g_cf->setDuration(0xFFFFFFFF);    // infinite
    g_cf->setMagnitude(0);
    try { g_cf->start(); }
    catch (...) {
        g_cf.reset();
        return -1;
    }

    g_active = true;
    return 0;
}

// Set wheel torque.  `mag` is DirectInput units: −10 000 … +10 000.
// For MOZA R12 V2 (12 Nm peak) → 10 000 = full torque.
// Call at up to 360 Hz from the DSP thread; function is lock-free.
//
// Hard safety clamp: ±8 333 ≈ ±10 Nm on a 12 Nm wheelbase.
// This is enforced at the DLL level so no Python bug can exceed it.
// If you need to raise the cap (different wheelbase) rebuild with a higher
// MOZA_MAG_LIMIT, but never exceed ±10 000.
static constexpr int32_t MOZA_MAG_LIMIT = 8333;  // ≈ 10 Nm on R12 V2

__declspec(dllexport) void __cdecl moza_set_magnitude(int32_t mag)
{
    if (mag >  MOZA_MAG_LIMIT) mag =  MOZA_MAG_LIMIT;
    if (mag < -MOZA_MAG_LIMIT) mag = -MOZA_MAG_LIMIT;
    if (g_active && g_cf)
        g_cf->setMagnitude((long)mag);
}

// Zero force and stop the effect (safe to call from any thread).
__declspec(dllexport) void __cdecl moza_stop(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    _stop_unlocked();
}

// Stop and release the ET effect.  Does NOT remove the SDK — the SDK is a
// singleton for the process lifetime so we can safely re-init without
// re-discovering devices (which causes ghost effects to stack).
__declspec(dllexport) void __cdecl moza_cleanup(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    _stop_unlocked();
    g_cf.reset();
    // NOTE: intentionally NOT calling removeMozaSDK here.
    //       Call moza_sdk_cleanup() only at process exit.
    g_active = false;
}

// Query whether the constant-force effect is running.
__declspec(dllexport) int __cdecl moza_is_active(void)
{
    return g_active ? 1 : 0;
}

// ── Motor EQ API ──────────────────────────────────────────────────────────────
// The MOZA SDK exposes a 6-band "road awareness" EQ on the motor.
// Bands (fixed frequencies): 7.5, 13, 22.5, 39, 55, 100 Hz
// Value range: [0, 500] for bands 0-4; [0, 100] for band 5 (100 Hz).
// 100 = unity (0 dB).  All values are amplitude percentages.
//
// These functions let us control Pit House's EQ without touching the device
// ownership model — no ET channel needed, no conflict with iRacing.

static const char* EQ_KEYS[6] = {
    "EqualizerAmp10",  "EqualizerAmp15",  "EqualizerAmp25",
    "EqualizerAmp40",  "EqualizerAmp60",  "EqualizerAmp100"
};
static const int EQ_MAX[6] = { 500, 500, 500, 500, 500, 100 };

// Read current Pit House EQ into out_bands6 (int[6], caller allocates).
// Returns 0 on success, MOZA ERRORCODE on failure.
__declspec(dllexport) int __cdecl moza_get_equalizer(int* out_bands6)
{
    ERRORCODE err = NORMAL;
    const std::map<std::string, int>* m = moza::getMotorEqualizerAmp(err);
    if (err != NORMAL || !m) return (int)err != 0 ? (int)err : -1;

    for (int i = 0; i < 6; ++i) {
        auto it = m->find(EQ_KEYS[i]);
        out_bands6[i] = (it != m->end()) ? it->second : 100;
    }
    return 0;
}

// Push 6 EQ band values to Pit House motor.
// bands6: int[6] in order [7.5, 13, 22.5, 39, 55, 100 Hz].
// Values auto-clamped to valid range.  Returns 0 on success.
__declspec(dllexport) int __cdecl moza_set_equalizer(const int* bands6)
{
    std::map<std::string, int> m;
    for (int i = 0; i < 6; ++i) {
        int v = bands6[i];
        if (v < 0)         v = 0;
        if (v > EQ_MAX[i]) v = EQ_MAX[i];
        m[EQ_KEYS[i]] = v;
    }
    ERRORCODE err = moza::setMotorEqualizerAmp(m);
    return (int)err;
}

// installMozaSDK is required before the EQ calls can work.
// This init is SDK-only (no device/effect) — safe to call without a window.
// Shares the g_sdk_installed singleton flag with moza_init so that calling
// this first (PH EQ "Connect SDK" button) then moza_init does NOT double-install.
__declspec(dllexport) int __cdecl moza_sdk_init(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_sdk_installed) return 0;   // already installed by moza_init or prior call
    try {
        moza::installMozaSDK();
        Sleep(1500);   // shorter wait — no device effect to create
        g_sdk_installed = true;
        return 0;
    } catch (...) {
        return -1;
    }
}

__declspec(dllexport) void __cdecl moza_sdk_cleanup(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_sdk_installed) return;
    try { moza::removeMozaSDK(); } catch (...) {}
    g_sdk_installed = false;
}

}  // extern "C"
