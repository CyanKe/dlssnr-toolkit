// truehdr_host.cpp — RTX Video TrueHDR host (SDR -> HDR up-conversion).
//
// Copyright (c) 2026 Cyanke. MIT License — see LICENSE at the repository root.
// Original work: not derived from purkatyy/DLSS5- and has no counterpart
// upstream. It requires the NVIDIA RTX Video SDK to build and the runtime DLLs
// to run; none of those are distributed with this repository (user-supplied).
//
// WHAT THIS IS (and is not): TrueHDR takes an 8-bit SDR image and synthesises a
// linear-light HDR version of it. It does NOT decode or preserve an existing
// HDR10 stream -- the SDK samples even label 10-bit P010 input as "assumed SDR".
// So this adds "produce HDR10 output from SDR source", not "handle HDR video".
//
// Contracts, from the official SDK (RTX_Video_SDK_v1.1.0):
//   input  : DXGI_FORMAT_R8G8B8A8_UNORM or B8G8R8A8_UNORM (same resolution)
//   output : DXGI_FORMAT_R16G16B16A16_FLOAT  (linear scRGB, may exceed 1.0)
//            or DXGI_FORMAT_R10G10B10A2_UNORM (ABGR10, clamped)
//   the output texture must carry D3D11_BIND_UNORDERED_ACCESS
//
// ISOLATION IS DELIBERATE. This DLL statically links its own copy of the SDK's
// NGX loader (nvsdk_ngx_s.lib) and calls NVSDK_NGX_D3D11_Init itself, exactly
// like Magpie's RtxVideoBridge. Our DLSSNR host uses a *different* NGX loader
// (D3D12, a different app id). NVIDIA has acknowledged a compatibility issue
// between DLSS and the RTX Video SDK's TrueHDR capability query, so the two
// must be able to fail independently.
//
// Exports:
//   thdr_init(w, h, outFormat, logPath) -> 1 ok   (outFormat 0=FP16, 1=R10G10B10A2)
//   thdr_available()                    -> 1 if TrueHDR.Available on this system
//   thdr_set_params(contrast, saturation, middleGray, maxLuminance)
//   thdr_process(inBgr, outBuf)         -> 1 ok
//       outFormat 0: outBuf is w*h*4 uint16 (FP16 RGBA, linear scRGB)
//       outFormat 1: outBuf is w*h   uint32 (R10G10B10A2 packed)
//   thdr_out_bytes()                    -> bytes per frame the caller must provide
//   thdr_shutdown()
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
#include <immintrin.h>

#include <nvsdk_ngx.h>
#include <nvsdk_ngx_defs.h>
#include <nvsdk_ngx_defs_truehdr.h>
#include <nvsdk_ngx_helpers_truehdr.h>

// The SDK samples use app id 0 with "." as the data path; nothing here ships to
// end users, so there is no reason to invent a project id.
#define THDR_APP_ID   0
#define THDR_APP_PATH L"."

// ---------------------------------------------------------------------------
// logging
// ---------------------------------------------------------------------------
static wchar_t g_logPath[MAX_PATH] = L"";
static int     g_perf = 0;

static void HLog(const char* fmt, ...) {
    if (!g_logPath[0]) return;
    FILE* f = nullptr;
    if (_wfopen_s(&f, g_logPath, L"a") != 0 || !f) return;
    va_list ap; va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static double NowMs() {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1000.0 / (double)f.QuadPart;
}

// BGR -> RGBA, 4 px per step (same routine as the other two hosts).
static void BGR2RGBA_row(const uint8_t* src, uint8_t* dst, int64_t w) {
    const __m128i msk = _mm_setr_epi8(2,1,0,-1, 5,4,3,-1, 8,7,6,-1, 11,10,9,-1);
    const __m128i alpha = _mm_set1_epi32((int)0xFF000000);
    int64_t grp = 0;
    const int64_t maxG = (w * 3 - 16) / 12;
    for (; grp < maxG; ++grp) {
        __m128i s = _mm_loadu_si128((const __m128i*)(src + grp * 12));
        _mm_storeu_si128((__m128i*)(dst + grp * 16),
                         _mm_or_si128(_mm_shuffle_epi8(s, msk), alpha));
    }
    for (int64_t i = grp * 4; i < w; ++i) {
        dst[i * 4 + 0] = src[i * 3 + 2];
        dst[i * 4 + 1] = src[i * 3 + 1];
        dst[i * 4 + 2] = src[i * 3 + 0];
        dst[i * 4 + 3] = 255;
    }
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
struct State {
    int w = 0, h = 0;
    int outFp16 = 1;                 // 1 = FP16 RGBA, 0 = R10G10B10A2

    ID3D11Device*        device  = nullptr;
    ID3D11DeviceContext* ctx     = nullptr;
    ID3D11Texture2D*     texIn   = nullptr;
    ID3D11Texture2D*     texOut  = nullptr;
    ID3D11Texture2D*     staging = nullptr;

    NVSDK_NGX_Parameter* params  = nullptr;
    NVSDK_NGX_Handle*    feature = nullptr;

    // NGX is a process-wide singleton and its Shutdown1 is unreliable here: it
    // hangs indefinitely after a successful init + available query (observed,
    // and consistent with Magpie's NgxRuntimeGuard treating shutdown as a
    // hazard). So NGX is initialised at most ONCE per process and is never shut
    // down; only the feature and textures are released. Process exit reclaims
    // the rest.
    bool ngxInited = false;
    int  hdrAvailable = -1;          // -1 unknown, 1 yes, 0 no
    bool inited = false;

    // live parameters
    unsigned contrast = 100, saturation = 100, middleGray = 50, maxLuminance = 1000;

    uint8_t* inStage = nullptr;      // w*h*4 RGBA8
};
static State g;
static HMODULE g_self = nullptr;

// A faulted NGX core can still own driver callbacks, so once something throws
// we never call back into it again (mirrors Magpie's NgxRuntimeGuard).
static bool g_faulted = false;

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) { g_self = h; DisableThreadLibraryCalls(h); }
    return TRUE;
}

static void ModuleDir(wchar_t* out, DWORD cch) {
    GetModuleFileNameW(g_self, out, cch);
    wchar_t* slash = wcsrchr(out, L'\\');
    if (slash) *slash = 0;
}

// ---------------------------------------------------------------------------
// teardown
// ---------------------------------------------------------------------------
static void Teardown() {
    if (g.feature) {
        NVSDK_NGX_D3D11_ReleaseFeature(g.feature);
        g.feature = nullptr;
    }
    // params, device and the NGX core deliberately survive: see the State
    // comment. Destroying them here is what hangs.

    if (g.staging) { g.staging->Release(); g.staging = nullptr; }
    if (g.texOut)  { g.texOut->Release();  g.texOut  = nullptr; }
    if (g.texIn)   { g.texIn->Release();   g.texIn   = nullptr; }
    // The device and immediate context are kept too -- they are cheap to hold
    // and re-creating them after an NGX init is exactly the sequence that
    // triggers the hang.
    if (g.inStage) { _aligned_free(g.inStage); g.inStage = nullptr; }
    g.inited = false;
}

// Initialises NGX at most once per process and records whether TrueHDR is
// available. Idempotent and safe to call from the probe entry point.
static bool CreateDevice();
static bool EnsureNgx(const wchar_t* dirForLog) {
    if (g.ngxInited) return true;
    if (g_faulted) { HLog("NGX unusable: previously faulted"); return false; }

    wchar_t dir[MAX_PATH];
    ModuleDir(dir, MAX_PATH);
    SetDllDirectoryW(dir);
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_USER_DIRS);
    AddDllDirectory(dir);

    if (!CreateDevice()) return false;

    HLog("NGX D3D11 init (appPath=%ls)", dir);
    NVSDK_NGX_Result r = NVSDK_NGX_D3D11_Init(THDR_APP_ID, dir, g.device);
    if (NVSDK_NGX_FAILED(r)) { HLog("NVSDK_NGX_D3D11_Init failed 0x%08X", (unsigned)r); return false; }

    r = NVSDK_NGX_D3D11_GetCapabilityParameters(&g.params);
    if (NVSDK_NGX_FAILED(r) || !g.params) {
        HLog("GetCapabilityParameters failed 0x%08X", (unsigned)r);
        g.ngxInited = true;              // do not retry a broken core
        return false;
    }

    // The call NVIDIA documented as conflicting with DLSS in some combinations.
    int avail = 0;
    r = g.params->Get(NVSDK_NGX_Parameter_TrueHDR_Available, &avail);
    g.hdrAvailable = (!NVSDK_NGX_FAILED(r) && avail) ? 1 : 0;
    HLog("TrueHDR.Available -> %d (result 0x%08X)", g.hdrAvailable, (unsigned)r);

    if (!g.hdrAvailable) {
        int needsDriver = 0;
        g.params->Get(NVSDK_NGX_Parameter_TrueHDR_NeedsUpdatedDriver, &needsDriver);
        int maj = 0, min = 0;
        g.params->Get(NVSDK_NGX_Parameter_TrueHDR_MinDriverVersionMajor, &maj);
        g.params->Get(NVSDK_NGX_Parameter_TrueHDR_MinDriverVersionMinor, &min);
        HLog("TrueHDR unavailable: needsUpdatedDriver=%d minDriver=%d.%d",
             needsDriver, maj, min);
    }
    g.ngxInited = true;
    return true;
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------
static bool CreateDevice() {
    if (g.device) return true;
    static const D3D_FEATURE_LEVEL levels[] = {
        D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0
    };
    D3D_FEATURE_LEVEL got{};
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
                                   levels, _countof(levels), D3D11_SDK_VERSION,
                                   &g.device, &got, &g.ctx);
    if (FAILED(hr) || !g.device) { HLog("D3D11CreateDevice failed 0x%08X", (unsigned)hr); return false; }
    HLog("D3D11 device created, feature level 0x%X", (unsigned)got);
    return true;
}

static bool CreateFeature(int w, int h) {
    const DXGI_FORMAT outFmt = g.outFp16 ? DXGI_FORMAT_R16G16B16A16_FLOAT
                                         : DXGI_FORMAT_R10G10B10A2_UNORM;

    // ---- input: RGBA8, CPU-writable ----
    D3D11_TEXTURE2D_DESC d{};
    d.Width = (UINT)w; d.Height = (UINT)h;
    d.MipLevels = 1; d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g.device->CreateTexture2D(&d, nullptr, &g.texIn))) {
        HLog("input texture create failed"); return false;
    }

    // ---- output: HDR format. The SDK requires UNORDERED_ACCESS here; without
    //      it the sample falls back to an internal temp texture. ----
    d.Format = outFmt;
    if (FAILED(g.device->CreateTexture2D(&d, nullptr, &g.texOut))) {
        HLog("output texture create failed (fmt=%d)", (int)outFmt); return false;
    }

    // ---- staging copy for readback ----
    D3D11_TEXTURE2D_DESC sd = d;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.BindFlags = 0;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (FAILED(g.device->CreateTexture2D(&sd, nullptr, &g.staging))) {
        HLog("staging texture create failed"); return false;
    }

    // ---- NGX is already up (EnsureNgx ran first); just create the feature ----
    size_t scratch = 0;
    NVSDK_NGX_Result r = NVSDK_NGX_D3D11_GetScratchBufferSize(
        NVSDK_NGX_Feature_TrueHDR, g.params, &scratch);
    HLog("TrueHDR scratch buffer = %zu bytes (result 0x%08X)", scratch, (unsigned)r);

    NVSDK_NGX_Feature_Create_Params cp{};
    r = NGX_D3D11_CREATE_TRUEHDR_EXT(g.ctx, &g.feature, g.params, &cp);
    if (NVSDK_NGX_FAILED(r) || !g.feature) {
        HLog("NGX_D3D11_CREATE_TRUEHDR_EXT failed 0x%08X", (unsigned)r); return false;
    }
    HLog("TrueHDR feature created (output %s)",
         g.outFp16 ? "R16G16B16A16_FLOAT" : "R10G10B10A2_UNORM");
    return true;
}

// ---------------------------------------------------------------------------
// exports
// ---------------------------------------------------------------------------
extern "C" {

__declspec(dllexport) int thdr_init(int w, int h, int outFormat, const wchar_t* logPath) {
    if (logPath) wcsncpy_s(g_logPath, _countof(g_logPath), logPath, _TRUNCATE);
    { FILE* f = nullptr; fopen_s(&f, "thdr_perf.on", "rb"); if (f) { g_perf = 1; fclose(f); } }

    HLog("=== thdr_init %dx%d outFormat=%d ===", w, h, outFormat);
    if (g_faulted) { HLog("refusing: NGX previously faulted"); return 0; }
    if (w <= 0 || h <= 0) return 0;

    if (g.inited && g.w == w && g.h == h && g.outFp16 == ((outFormat == 0) ? 1 : 0)) return 1;

    if (!EnsureNgx(L"")) return 0;
    if (!g.hdrAvailable) { HLog("TrueHDR not available; refusing init"); return 0; }

    // Only the feature and textures are rebuilt on a size/format change; the
    // device and NGX core stay up.
    if (g.feature) { NVSDK_NGX_D3D11_ReleaseFeature(g.feature); g.feature = nullptr; }
    if (g.staging) { g.staging->Release(); g.staging = nullptr; }
    if (g.texOut)  { g.texOut->Release();  g.texOut  = nullptr; }
    if (g.texIn)   { g.texIn->Release();   g.texIn   = nullptr; }
    if (g.inStage) { _aligned_free(g.inStage); g.inStage = nullptr; }

    g.w = w; g.h = h; g.outFp16 = (outFormat == 0) ? 1 : 0;

    if (!CreateFeature(w, h)) { Teardown(); return 0; }

    g.inStage = (uint8_t*)_aligned_malloc((size_t)w * h * 4, 64);
    if (!g.inStage) { Teardown(); return 0; }

    g.inited = true;
    HLog("init OK %dx%d -> %s", w, h,
         g.outFp16 ? "FP16 RGBA (linear scRGB)" : "R10G10B10A2");
    return 1;
}

// Does not create a feature; safe to call to decide whether to offer the option.
// Takes a log path because this is the first NGX call in a fresh process and is
// therefore the most likely place to stall (NGX locates -- and with OTA enabled
// may download -- the feature runtime here).
__declspec(dllexport) int thdr_available(const wchar_t* logPath) {
    if (logPath) wcsncpy_s(g_logPath, _countof(g_logPath), logPath, _TRUNCATE);
    HLog("=== thdr_available probe ===");
    if (g_faulted) { HLog("refusing: NGX previously faulted"); return 0; }
    if (!EnsureNgx(L"")) return 0;
    return g.hdrAvailable == 1 ? 1 : 0;
}

__declspec(dllexport) void thdr_set_params(unsigned contrast, unsigned saturation,
                                            unsigned middleGray, unsigned maxLuminance) {
    // Ranges enforced by the SDK: contrast/saturation 0-200, middleGray 10-100,
    // maxLuminance 400-2000.
    g.contrast     = contrast     > 200 ? 200 : contrast;
    g.saturation   = saturation   > 200 ? 200 : saturation;
    g.middleGray   = middleGray   < 10 ? 10 : (middleGray > 100 ? 100 : middleGray);
    g.maxLuminance = maxLuminance < 400 ? 400 : (maxLuminance > 2000 ? 2000 : maxLuminance);
    HLog("params contrast=%u saturation=%u middleGray=%u maxLuminance=%u",
         g.contrast, g.saturation, g.middleGray, g.maxLuminance);
}

__declspec(dllexport) int thdr_out_bytes() {
    if (!g.w || !g.h) return 0;
    return g.outFp16 ? g.w * g.h * 8 : g.w * g.h * 4;
}

__declspec(dllexport) int thdr_process(const uint8_t* inBgr, void* outBuf) {
    if (!g.inited || !g.feature) { HLog("process: not inited"); return 0; }
    if (!inBgr || !outBuf) return 0;
    if (g_faulted) return 0;

    const int W = g.w, H = g.h;
    const double t0 = NowMs();

    // 1. BGR -> RGBA into the staging buffer, then upload.
    for (int y = 0; y < H; ++y)
        BGR2RGBA_row(inBgr + (size_t)y * W * 3, g.inStage + (size_t)y * W * 4, W);
    g.ctx->UpdateSubresource(g.texIn, 0, nullptr, g.inStage, (UINT)W * 4, 0);
    const double t1 = NowMs();

    // 2. Evaluate. ClearState first: a leftover binding from another component
    //    makes NGX refuse the call.
    g.ctx->ClearState();
    NVSDK_NGX_D3D11_TRUEHDR_Eval_Params ep{};
    ep.pInput = g.texIn;
    ep.pOutput = g.texOut;
    ep.InputSubrectTL.X = 0; ep.InputSubrectTL.Y = 0;
    ep.InputSubrectBR.Width = (unsigned)W; ep.InputSubrectBR.Height = (unsigned)H;
    ep.OutputSubrectTL.X = 0; ep.OutputSubrectTL.Y = 0;
    ep.OutputSubrectBR.Width = (unsigned)W; ep.OutputSubrectBR.Height = (unsigned)H;
    ep.Contrast = g.contrast;
    ep.Saturation = g.saturation;
    ep.MiddleGray = g.middleGray;
    ep.MaxLuminance = g.maxLuminance;

    NVSDK_NGX_Result r = NGX_D3D11_EVALUATE_TRUEHDR_EXT(g.ctx, g.feature, g.params, &ep);
    g.ctx->ClearState();
    if (NVSDK_NGX_FAILED(r)) {
        HLog("EVALUATE_TRUEHDR failed 0x%08X", (unsigned)r);
        return 0;
    }
    const double t2 = NowMs();

    // 3. Read back through a staging texture -- the output is a render target
    //    and cannot be mapped directly.
    g.ctx->CopyResource(g.staging, g.texOut);
    D3D11_MAPPED_SUBRESOURCE m{};
    if (FAILED(g.ctx->Map(g.staging, 0, D3D11_MAP_READ, 0, &m))) {
        HLog("staging map failed"); return 0;
    }
    const int bpp = g.outFp16 ? 8 : 4;
    uint8_t* dst = (uint8_t*)outBuf;
    for (int y = 0; y < H; ++y)
        memcpy(dst + (size_t)y * W * bpp,
               (const uint8_t*)m.pData + (size_t)y * m.RowPitch,
               (size_t)W * bpp);
    g.ctx->Unmap(g.staging, 0);
    const double t3 = NowMs();

    if (g_perf) {
        HLog("PERF conv+upload=%.3f evaluate=%.3f readback=%.3f total=%.3f ms",
             t1 - t0, t2 - t1, t3 - t2, t3 - t0);
    }
    return 1;
}

__declspec(dllexport) void thdr_shutdown() {
    // Releases the feature and textures only. NGX itself is intentionally left
    // running -- see the State comment; shutting it down hangs this process.
    HLog("=== shutdown (NGX core retained) ===");
    Teardown();
}

} // extern "C"
