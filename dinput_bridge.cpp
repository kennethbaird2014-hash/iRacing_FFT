// dinput_bridge.cpp — Pure DirectInput constant-force FFB bridge
//
// Replaces moza_bridge.cpp.  Instead of using the MOZA proprietary SDK (which
// routes through Pit House and is blocked when iRacing FFB = 0%), this bridge
// talks to the wheel as a standard DirectInput FFB device using
// EXCLUSIVE | BACKGROUND cooperative level — the same technique MAIRA uses.
//
// With EXCLUSIVE access our effect runs unconditionally regardless of
// what iRacing's FFB slider is set to.
//
// Build: see build_dinput_bridge.bat
// Requires: dinput8.lib, dxguid.lib  (both ship with the Windows SDK)
// No MOZA SDK dependency.

#define DIRECTINPUT_VERSION 0x0800
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dinput.h>
#include <wbemidl.h>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#pragma comment(lib, "dinput8.lib")
#pragma comment(lib, "dxguid.lib")

// ── DI_FFNOMINALMAX ──────────────────────────────────────────────────────────
static constexpr int DI_MAX = DI_FFNOMINALMAX;   // 10 000

// ── Module state ─────────────────────────────────────────────────────────────
static IDirectInput8*       g_pDI       = nullptr;
static IDirectInputDevice8* g_pDevice   = nullptr;
static IDirectInputEffect*  g_pEffect   = nullptr;

static DIEFFECT          g_eff        = {};
static DICONSTANTFORCE   g_cf         = {};
static LONG              g_axes[1]    = { DIJOFS_X };
static LONG              g_dirs[1]    = { 1 };

static bool              g_active     = false;
static std::mutex        g_mutex;

// Name fragment to match against device product name (case-insensitive).
// Empty string matches any FFB device (picks the first one found).
static std::wstring      g_target_name;   // set via dinput_set_target_name()

// ── Device enumeration callback ───────────────────────────────────────────────
struct EnumCtx {
    IDirectInput8*       pDI;
    HWND                 hwnd;
    IDirectInputDevice8* pFound;
    std::wstring         targetName;
};

static BOOL CALLBACK EnumFFDevices(const DIDEVICEINSTANCEW* pdidInstance, VOID* pContext)
{
    auto* ctx = static_cast<EnumCtx*>(pContext);

    // Check target name filter
    if (!ctx->targetName.empty()) {
        std::wstring prod = pdidInstance->tszProductName;
        // toLower comparison
        auto lc = [](std::wstring s) { for (auto& c : s) c = towlower(c); return s; };
        if (lc(prod).find(lc(ctx->targetName)) == std::wstring::npos)
            return DIENUM_CONTINUE;
    }

    IDirectInputDevice8* pDevice = nullptr;
    HRESULT hr = ctx->pDI->CreateDevice(pdidInstance->guidInstance, &pDevice, nullptr);
    if (FAILED(hr)) return DIENUM_CONTINUE;

    // Must be an FFB device — check capabilities
    DIDEVCAPS caps = {};
    caps.dwSize = sizeof(caps);
    pDevice->GetCapabilities(&caps);
    if (!(caps.dwFlags & DIDC_FORCEFEEDBACK)) {
        pDevice->Release();
        return DIENUM_CONTINUE;
    }

    ctx->pFound = pDevice;
    return DIENUM_STOP;
}

// ── Effect GUID enumeration callback ─────────────────────────────────────────
struct EffEnumCtx {
    GUID found;
    bool ok;
};

static BOOL CALLBACK EnumEffects(LPCDIEFFECTINFOW pdei, VOID* pContext)
{
    auto* ctx = static_cast<EffEnumCtx*>(pContext);
    // We want hardware-supported constant force
    if ((pdei->dwEffType & DIEFT_CONSTANTFORCE) == DIEFT_CONSTANTFORCE) {
        ctx->found = pdei->guid;
        ctx->ok    = true;
        return DIENUM_STOP;
    }
    return DIENUM_CONTINUE;
}

// ── Internal helpers ──────────────────────────────────────────────────────────
static void _teardown_unlocked()
{
    if (g_pEffect) {
        try { g_pEffect->Stop(); } catch (...) {}
        g_pEffect->Release();
        g_pEffect = nullptr;
    }
    if (g_pDevice) {
        try { g_pDevice->Unacquire(); } catch (...) {}
        g_pDevice->Release();
        g_pDevice = nullptr;
    }
    if (g_pDI) {
        g_pDI->Release();
        g_pDI = nullptr;
    }
    g_active = false;
}

// ── Exported C API ────────────────────────────────────────────────────────────
extern "C" {

// Optional: set a device name substring filter before calling dinput_init.
// Example: "MOZA"  or  "Fanatec"  or "" (first FFB device found).
// Call before dinput_init.
__declspec(dllexport) void __cdecl dinput_set_target_name(const wchar_t* name)
{
    g_target_name = name ? name : L"";
}

// Initialise DirectInput and create a constant-force effect on the wheel.
// hwnd: Win32 window handle (PyQt6: int(widget.winId())).
// Returns:
//   0  — success
//  -1  — DirectInput8Create failed
//  -2  — No FFB device found
//  -3  — SetCooperativeLevel / Acquire failed
//  -4  — No ConstantForce effect supported by device
//  -5  — CreateEffect failed
__declspec(dllexport) int __cdecl dinput_init(void* hwnd)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_active) return 0;

    // 1. Create IDirectInput8
    HRESULT hr = DirectInput8Create(
        GetModuleHandle(nullptr),
        DIRECTINPUT_VERSION,
        IID_IDirectInput8W,
        reinterpret_cast<void**>(&g_pDI),
        nullptr);
    if (FAILED(hr)) return -1;

    // 2. Enumerate FFB devices
    EnumCtx ctx{ g_pDI, (HWND)hwnd, nullptr, g_target_name };
    g_pDI->EnumDevices(
        DI8DEVCLASS_GAMECTRL,
        EnumFFDevices,
        &ctx,
        DIEDFL_ATTACHEDONLY | DIEDFL_FORCEFEEDBACK);

    if (!ctx.pFound) {
        g_pDI->Release(); g_pDI = nullptr;
        return -2;
    }
    g_pDevice = ctx.pFound;

    // 3. Set exclusive+background cooperative level and acquire
    hr = g_pDevice->SetCooperativeLevel(
        (HWND)hwnd,
        DISCL_NONEXCLUSIVE | DISCL_BACKGROUND);  // coexists with Pit House
    if (FAILED(hr)) { _teardown_unlocked(); return -3; }

    hr = g_pDevice->Acquire();
    if (FAILED(hr)) { _teardown_unlocked(); return -3; }

    // 4. Enumerate effects to find ConstantForce GUID
    EffEnumCtx effCtx{};
    g_pDevice->EnumEffects(EnumEffects, &effCtx, DIEFT_CONSTANTFORCE);
    if (!effCtx.ok) { _teardown_unlocked(); return -4; }

    // 5. Build DIEFFECT struct for a constant-force effect
    ZeroMemory(&g_cf,  sizeof(g_cf));
    ZeroMemory(&g_eff, sizeof(g_eff));

    g_cf.lMagnitude = 0;

    g_axes[0] = DIJOFS_X;
    g_dirs[0] = 1;

    g_eff.dwSize                  = sizeof(DIEFFECT);
    g_eff.dwFlags                 = DIEFF_CARTESIAN | DIEFF_OBJECTOFFSETS;
    g_eff.dwDuration              = INFINITE;          // play forever
    g_eff.dwSamplePeriod          = 0;                 // driver default
    g_eff.dwGain                  = DI_FFNOMINALMAX;   // full gain
    g_eff.dwTriggerButton         = DIEB_NOTRIGGER;
    g_eff.dwTriggerRepeatInterval = 0;
    g_eff.cAxes                   = 1;
    g_eff.rgdwAxes                = (DWORD*)g_axes;
    g_eff.rglDirection            = g_dirs;
    g_eff.lpEnvelope              = nullptr;           // no envelope
    g_eff.cbTypeSpecificParams    = sizeof(DICONSTANTFORCE);
    g_eff.lpvTypeSpecificParams   = &g_cf;
    g_eff.dwStartDelay            = 0;

    hr = g_pDevice->CreateEffect(effCtx.found, &g_eff, &g_pEffect, nullptr);
    if (FAILED(hr) || !g_pEffect) { _teardown_unlocked(); return -5; }

    // 6. Download effect to device and start
    g_pEffect->Download();
    g_pEffect->Start(1, DIES_SOLO);   // 1 iteration, stop other effects

    g_active = true;
    return 0;
}

// Set wheel torque.  mag is DirectInput units: -10 000 … +10 000.
// Lock-free hot path — safe to call at 360 Hz from a worker thread.
__declspec(dllexport) void __cdecl dinput_set_magnitude(int32_t mag)
{
    if (!g_active || !g_pEffect) return;

    g_cf.lMagnitude = (LONG)mag;
    // Update only the type-specific parameters (magnitude) — no re-download needed.
    g_pEffect->SetParameters(&g_eff, DIEP_TYPESPECIFICPARAMS);
}

// Zero force (safe to call any time, any thread).
__declspec(dllexport) void __cdecl dinput_stop(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_active && g_pEffect) {
        g_cf.lMagnitude = 0;
        g_pEffect->SetParameters(&g_eff, DIEP_TYPESPECIFICPARAMS);
        g_pEffect->Stop();
    }
}

// Full teardown.
__declspec(dllexport) void __cdecl dinput_cleanup(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_pEffect) {
        g_cf.lMagnitude = 0;
        try { g_pEffect->SetParameters(&g_eff, DIEP_TYPESPECIFICPARAMS); } catch (...) {}
    }
    _teardown_unlocked();
}

// Returns 1 if effect is running.
__declspec(dllexport) int __cdecl dinput_is_active(void)
{
    return g_active ? 1 : 0;
}

// ── Enumerate-devices helper (for diagnostics) ────────────────────────────────
struct ListCtx {
    wchar_t* buf;
    int maxn, count;
};

static BOOL CALLBACK EnumAllFFDevices(const DIDEVICEINSTANCEW* pdidi, VOID* pv)
{
    auto* c = static_cast<ListCtx*>(pv);
    if (c->buf && c->count < c->maxn) {
        wchar_t* dst = c->buf + c->count * 256;
        wcsncpy_s(dst, 256, pdidi->tszProductName, _TRUNCATE);
    }
    c->count++;
    return DIENUM_CONTINUE;
}

// Returns number of FFB devices found (for diagnostics).
// out_names: flat buffer of wchar_t[max_names * 256] — each entry is a name.
// Pass NULL / 0 to just get the count.
__declspec(dllexport) int __cdecl dinput_enumerate_devices(wchar_t* out_names, int max_names)
{
    IDirectInput8W* pDI = nullptr;
    bool ownDI = false;
    if (!g_pDI) {
        HRESULT hr = DirectInput8Create(
            GetModuleHandle(nullptr),
            DIRECTINPUT_VERSION,
            IID_IDirectInput8W,
            reinterpret_cast<void**>(&pDI),
            nullptr);
        if (FAILED(hr)) return 0;
        ownDI = true;
    } else {
        pDI = reinterpret_cast<IDirectInput8W*>(g_pDI);
    }

    ListCtx ctx{ out_names, max_names, 0 };
    pDI->EnumDevices(DI8DEVCLASS_GAMECTRL, EnumAllFFDevices, &ctx,
                     DIEDFL_ATTACHEDONLY | DIEDFL_FORCEFEEDBACK);

    if (ownDI) pDI->Release();
    return ctx.count;
}

}  // extern "C"
