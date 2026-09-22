// dlssnr_host2.cpp — GPU-direct DLSS Feature 18 (neural render / NR) host.
//
// Copyright (c) 2026 Cyanke. MIT License — see LICENSE at the repository root.
//
// Provenance / 来源:
//   The upstream project purkatyy/DLSS5- (MIT, (c) 2026 ylso0) shipped a PREBUILT
//   dlssnr_host.dll, without source. This file is a from-scratch reimplementation
//   of that host's role by Cyanke, with a different contract: it takes packed BGR
//   straight from cv2 and can return RGBA straight from the GPU readback, which
//   removes the Python-side conversions that dominated the per-frame cost.
//   It also keeps a D3D12 device/queue/pipeline alive across frames instead of
//   rebuilding state per call.
//
//   The DLSS "NR" snippet it drives is NVIDIA's (nvngx_dlssnr.dll); that binary is
//   NOT distributed with this repository and must be supplied by the user.
//
// WHY THIS EXISTS (vs the old dlssnr_host.dll):
//   The old host's contract was dlssnr_process(inRgba, mv, dp, outRgba) — four CPU
//   pointers — which forces the Python side to do BGR->RGB + dstack(alpha) before
//   the call and RGBA->BGR after it. Those two numpy passes cost more than the GPU
//   work itself at 720p/1080p.
//
//   This host takes BGR straight from cv2 and can hand back RGBA straight from the
//   GPU readback:
//       in : BGR8  (H*W*3)  — exactly what cv2.VideoCapture gives us
//       out: RGBA8 (H*W*4)  — exactly what ffmpeg (-pix_fmt rgba) / PIL want
//   The only conversions left are a SIMD BGR->RGBA on the upload path (the GPU needs
//   RGBA input) and, in RGBA mode, NO conversion at all on the output path.
//
// Also caches every D3D12 object (allocator, command list, buffers) instead of
// recreating them per phase per frame, and uses TWO slots ("double buffering"):
// submit() returns without waiting, fetch() waits for the oldest slot. The CPU
// therefore stages/records frame N while the GPU is still running frame N-1.
//
// Exports:
//   dlssnr2_set_appdir(dir)                         -> where nvngx_dlssnr.dll
//                                                      lives; BEFORE first init
//   dlssnr2_init(w, h, logPath)                     -> 1 ok
//   -- synchronous (preview) --
//   dlssnr2_process(inBgr, outBgr, reset)           -> BGR in / BGR out
//   dlssnr2_process_rgba(inBgr, outRgba, reset)     -> BGR in / RGBA out
//   -- multi-layer, one command list, one feature per layer --
//   dlssnr2_chain(inBgr, outBgr, outRgba, layers, nLayers, reset)
//                                                   -> 1 ok / 0 fail
//       layers : const DlssnrLayerOpts* (style, intensity, localTone,
//                localStruct, skinStruct, autoMask), 24 bytes each
//       Each layer gets its OWN NGX feature, so each has its own temporal
//       history. Sharing one feature across layers makes the 2nd pass see a
//       history equal to its own input, which doubles the temporal loop gain
//       and pulses flat areas (measured 3.6x vs 1.7x on the test sequence).
//   -- pipelined (export) --
//   dlssnr2_submit(inBgr, reset)                    -> 1 ok (no wait)
//   dlssnr2_fetch(outRgba, outBgr)                  -> frames left in flight, -1 err
//   dlssnr2_pending()                               -> frames in flight
//   dlssnr2_drain()                                 -> flush everything, 0 ok
//   -- misc --
//   dlssnr2_set_options(style, intensity, localTone, localStruct, skinStruct,
//                       autoMask, uiCorrection)
//   -- experiment only, unused by the GUI; all no-ops in production --
//   dlssnr2_set_aux(mode, shift)       -> overwrite the MVec texture (0=zeros)
//   dlssnr2_aux_test(which, fmt, pattern, strength)
//                                      -> bind BidirDistortionField /
//                                         ControlMask / UI / UIAlpha in a
//                                         chosen format to probe whether the
//                                         feature reads them
//   dlssnr2_get_sizes(&w, &h)
//   dlssnr2_shutdown()
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
#include <immintrin.h>

#include "nvsdk_ngx.h"
#include "nvsdk_ngx_helpers.h"
#include "nvsdk_ngx_helpers_dlssg.h"

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "advapi32.lib")

// ProjectID used by the DLSS NR snippet (same one Magpie uses).
static const char* kProjectId = "7c134ab9-9677-4af5-a2b2-bca943350861";
static const unsigned long long kAppId = 0x0876232Cull;   // snippet Init_Ext app id
// Where nvngx_dlssnr.dll and the NGX runtime live.
//
// Resolved at load time to THIS DLL's own directory instead of a hard-coded
// checkout path, so the project can be moved or cloned anywhere. A host that
// keeps the engine elsewhere can still override it with dlssnr2_set_appdir()
// BEFORE the first dlssnr2_init(); NGX is inited once per process, so a later
// override has no effect.
static wchar_t g_appDir[MAX_PATH] = L"";
static const wchar_t* kAppDir = g_appDir;

// Log destination: this DLL's directory by default. Overridable via
// dlssnr2_init()'s logPath argument.
static wchar_t g_logPath[MAX_PATH] = L"";

// Fills g_appDir / g_logPath from the module handle, once.
static void ResolveOwnDirectory(HMODULE self) {
    if (g_appDir[0]) return;
    wchar_t path[MAX_PATH] = L"";
    if (!self) self = GetModuleHandleW(L"dlssnr_host2.dll");
    if (!self || !GetModuleFileNameW(self, path, _countof(path))) {
        // Last-resort fallback so a failure here is still diagnosable.
        wcsncpy_s(g_appDir, _countof(g_appDir), L".", _TRUNCATE);
    } else {
        wchar_t* slash = wcsrchr(path, L'\\');
        if (slash) *slash = 0;
        wcsncpy_s(g_appDir, _countof(g_appDir), path, _TRUNCATE);
    }
    _snwprintf_s(g_logPath, _countof(g_logPath), _TRUNCATE,
                 L"%s\\host2_log.txt", g_appDir);
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        ResolveOwnDirectory(hModule);
    }
    return TRUE;
}

// ---------------------------------------------------------------------------
// logging
// ---------------------------------------------------------------------------
static void HLog(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt);
    // NOTE: open with a plain L"a" mode string. Adding ", ccs=UTF-8" makes
    // vfprintf on a wide-mode stream crash (learned the hard way).
    FILE* f = nullptr; _wfopen_s(&f, g_logPath, L"a");
    if (f) { vfprintf(f, fmt, ap); fprintf(f, "\n"); fclose(f); }
    va_end(ap);
}

// perf log: only enabled when "host2_perf.on" exists next to this DLL (checked once)
static bool g_perf = false;
static void HPerf(const char* stage, double ms) {
    if (!g_perf) return;
    wchar_t p[MAX_PATH];
    _snwprintf_s(p, _countof(p), _TRUNCATE, L"%s\\host2_perf.txt", g_appDir);
    FILE* f = nullptr; _wfopen_s(&f, p, L"a");
    if (f) { fprintf(f, "%s %.3f\n", stage, ms); fclose(f); }
}
static double NowMs() {
    static LARGE_INTEGER freq{}; static bool init = false;
    if (!init) { QueryPerformanceFrequency(&freq); init = true; }
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1000.0 / (double)freq.QuadPart;
}

// ---------------------------------------------------------------------------
// caller-compatibility IAT hook (the snippet checks its own module name; it must
// believe it was loaded as "nvngx.dll" or Init_Ext refuses to run).
// ---------------------------------------------------------------------------
static HMODULE g_callerModule = nullptr;
static DWORD(WINAPI* g_origGetModuleFileNameW)(HMODULE, LPWSTR, DWORD) = nullptr;

static DWORD WINAPI HookedGetModuleFileNameW(HMODULE module, LPWSTR filename, DWORD size) {
    if (module == g_callerModule) {
        const wchar_t* A = L"nvngx.dll";
        const DWORD L = (DWORD)wcslen(A);
        if (!filename || !size) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return 0; }
        if (size <= L) {
            if (size > 1) memcpy(filename, A, (size - 1) * sizeof(wchar_t));
            filename[size - 1] = L'\0';
            SetLastError(ERROR_INSUFFICIENT_BUFFER);
            return size;
        }
        wcscpy_s(filename, size, A);
        return L + 1;
    }
    if (g_origGetModuleFileNameW) return g_origGetModuleFileNameW(module, filename, size);
    SetLastError(ERROR_INVALID_FUNCTION);
    return 0;
}

static void** FindImportedFunctionSlot(HMODULE module, const char* funcName) {
    auto* base = (uint8_t*)module;
    auto* dos = (IMAGE_DOS_HEADER*)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0) return nullptr;
    auto* nt = (IMAGE_NT_HEADERS64*)(base + dos->e_lfanew);
    auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress || !dir.Size) return nullptr;
    auto* desc = (IMAGE_IMPORT_DESCRIPTOR*)(base + dir.VirtualAddress);
    auto* end = (IMAGE_IMPORT_DESCRIPTOR*)(base + dir.VirtualAddress + dir.Size);
    for (; desc < end && desc->Name; ++desc) {
        const char* libName = (const char*)(base + desc->Name);
        if (_stricmp(libName, "KERNEL32.dll") != 0 &&
            _stricmp(libName, "api-ms-win-core-libraryloader-l1-2-0.dll") != 0 &&
            _stricmp(libName, "api-ms-win-core-libraryloader-l1-1-0.dll") != 0)
            continue;
        if (!desc->OriginalFirstThunk || !desc->FirstThunk) continue;
        auto* nameThunk = (IMAGE_THUNK_DATA64*)(base + desc->OriginalFirstThunk);
        auto* addrThunk = (IMAGE_THUNK_DATA64*)(base + desc->FirstThunk);
        for (; nameThunk->u1.AddressOfData; ++nameThunk, ++addrThunk) {
            if (IMAGE_SNAP_BY_ORDINAL64(nameThunk->u1.Ordinal)) continue;
            auto* imp = (IMAGE_IMPORT_BY_NAME*)(base + nameThunk->u1.AddressOfData);
            if (strcmp(imp->Name, funcName) == 0) return (void**)&addrThunk->u1.Function;
        }
    }
    return nullptr;
}

static bool InstallCallerCompatibility(HMODULE snippetModule) {
    auto slot = FindImportedFunctionSlot(snippetModule, "GetModuleFileNameW");
    if (!slot) { HLog("hook: slot not found"); return false; }
    if (*slot == (void*)&HookedGetModuleFileNameW) return true;   // already hooked
    HMODULE callerModule = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)&HookedGetModuleFileNameW, &callerModule);
    DWORD oldProt = 0;
    if (!VirtualProtect(slot, sizeof(void*), PAGE_READWRITE, &oldProt)) return false;
    g_callerModule = callerModule;
    void* original = InterlockedExchangePointer(slot, (void*)&HookedGetModuleFileNameW);
    memcpy(&g_origGetModuleFileNameW, &original, sizeof(original));
    VirtualProtect(slot, sizeof(void*), oldProt, nullptr);
    FlushInstructionCache(GetCurrentProcess(), slot, sizeof(void*));
    return g_origGetModuleFileNameW != nullptr;
}

// ---------------------------------------------------------------------------
// snippet entry points (resolved from nvngx_dlssnr.dll)
// ---------------------------------------------------------------------------
typedef NVSDK_NGX_Result(NVSDK_CONV* FnInitExt)(
    unsigned long long appId, const wchar_t* appDataPath, ID3D12Device* device,
    NVSDK_NGX_Version sdkVersion, const NVSDK_NGX_Parameter* params);
typedef NVSDK_NGX_Result(NVSDK_CONV* FnCreateFeature)(
    ID3D12GraphicsCommandList* cmdList, NVSDK_NGX_Feature featureId,
    NVSDK_NGX_Parameter* params, NVSDK_NGX_Handle** outHandle);
typedef NVSDK_NGX_Result(NVSDK_CONV* FnEvaluateFeature)(
    ID3D12GraphicsCommandList* cmdList, const NVSDK_NGX_Handle* feature,
    const NVSDK_NGX_Parameter* params, PFN_NVSDK_NGX_ProgressCallback cb);
typedef NVSDK_NGX_Result(NVSDK_CONV* FnReleaseFeature)(NVSDK_NGX_Handle* feature);

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// A "slot" is one complete set of per-frame resources. With two slots the CPU can
// stage/record/submit frame N while the GPU is still finishing frame N-1, instead
// of stalling on a fence every single frame.
// ---------------------------------------------------------------------------
struct Slot {
    ID3D12CommandAllocator* alloc = nullptr;
    ID3D12GraphicsCommandList* cmd = nullptr;
    ID3D12Resource* texIn = nullptr;        // R8G8B8A8_UNORM (colour in)
    ID3D12Resource* texOut = nullptr;       // R8G8B8A8_UNORM (eval output)
    ID3D12Resource* uploadBuf = nullptr;    // BGR -> RGBA staging
    ID3D12Resource* readbackBuf = nullptr;  // RGBA readback
    uint64_t fenceValue = 0;                // fence value signalled for this slot
    bool pending = false;                   // submitted, output not fetched yet
    double submitMs = 0.0;                  // QPC timestamp at submission (perf log)
};

// Number of in-flight frame slots (double buffering by default).
//
// Overridable at compile time purely so variants can be measured:
//     cl ... /DDLSSNR_SLOTS=3 ...
//
// Measured conclusion: 2 slots is already optimal for this workload. Going to 3
// changed throughput by less than 0.5% (within noise) at 720p / 1080p / 1440x1440
// and 2560x1440, and made the tail latency slightly WORSE at two of those sizes.
// The reason is that throughput is max(CPU_per_frame, GPU_per_frame) — deeper
// queues cannot create throughput, they only buffer it. 2 slots keeps the CPU
// stage overlapped with the previous frame's GPU pass, which is all that is
// needed. Each extra slot also costs ~33 MB of VRAM at 1080p (RGBA in + out +
// staging + readback), so the default stays at 2.
#ifndef DLSSNR_SLOTS
#define DLSSNR_SLOTS 2
#endif
static const int kSlots = DLSSNR_SLOTS;

// Maximum number of DLSSNR layers in one dlssnr2_chain() call.
static const int kMaxLayers = 8;

// Public ABI: one layer's parameters. Plain data with no pointers so a caller
// (Python/ctypes) can build an array of these directly. Layout is 4-byte fields
// only, i.e. 24 bytes with no padding.
struct DlssnrLayerOpts {
    int   style;
    float intensity;
    float localTone;
    float localStruct;
    float skinStruct;
    int   autoMask;
};

struct HostState {
    ID3D12Device* device = nullptr;
    ID3D12CommandQueue* queue = nullptr;
    ID3D12Fence* fence = nullptr;
    HANDLE fenceEvent = nullptr;
    uint64_t fenceValue = 0;

    int w = 0, h = 0;

    HMODULE snippet = nullptr;
    FnInitExt fnInitExt = nullptr;
    FnCreateFeature fnCreate = nullptr;
    FnEvaluateFeature fnEval = nullptr;
    FnReleaseFeature fnRelease = nullptr;

    // One NGX feature per layer. A feature owns its temporal history, so each
    // layer needs its own: running the SAME feature twice per frame makes the
    // second pass see a history that equals its own input, which doubles the
    // loop gain of the temporal recursion and pulses flat areas (measured 2-4x
    // the single-pass level instability).
    NVSDK_NGX_Handle* features[kMaxLayers] = {};
    int nFeatures = 0;
    NVSDK_NGX_Parameter* params = nullptr;

    Slot slots[kSlots];
    // read-only helpers shared by every slot
    ID3D12Resource* texZeroMV = nullptr;    // R16G16_FLOAT zero motion
    ID3D12Resource* texZeroDepth = nullptr; // R32_FLOAT zero depth
    // Experimental optional-guidance textures (all null in production).
    ID3D12Resource* texBidir = nullptr;     // DLSSNR.BidirectionalDistortionField
    ID3D12Resource* texCtrl  = nullptr;     // DLSSNR.ControlMask
    ID3D12Resource* texUI    = nullptr;     // DLSSNR.UI
    ID3D12Resource* texUIA   = nullptr;     // DLSSNR.UIAlpha

    int submitIdx = 0;                      // next slot to submit into
    int fetchIdx = 0;                       // oldest slot awaiting fetch
    int pending = 0;                        // frames currently in flight

    int style = 0;
    float intensity = 1.0f, localTone = 1.0f, localStruct = 1.0f, skinStruct = -1.0f;
    int autoMask = 0, uiCorrection = 0;

    bool inited = false;
};
static HostState g;

// Aux-guidance experiment state. 0 = all-zero motion (production behaviour),
// 1 = constant shift, 2 = spatial gradient, 3 = per-pixel random.
static int   g_auxMode  = 0;
static float g_auxShift = 32.0f;
static float g_auxU     = 0.0f;   // mode 1: constant motion, per axis, unclamped
static float g_auxV     = 0.0f;
static float g_mvScaleX = 1.0f;   // production default
static float g_mvScaleY = 1.0f;
static int   g_preset   = 0;      // DLSSNR.Hint.Render.Preset (production: 0)

// ---------------------------------------------------------------------------
// SIMD BGR <-> RGBA
// ---------------------------------------------------------------------------
// BGR(3ch) -> RGBA(4ch), alpha 255.  4 px per step: 12B in -> 16B out.
static void BGR2RGBA_row(const uint8_t* src, uint8_t* dst, int64_t w) {
    const __m128i msk = _mm_setr_epi8(2,1,0,-1, 5,4,3,-1, 8,7,6,-1, 11,10,9,-1);
    const __m128i alpha = _mm_set1_epi32((int)0xFF000000);
    int64_t grp = 0;
    const int64_t maxG = (w * 3 - 16) / 12;   // keep the 16B read inside the row
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

// RGBA(4ch) -> BGR(3ch).  4 px per step: 16B in -> 12B out.
static void RGBA2BGR_row(const uint8_t* src, uint8_t* dst, int64_t w) {
    const __m128i msk = _mm_setr_epi8(2,1,0, 6,5,4, 10,9,8, 14,13,12, 0,0,0,0);
    int64_t grp = 0;
    const int64_t groups = w / 4;
    for (; grp < groups; ++grp) {
        __m128i s = _mm_loadu_si128((const __m128i*)(src + grp * 16));
        __m128i r = _mm_shuffle_epi8(s, msk);
        uint8_t* d = dst + grp * 12;
        _mm_storel_epi64((__m128i*)d, r);
        *(uint32_t*)(d + 8) = (uint32_t)_mm_cvtsi128_si32(_mm_srli_si128(r, 8));
    }
    for (int64_t i = grp * 4; i < w; ++i) {
        dst[i * 3 + 0] = src[i * 4 + 2];
        dst[i * 3 + 1] = src[i * 4 + 1];
        dst[i * 3 + 2] = src[i * 4 + 0];
    }
}

// ---------------------------------------------------------------------------
// D3D12 helpers
// ---------------------------------------------------------------------------
static bool WaitForFence(uint64_t value) {
    if (!g.fence || g.fence->GetCompletedValue() >= value) return true;
    if (FAILED(g.fence->SetEventOnCompletion(value, g.fenceEvent))) return false;
    return WaitForSingleObject(g.fenceEvent, INFINITE) == WAIT_OBJECT_0;
}

static void BarrierOn(ID3D12GraphicsCommandList* cmd, ID3D12Resource* res,
                      D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = res;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = before;
    b.Transition.StateAfter = after;
    cmd->ResourceBarrier(1, &b);
}

static ID3D12Resource* CreateTexture(DXGI_FORMAT fmt, UINT w, UINT h, bool allowUav) {
    D3D12_HEAP_PROPERTIES heap{}; heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = w; desc.Height = h; desc.DepthOrArraySize = 1; desc.MipLevels = 1;
    desc.Format = fmt; desc.SampleDesc.Count = 1;
    desc.Flags = allowUav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource* tex = nullptr;
    if (FAILED(g.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&tex))))
        return nullptr;
    return tex;
}

static ID3D12Resource* CreateBuffer(UINT64 size, D3D12_HEAP_TYPE type) {
    D3D12_HEAP_PROPERTIES heap{}; heap.Type = type;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size; desc.Height = 1; desc.DepthOrArraySize = 1;
    desc.MipLevels = 1; desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    D3D12_RESOURCE_STATES st = (type == D3D12_HEAP_TYPE_UPLOAD)
        ? D3D12_RESOURCE_STATE_GENERIC_READ : D3D12_RESOURCE_STATE_COPY_DEST;
    ID3D12Resource* buf = nullptr;
    if (FAILED(g.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, st,
            nullptr, IID_PPV_ARGS(&buf))))
        return nullptr;
    return buf;
}

// ---------------------------------------------------------------------------
// teardown of GPU resources (keeps the D3D12 device/NGX core alive: re-initing
// the NGX core a second time in one process is unsafe)
// ---------------------------------------------------------------------------
static void ReleaseFeatureAndTextures() {
    // make sure no slot is still in flight before dropping the resources
    if (g.queue && g.fence && g.fenceValue) {
        g.queue->Signal(g.fence, g.fenceValue);
        WaitForFence(g.fenceValue);
    }
    for (int i = 0; i < kMaxLayers; ++i) {
        if (g.features[i]) {
            if (g.fnRelease) g.fnRelease(g.features[i]);
            else NVSDK_NGX_D3D12_ReleaseFeature(g.features[i]);
            g.features[i] = nullptr;
        }
    }
    g.nFeatures = 0;
    if (g.params) { NVSDK_NGX_D3D12_DestroyParameters(g.params); g.params = nullptr; }

    for (int s = 0; s < kSlots; ++s) {
        Slot& sl = g.slots[s];
        ID3D12Resource* res[] = { sl.texIn, sl.texOut, sl.uploadBuf, sl.readbackBuf };
        for (auto*& r : res) { if (r) { r->Release(); r = nullptr; } }
        if (sl.alloc) { sl.alloc->Release(); sl.alloc = nullptr; }
        if (sl.cmd) { sl.cmd->Release(); sl.cmd = nullptr; }
        sl.fenceValue = 0;
        sl.pending = false;
    }
    if (g.texZeroMV) { g.texZeroMV->Release(); g.texZeroMV = nullptr; }
    if (g.texZeroDepth) { g.texZeroDepth->Release(); g.texZeroDepth = nullptr; }
    if (g.texBidir) { g.texBidir->Release(); g.texBidir = nullptr; }
    if (g.texCtrl)  { g.texCtrl->Release();  g.texCtrl  = nullptr; }
    if (g.texUI)    { g.texUI->Release();    g.texUI    = nullptr; }
    if (g.texUIA)   { g.texUIA->Release();   g.texUIA   = nullptr; }
    g.submitIdx = g.fetchIdx = g.pending = 0;
}

// ---------------------------------------------------------------------------
// init / feature creation
// ---------------------------------------------------------------------------
static bool CreateDeviceAndQueue() {
    if (g.device && g.queue) return true;
    IDXGIFactory6* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) { HLog("dxgi factory fail"); return false; }
    IDXGIAdapter1* adapter = nullptr;
    for (UINT i = 0; factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; ++i) {
        DXGI_ADAPTER_DESC d{}; adapter->GetDesc(&d);
        if (d.VendorId == 0x10DE) break;         // NVIDIA
        adapter->Release(); adapter = nullptr;
    }
    if (!adapter) { factory->Release(); HLog("no NVIDIA adapter"); return false; }
    HRESULT hr = D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&g.device));
    adapter->Release(); factory->Release();
    if (FAILED(hr)) { HLog("D3D12CreateDevice fail 0x%08X", (unsigned)hr); return false; }

    D3D12_COMMAND_QUEUE_DESC qd{}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(g.device->CreateCommandQueue(&qd, IID_PPV_ARGS(&g.queue)))) { HLog("queue fail"); return false; }
    if (FAILED(g.device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&g.fence)))) { HLog("fence fail"); return false; }
    g.fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!g.fenceEvent) { HLog("event fail"); return false; }
    return true;
}

static bool CreateFeature(int w, int h) {
    static bool ngxInited = false;

    const wchar_t* paths[] = { kAppDir };
    NVSDK_NGX_FeatureCommonInfo fi{};
    fi.PathListInfo.Path = paths;
    fi.PathListInfo.Length = 1;

    if (!ngxInited) {
        // load the NR snippet
        wchar_t dll[MAX_PATH];
        swprintf_s(dll, L"%s\\nvngx_dlssnr.dll", kAppDir);
        g.snippet = LoadLibraryW(dll);
        if (!g.snippet) { HLog("LoadLibraryW(nvngx_dlssnr.dll) fail err=%u", GetLastError()); return false; }
        g.fnInitExt = (FnInitExt)GetProcAddress(g.snippet, "NVSDK_NGX_D3D12_Init_Ext");
        g.fnCreate  = (FnCreateFeature)GetProcAddress(g.snippet, "NVSDK_NGX_D3D12_CreateFeature");
        g.fnEval    = (FnEvaluateFeature)GetProcAddress(g.snippet, "NVSDK_NGX_D3D12_EvaluateFeature");
        g.fnRelease = (FnReleaseFeature)GetProcAddress(g.snippet, "NVSDK_NGX_D3D12_ReleaseFeature");
        if (!g.fnInitExt || !g.fnCreate || !g.fnEval) { HLog("snippet exports missing"); return false; }

        NVSDK_NGX_Result r = NVSDK_NGX_D3D12_Init_with_ProjectID(
            kProjectId, NVSDK_NGX_ENGINE_TYPE_CUSTOM, "DLSS5Tool-NR",
            kAppDir, g.device, &fi, NVSDK_NGX_Version_API);
        if (r != NVSDK_NGX_Result_Success) { HLog("Init_with_ProjectID fail 0x%08X", (unsigned)r); return false; }

        if (!InstallCallerCompatibility(g.snippet)) { HLog("caller hook fail"); return false; }

        r = g.fnInitExt(kAppId, kAppDir, g.device, NVSDK_NGX_Version_API, nullptr);
        if (r != NVSDK_NGX_Result_Success) { HLog("Init_Ext fail 0x%08X", (unsigned)r); return false; }
        ngxInited = true;
        HLog("NGX core + snippet inited");
    }

    if (NVSDK_NGX_D3D12_GetCapabilityParameters(&g.params) != NVSDK_NGX_Result_Success || !g.params) {
        HLog("GetCapabilityParameters fail"); return false;
    }

    g.params->Set(NVSDK_NGX_Parameter_Width, (uint32_t)w);
    g.params->Set(NVSDK_NGX_Parameter_Height, (uint32_t)h);
    g.params->Set("DLSSNR.Width", (uint32_t)w);
    g.params->Set("DLSSNR.Height", (uint32_t)h);
    g.params->Set("DLSSNR.InputWidth", (uint32_t)w);
    g.params->Set("DLSSNR.InputHeight", (uint32_t)h);
    g.params->Set("DLSSNR.OutputWidth", (uint32_t)w);
    g.params->Set("DLSSNR.OutputHeight", (uint32_t)h);
    g.params->Set("DLSSNR.Upscaling", 0u);
    g.params->Set("DLSSNR.Scale", 1.0f);
    g.params->Set("DLSSNR.ScalingRatio", 1.0f);
    g.params->Set("DLSSNR.Hint.Render.Preset", g_preset);
    g.params->Set("DLSSNR.Style", g.style);
    g.params->Set("DLSSNR.Intensity", g.intensity);
    g.params->Set("DLSSNR.LocalToneStrength", g.localTone);
    g.params->Set("DLSSNR.LocalStructureStrength", g.localStruct);
    g.params->Set("DLSSNR.SkinStructureStrength", g.skinStruct);
    g.params->Set("DLSSNR.UseAutoMask", g.autoMask);
    g.params->Set("DLSSNR.UICorrection", g.uiCorrection);
    g.params->Set(NVSDK_NGX_Parameter_CreationNodeMask, 1u);
    g.params->Set(NVSDK_NGX_Parameter_VisibilityNodeMask, 1u);
    g.params->Set(NVSDK_NGX_Parameter_PerfQualityValue, (int)NVSDK_NGX_PerfQuality_Value_Balanced);

    // create the feature (needs a command list; do it synchronously)
    ID3D12CommandAllocator* a = nullptr; ID3D12GraphicsCommandList* c = nullptr;
    if (FAILED(g.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&a))) ||
        FAILED(g.device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, a, nullptr, IID_PPV_ARGS(&c)))) {
        HLog("create cmd for feature fail"); return false;
    }
    NVSDK_NGX_Handle* handle = nullptr;
    NVSDK_NGX_Result r = g.fnCreate(c, (NVSDK_NGX_Feature)18, g.params, &handle);
    c->Close();
    ID3D12CommandList* lists[] = { c };
    g.queue->ExecuteCommandLists(1, lists);
    uint64_t v = ++g.fenceValue;
    g.queue->Signal(g.fence, v);
    WaitForFence(v);
    a->Release(); c->Release();
    if (r != NVSDK_NGX_Result_Success || !handle) {
        HLog("CreateFeature fail 0x%08X", (unsigned)r); return false;
    }
    g.features[0] = handle;
    if (g.nFeatures < 1) g.nFeatures = 1;
    return true;
}

// Create features up to `n` (the chain wants one per layer). Feature creation
// needs a command list, and NGX's own setup work is submitted through it, so
// each new feature gets its own short-lived list -- this only happens when the
// layer count or the frame size changes.
static bool EnsureFeatures(int n) {
    if (n < 1) n = 1;
    if (n > kMaxLayers) { HLog("EnsureFeatures: %d > %d", n, kMaxLayers); return false; }
    for (int i = 0; i < n; ++i) {
        if (g.features[i]) continue;
        ID3D12CommandAllocator* a = nullptr; ID3D12GraphicsCommandList* c = nullptr;
        if (FAILED(g.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&a))) ||
            FAILED(g.device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, a, nullptr, IID_PPV_ARGS(&c)))) {
            HLog("layer %d: create cmd fail", i); return false;
        }
        NVSDK_NGX_Handle* handle = nullptr;
        NVSDK_NGX_Result r = g.fnCreate(c, (NVSDK_NGX_Feature)18, g.params, &handle);
        c->Close();
        ID3D12CommandList* lists[] = { c };
        g.queue->ExecuteCommandLists(1, lists);
        uint64_t v = ++g.fenceValue;
        g.queue->Signal(g.fence, v);
        WaitForFence(v);
        a->Release(); c->Release();
        if (r != NVSDK_NGX_Result_Success || !handle) {
            HLog("CreateFeature[%d] fail 0x%08X", i, (unsigned)r); return false;
        }
        g.features[i] = handle;
        if (g.nFeatures < i + 1) g.nFeatures = i + 1;
        HLog("layer %d: own NGX feature created", i);
    }
    return true;
}

static bool CreateResources(int w, int h) {
    g.texZeroMV    = CreateTexture(DXGI_FORMAT_R16G16_FLOAT, w, h, true);
    g.texZeroDepth = CreateTexture(DXGI_FORMAT_R32_FLOAT,    w, h, true);
    if (!g.texZeroMV || !g.texZeroDepth) { HLog("shared tex alloc fail"); return false; }

    for (int s = 0; s < kSlots; ++s) {
        Slot& sl = g.slots[s];
        sl.texIn       = CreateTexture(DXGI_FORMAT_R8G8B8A8_UNORM, w, h, true);
        sl.texOut      = CreateTexture(DXGI_FORMAT_R8G8B8A8_UNORM, w, h, true);
        sl.uploadBuf   = CreateBuffer((uint64_t)w * h * 4, D3D12_HEAP_TYPE_UPLOAD);
        sl.readbackBuf = CreateBuffer((uint64_t)w * h * 4, D3D12_HEAP_TYPE_READBACK);
        if (!sl.texIn || !sl.texOut || !sl.uploadBuf || !sl.readbackBuf) {
            HLog("slot %d resource alloc fail", s); return false;
        }
        if (FAILED(g.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&sl.alloc))) ||
            FAILED(g.device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, sl.alloc, nullptr, IID_PPV_ARGS(&sl.cmd)))) {
            HLog("slot %d alloc/cmdlist fail", s); return false;
        }
        sl.cmd->Close();
        sl.pending = false;
        sl.fenceValue = 0;
    }
    g.submitIdx = g.fetchIdx = g.pending = 0;
    return true;
}

// ---------------------------------------------------------------------------
// aux-guidance upload (experiment only): writes half-float motion data into the
// otherwise-never-written MVec texture so we can tell whether the network
// actually consumes the auxiliary guidance inputs at all.
// ---------------------------------------------------------------------------
static uint16_t F2H(float f) {
    uint32_t x; memcpy(&x, &f, sizeof(x));
    uint32_t sign = (x >> 16) & 0x8000u;
    int32_t  exp  = (int32_t)((x >> 23) & 0xFFu) - 127 + 15;
    uint32_t man  = x & 0x7FFFFFu;
    if (exp <= 0)  return (uint16_t)sign;
    if (exp >= 31) return (uint16_t)(sign | 0x7C00u);
    return (uint16_t)(sign | ((uint32_t)exp << 10) | (man >> 13));
}

static bool UploadAuxTex(ID3D12Resource* tex, DXGI_FORMAT fmt, int w, int h,
                         const uint8_t* pixels, UINT rowPitch) {
    if (!tex || !g.device || !g.queue) return false;
    ID3D12Resource* up = CreateBuffer((uint64_t)rowPitch * h, D3D12_HEAP_TYPE_UPLOAD);
    if (!up) return false;
    uint8_t* m = nullptr; D3D12_RANGE noRead{ 0, 0 };
    if (FAILED(up->Map(0, &noRead, (void**)&m))) { up->Release(); return false; }
    memcpy(m, pixels, (size_t)rowPitch * h);
    up->Unmap(0, nullptr);

    ID3D12CommandAllocator* a = nullptr; ID3D12GraphicsCommandList* c = nullptr;
    if (FAILED(g.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&a))) ||
        FAILED(g.device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, a, nullptr, IID_PPV_ARGS(&c)))) {
        up->Release(); return false;
    }
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = tex;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = up;
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    src.PlacedFootprint.Footprint.Width = (UINT)w;
    src.PlacedFootprint.Footprint.Height = (UINT)h;
    src.PlacedFootprint.Footprint.Depth = 1;
    src.PlacedFootprint.Footprint.RowPitch = rowPitch;
    src.PlacedFootprint.Footprint.Format = fmt;
    c->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    // COMMON -> COPY_DEST was an implicit promotion; hand the resource back in
    // COMMON so SubmitCore's COMMON -> NON_PIXEL_SHADER_RESOURCE barrier holds.
    BarrierOn(c, tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    c->Close();
    g.queue->ExecuteCommandLists(1, (ID3D12CommandList**)&c);
    uint64_t v = ++g.fenceValue;
    g.queue->Signal(g.fence, v);
    WaitForFence(v);
    c->Release(); a->Release(); up->Release();
    return true;
}

// Builds the motion payload for the current mode and uploads it.
static int FillMVec() {
    if (!g.inited || !g.texZeroMV) return 0;
    const int W = g.w, H = g.h;
    const UINT rowPitch = (UINT)(((W * 4) + 255) & ~255u);
    uint8_t* buf = (uint8_t*)calloc((size_t)rowPitch * H, 1);
    if (!buf) return 0;
    const float S = g_auxShift;
    for (int y = 0; y < H; ++y) {
        uint16_t* row = (uint16_t*)(buf + (size_t)y * rowPitch);
        for (int x = 0; x < W; ++x) {
            float u = 0.0f, v = 0.0f;
            switch (g_auxMode) {
                case 1: u = g_auxU; v = g_auxV; break;
                case 2: u = S * (float)x / (float)W; v = S * (float)y / (float)H; break;
                case 3: u = S * ((float)rand() / (float)RAND_MAX * 2.0f - 1.0f);
                        v = S * ((float)rand() / (float)RAND_MAX * 2.0f - 1.0f); break;
                default: break;
            }
            row[x * 2 + 0] = F2H(u);
            row[x * 2 + 1] = F2H(v);
        }
    }
    bool ok = UploadAuxTex(g.texZeroMV, DXGI_FORMAT_R16G16_FLOAT, W, H, buf, rowPitch);
    free(buf);
    return ok ? 1 : 0;
}

// ---------------------------------------------------------------------------
// generic optional-guidance probe (EXPERIMENT ONLY): builds a texture in a
// chosen format, fills it with a chosen pattern, and binds it as one of the
// auxiliary DLSSNR inputs so we can measure whether the feature reads it.
// ---------------------------------------------------------------------------
static const DXGI_FORMAT kAuxFmts[] = {
    DXGI_FORMAT_R16G16_FLOAT, DXGI_FORMAT_R16G16B16A16_FLOAT,
    DXGI_FORMAT_R32G32_FLOAT, DXGI_FORMAT_R32_FLOAT,
    DXGI_FORMAT_R8_UNORM,     DXGI_FORMAT_R8G8B8A8_UNORM,
    DXGI_FORMAT_R16_FLOAT,    DXGI_FORMAT_R8G8_UNORM,
};
static const char* kAuxFmtNames[] = {
    "R16G16_FLOAT", "R16G16B16A16_FLOAT", "R32G32_FLOAT", "R32_FLOAT",
    "R8_UNORM",     "R8G8B8A8_UNORM",     "R16_FLOAT",    "R8G8_UNORM",
};
static const int kAuxFmtCount = 8;

static int AuxBpp(DXGI_FORMAT f) {
    switch (f) {
        case DXGI_FORMAT_R16G16_FLOAT:       return 4;
        case DXGI_FORMAT_R16G16B16A16_FLOAT: return 8;
        case DXGI_FORMAT_R32G32_FLOAT:       return 8;
        case DXGI_FORMAT_R32_FLOAT:          return 4;
        case DXGI_FORMAT_R8_UNORM:           return 1;
        case DXGI_FORMAT_R8G8B8A8_UNORM:     return 4;
        case DXGI_FORMAT_R16_FLOAT:          return 2;
        case DXGI_FORMAT_R8G8_UNORM:         return 2;
        default: return 0;
    }
}

static void PutB(uint8_t* p, float f) {
    float c = f < 0.0f ? 0.0f : (f > 1.0f ? 1.0f : f);
    *p = (uint8_t)(c * 255.0f + 0.5f);
}

// pattern: 0 = all zeros, 1 = all ones, 2 = spatial gradient, 3 = random
static bool BuildAuxPattern(DXGI_FORMAT fmt, int pattern, float S, int w, int h,
                            uint8_t** outBuf, UINT* outPitch) {
    const int bpp = AuxBpp(fmt);
    if (bpp <= 0) return false;
    const UINT pitch = (UINT)(((size_t)w * bpp + 255) & ~(size_t)255);
    uint8_t* buf = (uint8_t*)calloc((size_t)pitch * h, 1);
    if (!buf) return false;
    for (int y = 0; y < h; ++y) {
        uint8_t* row = buf + (size_t)y * pitch;
        for (int x = 0; x < w; ++x) {
            float a = 0.0f, b = 0.0f, c = 0.0f;
            switch (pattern) {
                case 1: a = b = c = 1.0f; break;
                case 4: a = b = c = (S < 0.0f ? 0.0f : (S > 1.0f ? 1.0f : S)); break;
                case 5: a = b = c = S; break;   // raw constant, unclamped (float formats)
                case 2: a = S * (float)x / (float)w; b = S * (float)y / (float)h;
                        c = (float)x / (float)w; break;
                case 3: a = S * ((float)rand() / (float)RAND_MAX * 2.0f - 1.0f);
                        b = S * ((float)rand() / (float)RAND_MAX * 2.0f - 1.0f);
                        c = (float)rand() / (float)RAND_MAX; break;
                default: break;
            }
            uint8_t* p = row + (size_t)x * bpp;
            switch (fmt) {
                case DXGI_FORMAT_R16G16_FLOAT:
                    *(uint16_t*)(p + 0) = F2H(a); *(uint16_t*)(p + 2) = F2H(b); break;
                case DXGI_FORMAT_R16G16B16A16_FLOAT:
                    *(uint16_t*)(p + 0) = F2H(a); *(uint16_t*)(p + 2) = F2H(b);
                    *(uint16_t*)(p + 4) = F2H(a); *(uint16_t*)(p + 6) = F2H(b); break;
                case DXGI_FORMAT_R32G32_FLOAT:
                    *(float*)(p + 0) = a; *(float*)(p + 4) = b; break;
                case DXGI_FORMAT_R32_FLOAT:  *(float*)p = c; break;
                case DXGI_FORMAT_R16_FLOAT:  *(uint16_t*)p = F2H(c); break;
                case DXGI_FORMAT_R8_UNORM:   PutB(p, c); break;
                case DXGI_FORMAT_R8G8_UNORM: PutB(p, c); PutB(p + 1, c); break;
                case DXGI_FORMAT_R8G8B8A8_UNORM:
                    PutB(p, c); PutB(p + 1, c); PutB(p + 2, c); PutB(p + 3, 1.0f); break;
                default: break;
            }
        }
    }
    *outBuf = buf; *outPitch = pitch;
    return true;
}

static void BindAuxTex(ID3D12GraphicsCommandList* cmd, const char* name,
                       ID3D12Resource* tex, int W, int H) {
    BarrierOn(cmd, tex, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    char buf[128];
    g.params->Set(name, tex);
    sprintf_s(buf, "%sSubrectBaseX", name);  g.params->Set(buf, 0u);
    sprintf_s(buf, "%sSubrectBaseY", name);  g.params->Set(buf, 0u);
    sprintf_s(buf, "%sSubrectWidth", name);  g.params->Set(buf, (uint32_t)W);
    sprintf_s(buf, "%sSubrectHeight", name); g.params->Set(buf, (uint32_t)H);
}

// ---------------------------------------------------------------------------
// exports
// ---------------------------------------------------------------------------
extern "C" {

__declspec(dllexport) void dlssnr2_set_appdir(const wchar_t* dir) {
    // Must be called BEFORE the first dlssnr2_init(): NGX is inited once per
    // process and bakes this path in. Ignored if a directory is already live.
    if (dir && dir[0] && !g.inited) wcsncpy_s(g_appDir, _countof(g_appDir), dir, _TRUNCATE);
}

__declspec(dllexport) int dlssnr2_init(int w, int h, const wchar_t* logPath) {
    { wchar_t p[MAX_PATH]; _snwprintf_s(p, _countof(p), _TRUNCATE, L"%s\\host2_perf.on", g_appDir);
      FILE* f = nullptr; _wfopen_s(&f, p, L"rb"); if (f) { g_perf = true; fclose(f); } }
    if (logPath && logPath[0])
        wcsncpy_s(g_logPath, _countof(g_logPath), logPath, _TRUNCATE);
    HLog("=== init %dx%d appdir=%ls ===", w, h, g_appDir);
    if (g.inited && g.w == w && g.h == h) return 1;

    if (!CreateDeviceAndQueue()) return 0;
    ReleaseFeatureAndTextures();               // drop the previous size (if any)

    g.w = w; g.h = h;
    if (!CreateFeature(w, h)) { HLog("CreateFeature stage fail"); return 0; }
    if (!CreateResources(w, h)) { HLog("CreateResources stage fail"); return 0; }

    g.inited = true;
    HLog("init OK %dx%d", w, h);
    return 1;
}

__declspec(dllexport) void dlssnr2_set_options(int style, float intensity, float localTone,
        float localStruct, float skinStruct, int autoMask, int uiCorrection) {
    g.style = style; g.intensity = intensity;
    g.localTone = localTone; g.localStruct = localStruct;
    g.skinStruct = skinStruct; g.autoMask = autoMask; g.uiCorrection = uiCorrection;
    if (g.params) {
        g.params->Set("DLSSNR.Style", style);
        g.params->Set("DLSSNR.Intensity", intensity);
        g.params->Set("DLSSNR.LocalToneStrength", localTone);
        g.params->Set("DLSSNR.LocalStructureStrength", localStruct);
        g.params->Set("DLSSNR.SkinStructureStrength", skinStruct);
        g.params->Set("DLSSNR.UseAutoMask", autoMask);
        g.params->Set("DLSSNR.UICorrection", uiCorrection);
    }
}

// EXPERIMENT ONLY. Injects a non-zero motion field so we can determine whether
// this DLL build consumes the auxiliary guidance inputs (MVec/Depth) at all.
//   mode 0 = all zeros (production), 1 = const shift, 2 = gradient, 3 = random
__declspec(dllexport) int dlssnr2_set_aux(int mode, float shift) {
    g_auxMode = mode; g_auxShift = shift;
    return FillMVec();
}

// EXPERIMENT ONLY. Fills the whole MVec texture with one constant (u, v).
// Values are written verbatim (not clamped), so callers can probe whether the
// feature wants pixel units, normalised UV, or something else.
__declspec(dllexport) int dlssnr2_mvec_const(float u, float v) {
    g_auxMode = 1; g_auxU = u; g_auxV = v;
    return FillMVec();
}

// EXPERIMENT ONLY. Overrides DLSSNR.Hint.Render.Preset (production: 0).
// Must be called BEFORE dlssnr2_init -- the preset selects the weight set at
// feature-creation time. The DLL ships several (WEIGHTS_HT, CC_SILVER_AARDWOLD)
// and logs a fallback when the requested one is absent from this build.
__declspec(dllexport) void dlssnr2_set_preset(int p) {
    g_preset = p;
}

// EXPERIMENT ONLY. Overrides DLSSNR.MVecScaleX/Y (production: 1.0).
__declspec(dllexport) void dlssnr2_set_mvec_scale(float sx, float sy) {
    g_mvScaleX = sx; g_mvScaleY = sy;
}

// EXPERIMENT ONLY. which: 0=unbind, 1=BidirectionalDistortionField,
// 2=ControlMask, 3=UI, 4=UIAlpha. fmtSel indexes kAuxFmts.
__declspec(dllexport) int dlssnr2_aux_test(int which, int fmtSel, int pattern, float strength) {
    if (!g.inited) return 0;
    if (fmtSel < 0 || fmtSel >= kAuxFmtCount) return 0;
    ID3D12Resource** slotPtr = nullptr;
    switch (which) {
        case 1: slotPtr = &g.texBidir; break;
        case 2: slotPtr = &g.texCtrl;  break;
        case 3: slotPtr = &g.texUI;    break;
        case 4: slotPtr = &g.texUIA;   break;
        default: return 0;
    }
    const DXGI_FORMAT fmt = kAuxFmts[fmtSel];
    uint8_t* buf = nullptr; UINT pitch = 0;
    if (!BuildAuxPattern(fmt, pattern, strength, g.w, g.h, &buf, &pitch)) return 0;
    ID3D12Resource* tex = CreateTexture(fmt, g.w, g.h, true);
    if (!tex) { free(buf); return 0; }
    bool ok = UploadAuxTex(tex, fmt, g.w, g.h, buf, pitch);
    free(buf);
    if (!ok) { tex->Release(); return 0; }
    if (*slotPtr) (*slotPtr)->Release();
    *slotPtr = tex;
    return 1;
}

// ===========================================================================
// Pipelined core: SubmitCore() records+submits a frame into a slot and returns
// immediately; FetchCore() waits for the OLDEST slot and hands its pixels back.
// Alternating slots means the CPU stages frame N while the GPU still works on
// frame N-1, so the per-frame CPU work hides under the GPU pass instead of
// adding to it.
//
// Steady-state throughput is max(CPU_per_frame, GPU_per_frame) and does NOT
// improve with more slots once the pipeline is deep enough (2 is enough here).
// Measured: 2 vs 3 slots gave identical throughput (97 fps @1080p) at every
// resolution tested, and stayed identical even when the CPU stage was
// artificially inflated to 12 ms vs a 7 ms GPU pass — i.e. this holds for both
// GPU-bound and CPU-bound pipelines. Slots only bound how many frames may be
// queued at once; they do not create throughput.
// ===========================================================================
// ---------------------------------------------------------------------------
// Per-evaluate parameter binding, shared by the single-pass path and the chain.
// Callers differ only in which textures go in and out.
// ---------------------------------------------------------------------------
static void BindEvalParams(ID3D12GraphicsCommandList* cmd, ID3D12Resource* in,
                           ID3D12Resource* out, int reset) {
    const int W = g.w, H = g.h;
    BarrierOn(cmd, g.texZeroMV, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    BarrierOn(cmd, g.texZeroDepth, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);

    g.params->Set("DLSSNR.Color", in);
    g.params->Set("DLSSNR.Output", out);
    g.params->Set("DLSSNR.MVec", g.texZeroMV);
    g.params->Set("DLSSNR.Depth", g.texZeroDepth);
    g.params->Set("DLSSNR.Reset", reset ? 1 : 0);
    g.params->Set("DLSSNR.ColorSubrectBaseX", 0u);
    g.params->Set("DLSSNR.ColorSubrectBaseY", 0u);
    g.params->Set("DLSSNR.ColorSubrectWidth", (uint32_t)W);
    g.params->Set("DLSSNR.ColorSubrectHeight", (uint32_t)H);
    g.params->Set("DLSSNR.OutputSubrectBaseX", 0u);
    g.params->Set("DLSSNR.OutputSubrectBaseY", 0u);
    g.params->Set("DLSSNR.OutputSubrectWidth", (uint32_t)W);
    g.params->Set("DLSSNR.OutputSubrectHeight", (uint32_t)H);
    g.params->Set("DLSSNR.MVecSubrectBaseX", 0u);
    g.params->Set("DLSSNR.MVecSubrectBaseY", 0u);
    g.params->Set("DLSSNR.MVecSubrectWidth", (uint32_t)W);
    g.params->Set("DLSSNR.MVecSubrectHeight", (uint32_t)H);
    g.params->Set("DLSSNR.DepthSubrectBaseX", 0u);
    g.params->Set("DLSSNR.DepthSubrectBaseY", 0u);
    g.params->Set("DLSSNR.DepthSubrectWidth", (uint32_t)W);
    g.params->Set("DLSSNR.DepthSubrectHeight", (uint32_t)H);
    g.params->Set("DLSSNR.MVecScaleX", g_mvScaleX);
    g.params->Set("DLSSNR.MVecScaleY", g_mvScaleY);
    g.params->Set("DLSSNR.DepthInverted", 1);
    g.params->Set("DLSSNR.Enabled", 1);

    // experiment-only optional guidance inputs (all null in production)
    if (g.texBidir) BindAuxTex(cmd, "DLSSNR.BidirectionalDistortionField", g.texBidir, W, H);
    if (g.texCtrl)  BindAuxTex(cmd, "DLSSNR.ControlMask",  g.texCtrl,  W, H);
    if (g.texUI)    BindAuxTex(cmd, "DLSSNR.UI",           g.texUI,    W, H);
    if (g.texUIA)   BindAuxTex(cmd, "DLSSNR.UIAlpha",      g.texUIA,   W, H);
}

// Undo the shared-texture binds above so the next frame sees them in COMMON.
static void UnbindEvalParams(ID3D12GraphicsCommandList* cmd) {
    BarrierOn(cmd, g.texZeroMV, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    BarrierOn(cmd, g.texZeroDepth, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    if (g.texBidir) BarrierOn(cmd, g.texBidir, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    if (g.texCtrl)  BarrierOn(cmd, g.texCtrl,  D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    if (g.texUI)    BarrierOn(cmd, g.texUI,    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    if (g.texUIA)   BarrierOn(cmd, g.texUIA,   D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
}

// One layer's own settings onto the shared parameter object. The textures and
// everything else stay as BindEvalParams left them.
static void SetLayerParams(const DlssnrLayerOpts& L) {
    g.params->Set("DLSSNR.Style", L.style);
    g.params->Set("DLSSNR.Intensity", L.intensity);
    g.params->Set("DLSSNR.LocalToneStrength", L.localTone);
    g.params->Set("DLSSNR.LocalStructureStrength", L.localStruct);
    g.params->Set("DLSSNR.SkinStructureStrength", L.skinStruct);
    g.params->Set("DLSSNR.UseAutoMask", L.autoMask);
}

// upload buffer -> texIn, leaving texIn readable as a shader resource
static void UploadToTexIn(Slot& sl) {
    const int W = g.w, H = g.h;
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = sl.texIn;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = sl.uploadBuf;
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    src.PlacedFootprint.Footprint.Width = W;
    src.PlacedFootprint.Footprint.Height = H;
    src.PlacedFootprint.Footprint.Depth = 1;
    src.PlacedFootprint.Footprint.RowPitch = W * 4;
    src.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    BarrierOn(sl.cmd, sl.texIn, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
    sl.cmd->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    BarrierOn(sl.cmd, sl.texIn, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
}

// eval result texture -> this slot's readback heap
static void TexToReadback(Slot& sl, ID3D12Resource* tex) {
    const int W = g.w, H = g.h;
    BarrierOn(sl.cmd, tex, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION rdst{}; rdst.pResource = sl.readbackBuf;
    rdst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    rdst.PlacedFootprint.Footprint.Width = W;
    rdst.PlacedFootprint.Footprint.Height = H;
    rdst.PlacedFootprint.Footprint.Depth = 1;
    rdst.PlacedFootprint.Footprint.RowPitch = W * 4;
    rdst.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    D3D12_TEXTURE_COPY_LOCATION rsrc{}; rsrc.pResource = tex;
    rsrc.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; rsrc.SubresourceIndex = 0;
    sl.cmd->CopyTextureRegion(&rdst, 0, 0, 0, &rsrc, nullptr);
}

// readback heap -> caller's buffer (RGBA memcpy, or the SIMD RGBA->BGR swizzle)
static bool ReadbackTo(Slot& sl, uint8_t* outBgr, uint8_t* outRgba) {
    const int W = g.w, H = g.h;
    const size_t planePx = (size_t)W * H;
    uint8_t* rb = nullptr;
    D3D12_RANGE readRange{ 0, planePx * 4 };
    if (FAILED(sl.readbackBuf->Map(0, &readRange, (void**)&rb))) {
        HLog("readback map fail"); return false;
    }
    if (outRgba) {
        memcpy(outRgba, rb, planePx * 4);
    } else if (outBgr) {
        for (int y = 0; y < H; ++y)
            RGBA2BGR_row(rb + (size_t)y * W * 4, outBgr + (size_t)y * W * 3, W);
    }
    sl.readbackBuf->Unmap(0, nullptr);
    return true;
}

static int SubmitCore(const uint8_t* inBgr, int reset) {
    if (!g.inited || !g.features[0]) { HLog("submit: not inited"); return 0; }
    if (g.pending >= kSlots) { HLog("submit: pipeline full"); return 0; }

    Slot& sl = g.slots[g.submitIdx];
    const double t0 = NowMs();
    const int W = g.w, H = g.h;

    // ---- 1. CPU: BGR -> RGBA into this slot's staging buffer ----
    uint8_t* mapped = nullptr;
    D3D12_RANGE noRead{ 0, 0 };
    if (FAILED(sl.uploadBuf->Map(0, &noRead, (void**)&mapped))) { HLog("upload map fail"); return 0; }
    for (int y = 0; y < H; ++y)
        BGR2RGBA_row(inBgr + (size_t)y * W * 3, mapped + (size_t)y * W * 4, W);
    sl.uploadBuf->Unmap(0, nullptr);
    const double t1 = NowMs(); HPerf("convIn", t1 - t0);

    // ---- 2. record: upload -> eval -> readback in ONE command list ----
    // Safe to reset: this slot's previous submission was already fetched.
    if (FAILED(sl.alloc->Reset()) || FAILED(sl.cmd->Reset(sl.alloc, nullptr))) {
        HLog("cmd reset fail"); return 0;
    }

    UploadToTexIn(sl);
    BarrierOn(sl.cmd, sl.texOut, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    BindEvalParams(sl.cmd, sl.texIn, sl.texOut, reset);

    NVSDK_NGX_Result r = g.fnEval(sl.cmd, g.features[0], g.params, nullptr);
    if (r != NVSDK_NGX_Result_Success) {
        HLog("EvaluateFeature fail 0x%08X", (unsigned)r);
        sl.cmd->Close();
        return 0;
    }
    const double t2 = NowMs(); HPerf("eval", t2 - t1);

    TexToReadback(sl, sl.texOut);

    // back to COMMON so the slot is ready for its next submission
    BarrierOn(sl.cmd, sl.texIn, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    BarrierOn(sl.cmd, sl.texOut, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    UnbindEvalParams(sl.cmd);
    sl.cmd->Close();

    ID3D12CommandList* lists[] = { sl.cmd };
    g.queue->ExecuteCommandLists(1, lists);
    const uint64_t v = ++g.fenceValue;
    g.queue->Signal(g.fence, v);          // NOTE: no wait here — that is the whole point

    sl.fenceValue = v;
    sl.pending = true;
    sl.submitMs = NowMs();
    g.submitIdx = (g.submitIdx + 1) % kSlots;
    ++g.pending;
    return 1;
}

// Waits for the oldest in-flight slot, copies its pixels to the caller's buffer,
// and frees the slot. Returns the number of frames still in flight (>=0), or -1.
static int FetchCore(uint8_t* outBgr, uint8_t* outRgba) {
    if (g.pending <= 0) return g.pending;
    Slot& sl = g.slots[g.fetchIdx];
    if (!sl.pending) { HLog("fetch: slot not pending (state bug)"); return -1; }

    if (!WaitForFence(sl.fenceValue)) { HLog("fence wait fail"); return -1; }
    if (sl.submitMs > 0.0) HPerf("gpu", NowMs() - sl.submitMs);  // blocked time

    if (!ReadbackTo(sl, outBgr, outRgba)) return -1;

    sl.pending = false;
    sl.fenceValue = 0;
    g.fetchIdx = (g.fetchIdx + 1) % kSlots;
    --g.pending;
    return g.pending;
}

// Drain everything (used to keep the synchronous wrappers honest).
static int DrainCore() {
    while (g.pending > 0) {
        int left = FetchCore(nullptr, nullptr);
        if (left < 0) return -1;
    }
    return 0;
}

// ---------------------------------------------------------------------------
// Multi-layer chain: N parameter sets per frame, each on its OWN NGX feature.
//
// Why one feature per layer instead of N passes on one: a feature owns its
// temporal history. Running the same feature twice per frame means the second
// pass sees a history that is bit-identical to its own input -- a degenerate
// (input, history) pair, since this build feeds zero motion vectors and the
// network therefore reads "nothing moved, trust the history completely". That
// doubles the loop gain of the temporal recursion, and a nonlinear recursion
// with doubled gain oscillates at the frame rate. Measured on a 70%-flat test
// sequence: the stage-mean level instability of the flat area goes from 0.091
// (1 layer) to 0.223 (2 layers on one feature) for a static input, and 0.139 ->
// 0.531 with slow motion inside the flat area -- i.e. 2-4x, worst exactly where
// the eye notices it. Independent features restore a proper pair per layer.
//
// Bonus: the whole chain goes into ONE command list, so a frame costs one
// upload, one fence wait and one readback instead of N of each.
//
//   layers[i]  : per-layer parameters (see DlssnrLayerOpts)
//   nLayers    : 1..kMaxLayers
//   reset      : passed to every layer (frame 0 / scene cut)
// Returns 1 on success, 0 on failure (caller falls back to the N-pass path).
// ---------------------------------------------------------------------------
__declspec(dllexport) int dlssnr2_chain(const unsigned char* inBgr,
                                        unsigned char* outBgr,
                                        unsigned char* outRgba,
                                        const DlssnrLayerOpts* layers,
                                        int nLayers, int reset) {
    if (!g.inited || !g.features[0] || !layers || !inBgr ||
        (!outBgr && !outRgba)) {
        HLog("chain: bad arguments"); return 0;
    }
    if (nLayers < 1 || nLayers > kMaxLayers) {
        HLog("chain: layer count %d out of range", nLayers); return 0;
    }
    if (g.pending >= kSlots) { HLog("chain: pipeline full"); return 0; }
    if (!EnsureFeatures(nLayers)) { HLog("chain: feature setup fail"); return 0; }

    Slot& sl = g.slots[g.submitIdx];
    const int W = g.w, H = g.h;
    const double t0 = NowMs();

    // ---- 1. CPU: BGR -> RGBA into this slot's staging buffer (once) ----
    uint8_t* mapped = nullptr;
    D3D12_RANGE noRead{ 0, 0 };
    if (FAILED(sl.uploadBuf->Map(0, &noRead, (void**)&mapped))) {
        HLog("chain upload map fail"); return 0;
    }
    for (int y = 0; y < H; ++y)
        BGR2RGBA_row(inBgr + (size_t)y * W * 3, mapped + (size_t)y * W * 4, W);
    sl.uploadBuf->Unmap(0, nullptr);
    const double t1 = NowMs(); HPerf("convIn", t1 - t0);

    if (FAILED(sl.alloc->Reset()) || FAILED(sl.cmd->Reset(sl.alloc, nullptr))) {
        HLog("chain cmd reset fail"); return 0;
    }

    // ---- 2. record upload + every layer into one command list ----
    UploadToTexIn(sl);

    // texIn / texOut ping-pong; both start and end in COMMON.
    ID3D12Resource* src = sl.texIn;
    ID3D12Resource* dst = sl.texOut;
    for (int k = 0; k < nLayers; ++k) {
        BarrierOn(sl.cmd, src,
                  (k == 0) ? D3D12_RESOURCE_STATE_COPY_DEST
                           : D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                  D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        BarrierOn(sl.cmd, dst,
                  (k == 0) ? D3D12_RESOURCE_STATE_COMMON
                           : D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        BindEvalParams(sl.cmd, src, dst, reset);
        SetLayerParams(layers[k]);
        NVSDK_NGX_Result r = g.fnEval(sl.cmd, g.features[k], g.params, nullptr);
        if (r != NVSDK_NGX_Result_Success) {
            HLog("chain: layer %d EvaluateFeature fail 0x%08X", k, (unsigned)r);
            sl.cmd->Close();
            return 0;
        }
        ID3D12Resource* t = src; src = dst; dst = t;   // next layer reads this one
    }
    const double t2 = NowMs(); HPerf("eval", t2 - t1);

    // `src` now holds the last layer's output; `dst` is the stale input texture.
    TexToReadback(sl, src);
    BarrierOn(sl.cmd, src, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    BarrierOn(sl.cmd, dst, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
              D3D12_RESOURCE_STATE_COMMON);
    UnbindEvalParams(sl.cmd);
    sl.cmd->Close();

    // ---- 3. one submit, one wait, one readback ----
    ID3D12CommandList* lists[] = { sl.cmd };
    g.queue->ExecuteCommandLists(1, lists);
    const uint64_t v = ++g.fenceValue;
    g.queue->Signal(g.fence, v);
    if (!WaitForFence(v)) { HLog("chain fence wait fail"); return 0; }
    const double t3 = NowMs(); HPerf("gpu", t3 - t2);

    if (!ReadbackTo(sl, outBgr, outRgba)) return 0;
    const double t4 = NowMs(); HPerf("convOut", t4 - t3);
    return 1;
}

// ---- synchronous wrappers (preview path: one frame in, one frame out) ----
static int ProcessCore(const uint8_t* inBgr, uint8_t* outBgr, uint8_t* outRgba, int reset) {
    if (DrainCore() < 0) return 0;
    if (!SubmitCore(inBgr, reset)) return 0;
    const int left = FetchCore(outBgr, outRgba);
    return left >= 0 ? 1 : 0;
}

// ---- pipelined API (export path) ----
//   submit(bgr, reset) -> 1 on success
//   fetch(out)         -> frames still in flight after this fetch (0 = drained), -1 = error
//   Callers submit then fetch the PREVIOUS frame's result, so the CPU never waits
//   for the frame it just queued. Finish with dlssnr2_drain().
__declspec(dllexport) int dlssnr2_submit(unsigned char* inBgr, int reset) {
    return SubmitCore(inBgr, reset);
}

__declspec(dllexport) int dlssnr2_fetch(unsigned char* outRgba, unsigned char* outBgr) {
    return FetchCore(outBgr, outRgba);
}

__declspec(dllexport) int dlssnr2_pending() {
    return g.pending;
}

__declspec(dllexport) int dlssnr2_drain() {
    return DrainCore();
}

__declspec(dllexport) int dlssnr2_process(unsigned char* inBgr, unsigned char* outBgr, int reset) {
    return ProcessCore(inBgr, outBgr, nullptr, reset);
}

__declspec(dllexport) int dlssnr2_process_rgba(unsigned char* inBgr, unsigned char* outRgba, int reset) {
    return ProcessCore(inBgr, nullptr, outRgba, reset);
}

__declspec(dllexport) void dlssnr2_get_sizes(int* outW, int* outH) {
    if (outW) *outW = g.w;
    if (outH) *outH = g.h;
}

__declspec(dllexport) void dlssnr2_shutdown() {
    // Deliberately does NOT tear down the NGX core / D3D12 device: re-initing the
    // NGX core in the same process is unsafe, and the OS reclaims everything at
    // process exit. Size changes go through dlssnr2_init() again, which reuses the
    // device and only rebuilds the feature + slots.
}

}  // extern "C"
