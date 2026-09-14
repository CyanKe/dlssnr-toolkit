// vfx_host.cpp — RTX Video SuperRes family host (VSR / Denoise / Deblur / HighBitrate).
//
// Copyright (c) 2026 Cyanke. MIT License — see LICENSE at the repository root.
// Original work: not derived from purkatyy/DLSS5- and has no counterpart
// upstream. It requires the NVIDIA RTX Video SDK to build and the runtime DLLs
// to run; none of those are distributed with this repository (user-supplied).
//
// All four "features" are the SAME NVIDIA effect ("VideoSuperRes"), selected by
// its QualityLevel:
//     0        VSR_Bicubic
//     1..4     VSR_Low / Medium / High / Ultra          <- AI upscaling (changes resolution)
//     8..11    Denoise_Low / Medium / High / Ultra      <- same resolution
//     12..15   Deblur_Low / Medium / High / Ultra       <- same resolution
//     16..19   HighBitrate_Low / Medium / High / Ultra  <- same resolution
// (Deblur and HighBitrate exist in the binary but Magpie never exposes them;
//  established by enumerating nvVFXVideoSuperRes.dll's own parameter docs.)
//
// SESSIONS, NOT A SINGLETON. Because one effect instance can only hold one
// QualityLevel, running two of these over the same frame (e.g. upscale, then
// sharpen) needs two live instances. vfx_create() hands back a session id and
// every other call takes it, so several can coexist without fighting over one
// cache slot. That also means no model reload per frame.
//
// WHY A SEPARATE DLL FROM dlssnr_host2:
//   This path goes through the Video Effects SDK (NvVFX + NvCVImage + CUDA),
//   not NGX Direct3D12. Keeping it isolated means the DLSSNR path is untouched
//   and either one can fail without taking the other down.
//
// NO IMPORT LIBRARY: NVVideoEffects.dll / NVCVImage.dll are loaded with
// LoadLibrary + GetProcAddress. The plugin itself is found via
// NV_VIDEO_EFFECTS_PATH=USE_APP_PATH (the documented mechanism, taken from
// Magpie's RTXVideoDenoiser.cpp) plus an explicit SetDllDirectory.
//
// THE NvCVImage STRUCT IS INTENTIONALLY OPAQUE. We never read a field:
// NvCVImage_Init/Alloc fill it in, and we already know the pixel pointers
// because we hand them in. That removes any chance of guessing an offset wrong.
//
// Exports:
//   vfx_create(w, h, outW, outH, quality, logPath) -> session id (>=1) or 0
//   vfx_process_rgba(sid, inBgr, outRgba)          -> 1 ok
//   vfx_process_bgr(sid, inBgr, outBgr)            -> 1 ok
//   vfx_destroy(sid)
//   vfx_shutdown()                                  -> destroys every session
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdint.h>
#include <immintrin.h>

// ---------------------------------------------------------------------------
// logging
// ---------------------------------------------------------------------------
static wchar_t g_logPath[MAX_PATH] = L"";
static int     g_perf = 0;

static void HLog(const char* fmt, ...) {
    if (!g_logPath[0]) return;
    FILE* f = nullptr;
    // Plain "a": opening with ccs=UTF-8 turns this into a wide-char stream,
    // which is undefined behaviour for the narrow vfprintf below (it crashes).
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

// ---------------------------------------------------------------------------
// ABI declarations (Video Effects SDK + NvCVImage)
//
// Enum values are the ones this runtime actually accepts, established
// empirically and cross-checked against the official nvCVImage.h ordering
// (Y=1, A=2, YA=3, RGB=4, BGR=5, RGBA=6, BGRA=7, ARGB=8, ABGR=9, YUV...=10+;
//  U8=1, U16=2, S16=3, F16=4, S32=5, F32=6). The status codes match the
// official NvCV_Status enum exactly.
// ---------------------------------------------------------------------------
typedef int NvCV_Status;
typedef void* NvVFX_Handle;
typedef void* CUstream;

#define NVCV_SUCCESS             0
#define NVCV_ERR_PIXELFORMAT    (-9)

#define NVCV_INTERLEAVED  0
#define NVCV_CPU          0
#define NVCV_U8           1
#define NVCV_RGBA         6

// Only one memory-space value passes the plugin's "GPU-resident" check, so it
// is probed rather than assumed.
static const int kGpuSpaceCandidates[] = { 1, 2 };

typedef NvCV_Status (*PFN_CreateEffect)(const char*, NvVFX_Handle*);
typedef NvCV_Status (*PFN_DestroyEffect)(NvVFX_Handle);
typedef NvCV_Status (*PFN_Load)(NvVFX_Handle);
typedef NvCV_Status (*PFN_Run)(NvVFX_Handle, int);
typedef NvCV_Status (*PFN_SetImage)(NvVFX_Handle, const char*, void*);
typedef NvCV_Status (*PFN_SetU32)(NvVFX_Handle, const char*, unsigned int);
typedef NvCV_Status (*PFN_SetCudaStream)(NvVFX_Handle, const char*, CUstream);
typedef NvCV_Status (*PFN_CudaStreamCreate)(CUstream*);
typedef NvCV_Status (*PFN_CudaStreamDestroy)(CUstream);
typedef NvCV_Status (*PFN_CudaStreamSynchronize)(CUstream);
typedef NvCV_Status (*PFN_GetVersion)(unsigned int*);
typedef const char* (*PFN_GetErrorString)(NvCV_Status);

typedef NvCV_Status (*PFN_ImageInit)(void*, unsigned, unsigned, int, void*,
                                     int, int, int, int);
typedef NvCV_Status (*PFN_ImageAlloc)(void*, unsigned, unsigned, int, int, int, int, unsigned);
typedef NvCV_Status (*PFN_ImageDealloc)(void*);
typedef NvCV_Status (*PFN_ImageTransfer)(const void*, void*, float, CUstream, void*);
typedef void        (*PFN_ImageCtor)(void*);

static HMODULE g_vfx = nullptr, g_cv = nullptr;
static PFN_CreateEffect          pCreateEffect = nullptr;
static PFN_DestroyEffect         pDestroyEffect = nullptr;
static PFN_Load                  pLoad = nullptr;
static PFN_Run                   pRun = nullptr;
static PFN_SetImage              pSetImage = nullptr;
static PFN_SetU32                pSetU32 = nullptr;
static PFN_SetCudaStream         pSetCudaStream = nullptr;
static PFN_CudaStreamCreate      pStreamCreate = nullptr;
static PFN_CudaStreamDestroy     pStreamDestroy = nullptr;
static PFN_CudaStreamSynchronize pStreamSync = nullptr;
static PFN_GetVersion            pGetVersion = nullptr;
static PFN_GetErrorString        pErrStr = nullptr;
static PFN_ImageInit             pImgInit = nullptr;
static PFN_ImageAlloc            pImgAlloc = nullptr;
static PFN_ImageDealloc          pImgDealloc = nullptr;
static PFN_ImageTransfer         pImgTransfer = nullptr;
static PFN_ImageCtor             pImgCtor = nullptr;

// Far more than this runtime needs, and we never interpret the contents.
static const size_t kImgBytes = 512;
static const int    kMaxSessions = 4;

static const char* ErrTxt(NvCV_Status s) {
    if (pErrStr) { const char* t = pErrStr(s); if (t) return t; }
    return "?";
}

// ---------------------------------------------------------------------------
// SIMD BGR <-> RGBA (same proven routines as dlssnr_host2.cpp)
// ---------------------------------------------------------------------------
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
// session
// ---------------------------------------------------------------------------
struct Session {
    bool used = false;
    int  w = 0, h = 0;
    int  outW = 0, outH = 0;
    int  quality = 0;

    NvVFX_Handle effect = nullptr;
    CUstream stream = nullptr;

    alignas(16) unsigned char inGpu[kImgBytes]{};
    alignas(16) unsigned char outGpu[kImgBytes]{};
    alignas(16) unsigned char inCpu[kImgBytes]{};
    alignas(16) unsigned char outCpu[kImgBytes]{};
    alignas(16) unsigned char scratch[kImgBytes]{};

    // Staging is allocated once, not per frame. inStage holds the BGR->RGBA
    // conversion; outStage is only used when the caller wants BGR, because the
    // RGBA path points the descriptor straight at the caller's buffer.
    uint8_t* inStage = nullptr;
    uint8_t* outStage = nullptr;
};
static Session g_sess[kMaxSessions];
static HMODULE g_self = nullptr;

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) { g_self = h; DisableThreadLibraryCalls(h); }
    return TRUE;
}

static void ModuleDir(wchar_t* out, DWORD cch) {
    GetModuleFileNameW(g_self, out, cch);
    wchar_t* slash = wcsrchr(out, L'\\');
    if (slash) *slash = 0;
}

static FARPROC Need(HMODULE m, const char* name, const char* what) {
    FARPROC p = GetProcAddress(m, name);
    if (!p) HLog("missing export %s from %s", name, what);
    return p;
}

static bool LoadModules() {
    if (g_vfx && g_cv) return true;

    wchar_t dir[MAX_PATH];
    ModuleDir(dir, MAX_PATH);

    // The NvVFX loader resolves the plugin itself with a plain LoadLibrary, so
    // both the documented env var and the search path have to point at us.
    SetEnvironmentVariableW(L"NV_VIDEO_EFFECTS_PATH", L"USE_APP_PATH");
    SetDllDirectoryW(dir);
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_USER_DIRS);
    AddDllDirectory(dir);

    wchar_t p1[MAX_PATH], p2[MAX_PATH];
    swprintf_s(p1, L"%s\\NVCVImage.dll", dir);
    swprintf_s(p2, L"%s\\NVVideoEffects.dll", dir);

    // NVCVImage first: the loader links against it.
    g_cv = LoadLibraryExW(p1, nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!g_cv) { HLog("LoadLibrary NVCVImage failed err=%lu", GetLastError()); return false; }
    g_vfx = LoadLibraryExW(p2, nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!g_vfx) { HLog("LoadLibrary NVVideoEffects failed err=%lu", GetLastError()); return false; }

    pCreateEffect   = (PFN_CreateEffect)  Need(g_vfx, "NvVFX_CreateEffect", "NVVideoEffects");
    pDestroyEffect  = (PFN_DestroyEffect) Need(g_vfx, "NvVFX_DestroyEffect", "NVVideoEffects");
    pLoad           = (PFN_Load)          Need(g_vfx, "NvVFX_Load", "NVVideoEffects");
    pRun            = (PFN_Run)           Need(g_vfx, "NvVFX_Run", "NVVideoEffects");
    pSetImage       = (PFN_SetImage)      Need(g_vfx, "NvVFX_SetImage", "NVVideoEffects");
    pSetU32         = (PFN_SetU32)        Need(g_vfx, "NvVFX_SetU32", "NVVideoEffects");
    pSetCudaStream  = (PFN_SetCudaStream) Need(g_vfx, "NvVFX_SetCudaStream", "NVVideoEffects");
    pStreamCreate   = (PFN_CudaStreamCreate) Need(g_vfx, "NvVFX_CudaStreamCreate", "NVVideoEffects");
    pStreamDestroy  = (PFN_CudaStreamDestroy)Need(g_vfx, "NvVFX_CudaStreamDestroy", "NVVideoEffects");
    pStreamSync     = (PFN_CudaStreamSynchronize)Need(g_vfx, "NvVFX_CudaStreamSynchronize", "NVVideoEffects");
    pGetVersion     = (PFN_GetVersion)    Need(g_vfx, "NvVFX_GetVersion", "NVVideoEffects");

    pImgInit     = (PFN_ImageInit)     Need(g_cv, "NvCVImage_Init", "NVCVImage");
    pImgAlloc    = (PFN_ImageAlloc)    Need(g_cv, "NvCVImage_Alloc", "NVCVImage");
    pImgDealloc  = (PFN_ImageDealloc)  Need(g_cv, "NvCVImage_Dealloc", "NVCVImage");
    pImgTransfer = (PFN_ImageTransfer) Need(g_cv, "NvCVImage_Transfer", "NVCVImage");
    pErrStr      = (PFN_GetErrorString)Need(g_cv, "NvCV_GetErrorStringFromCode", "NVCVImage");
    // Mangled default constructor; zeroed storage works too, but calling the
    // real one costs nothing and removes the assumption.
    pImgCtor     = (PFN_ImageCtor)     GetProcAddress(g_cv, "??0NvCVImage@@QEAA@XZ");

    if (!pCreateEffect || !pLoad || !pRun || !pSetImage || !pSetU32 ||
        !pSetCudaStream || !pStreamCreate || !pStreamSync ||
        !pImgInit || !pImgAlloc || !pImgTransfer) {
        HLog("RTX Video runtime exports incomplete");
        return false;
    }

    unsigned int ver = 0;
    if (pGetVersion && pGetVersion(&ver) == NVCV_SUCCESS)
        HLog("NVVideoEffects version 0x%08X (%u.%u.%u)",
             ver, (ver >> 24) & 0xFF, (ver >> 16) & 0xFF, (ver >> 8) & 0xFF);
    return true;
}

static bool MemInit(void* img, int w, int h, void* pixels) {
    memset(img, 0, kImgBytes);
    if (pImgCtor) pImgCtor(img);
    NvCV_Status r = pImgInit(img, (unsigned)w, (unsigned)h, 0, pixels,
                             NVCV_RGBA, NVCV_U8, NVCV_INTERLEAVED, NVCV_CPU);
    if (r != NVCV_SUCCESS) HLog("NvCVImage_Init failed %d %s", r, ErrTxt(r));
    return r == NVCV_SUCCESS;
}

static bool MemAllocGpu(void* img, int w, int h, int space) {
    memset(img, 0, kImgBytes);
    if (pImgCtor) pImgCtor(img);
    NvCV_Status r = pImgAlloc(img, (unsigned)w, (unsigned)h,
                              NVCV_RGBA, NVCV_U8, NVCV_INTERLEAVED, space, 32);
    if (r != NVCV_SUCCESS)
        HLog("NvCVImage_Alloc(gpu,space=%d) failed %d %s", space, r, ErrTxt(r));
    return r == NVCV_SUCCESS;
}

// Creates and loads an effect for `quality` bound to the session's images.
static bool CreateLoaded(Session& s, int quality, NvVFX_Handle* out) {
    NvVFX_Handle h = nullptr;
    NvCV_Status r = pCreateEffect("VideoSuperRes", &h);
    if (r != NVCV_SUCCESS || !h) {
        HLog("NvVFX_CreateEffect failed %d %s", r, ErrTxt(r));
        return false;
    }
    if ((r = pSetImage(h, "SrcImage0", s.inGpu)) != NVCV_SUCCESS ||
        (r = pSetImage(h, "DstImage0", s.outGpu)) != NVCV_SUCCESS) {
        HLog("NvVFX_SetImage failed %d %s", r, ErrTxt(r));
        pDestroyEffect(h); return false;
    }
    if ((r = pSetCudaStream(h, "CudaStream", s.stream)) != NVCV_SUCCESS) {
        HLog("NvVFX_SetCudaStream failed %d %s", r, ErrTxt(r));
        pDestroyEffect(h); return false;
    }
    if ((r = pSetU32(h, "QualityLevel", (unsigned)quality)) != NVCV_SUCCESS) {
        HLog("NvVFX_SetU32(QualityLevel=%d) failed %d %s", quality, r, ErrTxt(r));
        pDestroyEffect(h); return false;
    }
    if ((r = pLoad(h)) != NVCV_SUCCESS) {
        HLog("NvVFX_Load failed for quality=%d : %d %s", quality, r, ErrTxt(r));
        pDestroyEffect(h); return false;
    }
    *out = h;
    return true;
}

static void DestroySession(Session& s) {
    if (!s.used) return;
    // NvVFX_DestroyEffect NEVER returns on this runtime -- it blocks forever
    // after a successful Load (measured: hangs past 120 s). So the effect handle
    // is deliberately leaked and only our own allocations are freed. Since a
    // session is keyed by (size, quality) and reused for the process lifetime,
    // that is a bounded, one-time cost rather than a per-frame leak.
    if (s.effect) { HLog("NOTE: leaking effect handle (NvVFX_DestroyEffect hangs)"); s.effect = nullptr; }
    if (pImgDealloc) { pImgDealloc(s.outGpu); pImgDealloc(s.inGpu); }
    memset(s.inGpu, 0, kImgBytes);  memset(s.outGpu, 0, kImgBytes);
    memset(s.inCpu, 0, kImgBytes);  memset(s.outCpu, 0, kImgBytes);
    memset(s.scratch, 0, kImgBytes);
    if (s.inStage)  { _aligned_free(s.inStage);  s.inStage = nullptr; }
    if (s.outStage) { _aligned_free(s.outStage); s.outStage = nullptr; }
    if (s.stream && pStreamDestroy) { pStreamDestroy(s.stream); s.stream = nullptr; }
    s.used = false;
}

// ---------------------------------------------------------------------------
// core
// ---------------------------------------------------------------------------
static int ProcessCore(Session& s, const uint8_t* inBgr, uint8_t* outRgba, uint8_t* outBgr) {
    if (!s.used || !s.effect) { HLog("process: session not ready"); return 0; }
    if (!inBgr || (!outRgba && !outBgr)) return 0;

    const int W = s.w, H = s.h, OW = s.outW, OH = s.outH;
    const double t0 = NowMs();

    // 1. CPU: BGR -> RGBA into the persistent staging buffer. inCpu is already
    //    bound to it, so no descriptor work happens on this path.
    for (int y = 0; y < H; ++y)
        BGR2RGBA_row(inBgr + (size_t)y * W * 3, s.inStage + (size_t)y * W * 4, W);
    const double t1 = NowMs();

    // 2. Bind the output descriptor. For RGBA callers it points straight at
    //    their buffer, so the readback is pure DMA and nothing is copied.
    uint8_t* dstStage = outRgba ? outRgba : s.outStage;
    if (!MemInit(s.outCpu, OW, OH, dstStage)) return 0;

    // 3. H2D, run, D2H -- queued on this session's CUDA stream.
    NvCV_Status r = pImgTransfer(s.inCpu, s.inGpu, 1.0f, s.stream, s.scratch);
    if (r != NVCV_SUCCESS) { HLog("Transfer H2D failed %d %s", r, ErrTxt(r)); return 0; }
    const double t2 = NowMs();

    r = pRun(s.effect, 0);
    if (r != NVCV_SUCCESS) { HLog("NvVFX_Run failed %d %s", r, ErrTxt(r)); return 0; }
    const double t3 = NowMs();

    r = pImgTransfer(s.outGpu, s.outCpu, 1.0f, s.stream, s.scratch);
    if (r != NVCV_SUCCESS) { HLog("Transfer D2H failed %d %s", r, ErrTxt(r)); return 0; }
    if ((r = pStreamSync(s.stream)) != NVCV_SUCCESS) {
        HLog("stream sync failed %d %s", r, ErrTxt(r)); return 0;
    }
    const double t4 = NowMs();

    if (outBgr) {
        for (int y = 0; y < OH; ++y)
            RGBA2BGR_row(dstStage + (size_t)y * OW * 4, outBgr + (size_t)y * OW * 3, OW);
    }
    const double t5 = NowMs();

    if (g_perf) {
        HLog("PERF convIn=%.3f h2d=%.3f run=%.3f d2h+sync=%.3f convOut=%.3f total=%.3f ms",
             t1 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4, t5 - t0);
    }
    return 1;
}

static Session* Get(int sid) {
    if (sid < 1 || sid > kMaxSessions) return nullptr;
    Session& s = g_sess[sid - 1];
    return s.used ? &s : nullptr;
}

// ---------------------------------------------------------------------------
// exports
// ---------------------------------------------------------------------------
extern "C" {

// Returns a session id (>=1) on success, 0 on failure. Each call creates an
// independent effect instance, so up to kMaxSessions can be live at once.
__declspec(dllexport) int vfx_create(int w, int h, int outW, int outH,
                                     int quality, const wchar_t* logPath) {
    if (logPath) wcsncpy_s(g_logPath, _countof(g_logPath), logPath, _TRUNCATE);
    { FILE* f = nullptr; fopen_s(&f, "vfx_perf.on", "rb"); if (f) { g_perf = 1; fclose(f); } }

    HLog("=== vfx_create %dx%d -> %dx%d quality=%d ===", w, h, outW, outH, quality);
    if (w <= 0 || h <= 0 || outW <= 0 || outH <= 0) { HLog("bad sizes"); return 0; }
    if (!LoadModules()) return 0;

    const bool sameRes = (outW == w && outH == h);
    if (!sameRes && (quality < 1 || quality > 4)) {
        HLog("quality %d requires equal in/out resolution (only VSR 1-4 rescales)", quality);
        return 0;
    }

    int slot = -1;
    for (int i = 0; i < kMaxSessions; ++i) if (!g_sess[i].used) { slot = i; break; }
    if (slot < 0) { HLog("no free session slot (max %d)", kMaxSessions); return 0; }

    Session& s = g_sess[slot];
    s.w = w; s.h = h; s.outW = outW; s.outH = outH; s.quality = quality;

    if (pStreamCreate(&s.stream) != NVCV_SUCCESS || !s.stream) {
        HLog("NvVFX_CudaStreamCreate failed"); s = Session{}; return 0;
    }

    // The plugin validates "GPU-resident" itself, and only one memory-space
    // value satisfies it, so probe rather than assume.
    bool gpuOk = false;
    for (int cand : kGpuSpaceCandidates) {
        if (MemAllocGpu(s.inGpu, w, h, cand) && MemAllocGpu(s.outGpu, outW, outH, cand)) {
            gpuOk = true;
            HLog("gpu memory space resolved to %d", cand);
            break;
        }
        HLog("gpu space %d rejected, trying next", cand);
    }
    if (!gpuOk) { HLog("no usable GPU memory space"); DestroySession(s); return 0; }

    s.inStage  = (uint8_t*)_aligned_malloc((size_t)w * h * 4, 64);
    s.outStage = (uint8_t*)_aligned_malloc((size_t)outW * outH * 4, 64);
    if (!s.inStage || !s.outStage) { HLog("staging alloc failed"); DestroySession(s); return 0; }
    if (!MemInit(s.inCpu, w, h, s.inStage) || !MemInit(s.outCpu, outW, outH, s.outStage)) {
        DestroySession(s); return 0;
    }
    memset(s.scratch, 0, kImgBytes);
    if (pImgCtor) pImgCtor(s.scratch);

    NvVFX_Handle hh = nullptr;
    if (!CreateLoaded(s, quality, &hh)) { DestroySession(s); return 0; }
    s.effect = hh;
    s.used = true;
    HLog("session %d ready: in=%dx%d out=%dx%d quality=%d", slot + 1, w, h, outW, outH, quality);
    return slot + 1;
}

__declspec(dllexport) int vfx_process_rgba(int sid, const uint8_t* inBgr, uint8_t* outRgba) {
    Session* s = Get(sid);
    if (!s) { HLog("process_rgba: bad session %d", sid); return 0; }
    return ProcessCore(*s, inBgr, outRgba, nullptr);
}

__declspec(dllexport) int vfx_process_bgr(int sid, const uint8_t* inBgr, uint8_t* outBgr) {
    Session* s = Get(sid);
    if (!s) { HLog("process_bgr: bad session %d", sid); return 0; }
    return ProcessCore(*s, inBgr, nullptr, outBgr);
}

__declspec(dllexport) void vfx_destroy(int sid) {
    Session* s = Get(sid);
    if (!s) return;
    HLog("destroy session %d", sid);
    DestroySession(*s);
}

// Switches the QualityLevel of an existing session in place.
//
// This matters because creating a session costs a full model Load (~13 s) and
// destroying one is impossible (see DestroySession). If SetU32 + Load on an
// already-loaded handle works, one session can serve every mode at a given
// size, which keeps the pool small and makes mode changes cheap after the first.
__declspec(dllexport) int vfx_set_quality(int sid, int quality) {
    Session* s = Get(sid);
    if (!s) { HLog("set_quality: bad session %d", sid); return 0; }
    if (s->quality == quality) return 1;
    const bool sameRes = (s->outW == s->w && s->outH == s->h);
    if (!sameRes && (quality < 1 || quality > 4)) {
        HLog("set_quality: %d needs equal in/out resolution", quality);
        return 0;
    }
    if (!pSetU32 || !pLoad) return 0;
    const double t0 = NowMs();
    NvCV_Status r = pSetU32(s->effect, "QualityLevel", (unsigned)quality);
    if (r == NVCV_SUCCESS) r = pLoad(s->effect);
    if (r != NVCV_SUCCESS) {
        HLog("set_quality(%d) failed %d %s", quality, r, ErrTxt(r));
        return 0;
    }
    HLog("set_quality %d -> %d in %.0f ms", s->quality, quality, NowMs() - t0);
    s->quality = quality;
    return 1;
}

__declspec(dllexport) void vfx_shutdown() {
    HLog("=== shutdown (all sessions) ===");
    for (int i = 0; i < kMaxSessions; ++i) DestroySession(g_sess[i]);
}

// DIAGNOSTIC. Isolates transfer cost from compute, and tests whether giving
// NvCVImage_Transfer a PROPERLY ALLOCATED GPU scratch image (rather than the
// zeroed, unallocated descriptor we normally pass) changes anything.
//
// It does two things, because the NvCVImage_Transfer signature differs:
//   scratch != null : Transfer(src, dst, scale, stream, scratch)
//   scratch == null : Transfer(src, dst, scale, stream, nullptr)
// Both are legal; the runtime allocates internally when it needs to. The point
// is to find out which is faster rather than assume.
__declspec(dllexport) int vfx_bench_transfer(int sid, int reps, int useScratch,
                                             double* outH2D, double* outRun,
                                             double* outD2H) {
    Session* s = Get(sid);
    if (!s || !s->used || reps <= 0) return 0;

    // A real GPU image for the scratch, allocated at the OUTPUT size.
    alignas(16) unsigned char scratchImg[kImgBytes]{};
    bool scratchOk = false;
    if (useScratch) {
        if (pImgCtor) pImgCtor(scratchImg);
        scratchOk = MemAllocGpu(scratchImg, s->outW, s->outH, g_sess[0].used ? 1 : 1);
    }
    void* scratchPtr = scratchOk ? (void*)scratchImg : nullptr;

    // Three phases timed separately, over many reps, with ONE sync at the end of
    // each phase so the numbers are pipeline-visible rather than per-call sync.
    double t0 = NowMs();
    for (int i = 0; i < reps; ++i) {
        NvCV_Status r = pImgTransfer(s->inCpu, s->inGpu, 1.0f, s->stream, scratchPtr);
        if (r != NVCV_SUCCESS) { HLog("bench h2d failed %d %s", r, ErrTxt(r)); return 0; }
    }
    pStreamSync(s->stream);
    double t1 = NowMs();

    for (int i = 0; i < reps; ++i) {
        NvCV_Status r = pRun(s->effect, 0);
        if (r != NVCV_SUCCESS) { HLog("bench run failed %d %s", r, ErrTxt(r)); return 0; }
    }
    pStreamSync(s->stream);
    double t2 = NowMs();

    for (int i = 0; i < reps; ++i) {
        NvCV_Status r = pImgTransfer(s->outGpu, s->outCpu, 1.0f, s->stream, scratchPtr);
        if (r != NVCV_SUCCESS) { HLog("bench d2h failed %d %s", r, ErrTxt(r)); return 0; }
    }
    pStreamSync(s->stream);
    double t3 = NowMs();

    if (outH2D) *outH2D = (t1 - t0) / reps;
    if (outRun) *outRun = (t2 - t1) / reps;
    if (outD2H) *outD2H = (t3 - t2) / reps;

    HLog("BENCH scratch=%d reps=%d  h2d=%.3f run=%.3f d2h=%.3f ms/frame",
         useScratch ? 1 : 0, reps, (t1 - t0) / reps, (t2 - t1) / reps, (t3 - t2) / reps);

    if (scratchOk && pImgDealloc) pImgDealloc(scratchImg);
    return 1;
}

} // extern "C"
