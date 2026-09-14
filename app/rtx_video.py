"""RTX Video stages — VSR upscaling, Denoise, Deblur, HighBitrate and TrueHDR.

Copyright (c) 2026 Cyanke. MIT License — see LICENSE at the repository root.

This file is an original work: it is not derived from purkatyy/DLSS5- and has no
counterpart upstream. It wraps the official NVIDIA RTX Video SDK
(vfx_host.dll / truehdr_host.dll), which the user must supply separately; no
NVIDIA binary is distributed with this repository.

Independent capabilities, each with its own switch:

  AI 放大 (VSR)       QualityLevel 1..4   changes the output resolution
  降噪 (Denoise)      QualityLevel 8..11  same resolution
  去模糊 (Deblur)     QualityLevel 12..15 same resolution
  压缩画面对抗        QualityLevel 16..19 same resolution
  SDR -> HDR10        TrueHDR             same resolution, HDR output

The first four are ONE NVIDIA effect ("VideoSuperRes") selected by QualityLevel,
so a single instance can only hold one of them. vfx_host.dll therefore exposes a
session API: each (size, quality) pair gets its own instance, which is what lets
an upscale and a sharpen coexist on the same frame without reloading models.

TrueHDR is a separate feature in truehdr_host.dll, which statically links its own
NGX loader so it cannot disturb the DLSSNR path. Verified experimentally: both
initialise and run in one process, and TrueHDR.Available still reports 1 with
DLSSNR live.

Stage order, applied in this sequence when enabled:

    decode (BGR 8bit)
      -> VSR upscale                    (changes resolution; later stages see the new size)
      -> DLSSNR                         (same resolution, the existing path)
      -> Denoise / Deblur / HighBitrate (same resolution)
      -> TrueHDR                        (8bit SDR in, HDR out; must be last)
"""

import ctypes
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# QualityLevel tables
# ---------------------------------------------------------------------------
VSR_CHOICES = {
    "关闭": 0,
    "放大 低": 1, "放大 中": 2, "放大 高": 3, "放大 超高": 4,
}

ENHANCE_CHOICES = {
    "关闭": 0,
    "降噪 低": 8, "降噪 中": 9, "降噪 高": 10, "降噪 超高": 11,
    "去模糊 低": 12, "去模糊 中": 13, "去模糊 高": 14, "去模糊 超高": 15,
    "高码率 低": 16, "高码率 中": 17, "高码率 高": 18, "高码率 超高": 19,
}

QUALITY_NAMES = {
    0: "VSR_Bicubic",
    1: "VSR_Low", 2: "VSR_Medium", 3: "VSR_High", 4: "VSR_Ultra",
    8: "Denoise_Low", 9: "Denoise_Medium", 10: "Denoise_High", 11: "Denoise_Ultra",
    12: "Deblur_Low", 13: "Deblur_Medium", 14: "Deblur_High", 15: "Deblur_Ultra",
    16: "HighBitrate_Low", 17: "HighBitrate_Medium", 18: "HighBitrate_High",
    19: "HighBitrate_Ultra",
}

SIZE_CHOICES = ["×1.5", "×2", "×3", "×4", "720p", "1080p", "1440p", "2160p"]
_SCALE = {"×1.5": 1.5, "×2": 2.0, "×3": 3.0, "×4": 4.0}
_NAMED_H = {"720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160}


def is_vsr(q):
    return 1 <= q <= 4


def is_enhance(q):
    return 8 <= q <= 19


def target_size(label, w, h):
    """Output size for a size label. Keeps the aspect ratio and rounds to even
    numbers, because yuv420p chroma is subsampled and odd sizes break encoders.
    Never downscales: returns (w, h) unchanged if the target is not larger."""
    if label in _NAMED_H:
        th = _NAMED_H[label]
    elif label in _SCALE:
        th = int(round(h * _SCALE[label]))
    else:
        return w, h
    if th <= h:
        return w, h
    ow = int(round(w * th / h))
    return ow - (ow % 2), th - (th % 2)


# ---------------------------------------------------------------------------
# vfx_host.dll — VideoSuperRes family (session based)
# ---------------------------------------------------------------------------
class _Vfx:
    """Session pool for the VideoSuperRes family.

    Two hard facts from measurement drive this design:

      * creating a session costs a full model Load, ~13 s
      * NvVFX_DestroyEffect NEVER returns, so a session cannot be torn down

    Therefore sessions are keyed by SIZE ONLY and the QualityLevel is switched
    in place, which is instantaneous (SetU32 + Load on a live handle, measured
    at 0.00 s and bit-exact reproducible). One session per size pair serves
    every mode, so the pool stays at two or three and each size pays the 13 s
    model load exactly once.
    """

    def __init__(self):
        self.lib = None
        self.sessions = {}      # (w, h, ow, oh) -> session id
        self._quality = {}      # session id -> current quality
        self._order = []        # keys, least recently used first

    # Sessions are expensive to make (13 s) and impossible to destroy cleanly, so
    # only this many are kept alive; going past it retires the oldest.
    SOFT_CAP = 3

    def _load(self):
        if self.lib is not None:
            return True
        dll = os.path.join(_HERE, "vfx_host.dll")
        if not os.path.exists(dll):
            raise RuntimeError("缺少 vfx_host.dll")
        d = ctypes.CDLL(dll)
        d.vfx_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        d.vfx_create.restype = ctypes.c_int
        d.vfx_set_quality.argtypes = [ctypes.c_int, ctypes.c_int]
        d.vfx_set_quality.restype = ctypes.c_int
        d.vfx_process_rgba.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        d.vfx_process_rgba.restype = ctypes.c_int
        d.vfx_process_bgr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        d.vfx_process_bgr.restype = ctypes.c_int
        d.vfx_destroy.argtypes = [ctypes.c_int]
        d.vfx_destroy.restype = None
        d.vfx_shutdown.argtypes = []
        self.lib = d
        return True

    def available(self):
        try:
            return self._load()
        except Exception:
            return False

    def session(self, w, h, ow, oh, quality):
        """Session for this size pair, with `quality` selected.

        The first call for a given size costs ~13 s (model load); later calls,
        including quality changes, are free.
        """
        self._load()
        key = (w, h, ow, oh)
        sid = self.sessions.get(key)
        if sid is None:
            self._retire_oldest()
            log = os.path.join(_HERE, "vfx_log.txt")
            sid = self.lib.vfx_create(w, h, ow, oh, quality, log)
            if sid < 1:
                raise RuntimeError("RTX Video 初始化失败 %dx%d → %dx%d"
                                   % (w, h, ow, oh))
            self.sessions[key] = sid
            self._quality[sid] = quality
            self._order.append(key)
            return sid
        if self._quality.get(sid) != quality:
            if self.lib.vfx_set_quality(sid, quality) != 1:
                raise RuntimeError("RTX Video 模式切换失败 (模式 %s)"
                                   % QUALITY_NAMES.get(quality, quality))
            self._quality[sid] = quality
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        return sid

    def _retire_oldest(self):
        """Free one slot if the pool would otherwise overflow."""
        while len(self.sessions) >= self.SOFT_CAP and self._order:
            self.release(self._order[0])

    def release(self, *keys):
        """Drop sessions for these size pairs.

        vfx_destroy frees our buffers but intentionally leaks the NVIDIA effect
        handle, because destroying it hangs the process forever.
        """
        for key in keys:
            sid = self.sessions.pop(key, None)
            if sid and self.lib is not None:
                self._quality.pop(sid, None)
                try:
                    self.lib.vfx_destroy(sid)
                except Exception:
                    pass
            if key in self._order:
                self._order.remove(key)

    def process_bgr(self, sid, bgr, out=None):
        if out is None:
            out = np.empty_like(bgr)
        r = self.lib.vfx_process_bgr(sid, bgr.ctypes.data_as(ctypes.c_void_p),
                                     out.ctypes.data_as(ctypes.c_void_p))
        if r != 1:
            raise RuntimeError("RTX Video 处理失败")
        return out

    def process_rgba(self, sid, bgr, out):
        r = self.lib.vfx_process_rgba(sid, bgr.ctypes.data_as(ctypes.c_void_p),
                                      out.ctypes.data_as(ctypes.c_void_p))
        if r != 1:
            raise RuntimeError("RTX Video 处理失败")
        return out

    def close(self):
        if self.lib is not None:
            try:
                self.lib.vfx_shutdown()
            except Exception:
                pass
        self.sessions.clear()


# ---------------------------------------------------------------------------
# truehdr_host.dll — SDR -> HDR
# ---------------------------------------------------------------------------
class _TrueHdr:
    FP16, R10 = 0, 1

    def __init__(self):
        self.lib = None
        self.state = None
        self._avail = None
        self._out = None
        self.out_bytes = 0

    def _load(self):
        if self.lib is not None:
            return True
        dll = os.path.join(_HERE, "truehdr_host.dll")
        if not os.path.exists(dll):
            raise RuntimeError("缺少 truehdr_host.dll")
        d = ctypes.CDLL(dll)
        d.thdr_init.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_wchar_p]
        d.thdr_init.restype = ctypes.c_int
        d.thdr_available.argtypes = [ctypes.c_wchar_p]
        d.thdr_available.restype = ctypes.c_int
        d.thdr_set_params.argtypes = [ctypes.c_uint] * 4
        d.thdr_set_params.restype = None
        d.thdr_out_bytes.argtypes = []
        d.thdr_out_bytes.restype = ctypes.c_int
        d.thdr_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        d.thdr_process.restype = ctypes.c_int
        d.thdr_shutdown.argtypes = []
        self.lib = d
        return True

    def available(self):
        """Whether this system exposes TrueHDR.

        Cached deliberately: the probe starts the NGX core, and NGX is a
        process-wide singleton that must not be initialised twice.
        """
        if self._avail is not None:
            return self._avail
        try:
            self._load()
        except Exception:
            self._avail = False
            return False
        log = os.path.join(_HERE, "thdr_log.txt")
        try:
            self._avail = bool(self.lib.thdr_available(log))
        except Exception:
            self._avail = False
        return self._avail

    def ensure(self, w, h, fmt=None):
        self._load()
        fmt = self.FP16 if fmt is None else int(fmt)
        key = (w, h, fmt)
        if self.state == key:
            return True
        log = os.path.join(_HERE, "thdr_log.txt")
        if self.lib.thdr_init(w, h, fmt, log) != 1:
            raise RuntimeError("TrueHDR 初始化失败（需要 RTX 显卡与较新驱动）")
        self.state = key
        self.out_bytes = int(self.lib.thdr_out_bytes())
        if fmt == self.FP16:
            self._out = np.zeros((h, w, 4), np.float16)
        else:
            self._out = np.zeros((h, w), np.uint32)
        return True

    def set_params(self, contrast=100, saturation=100, middle_gray=50,
                   max_luminance=1000):
        self._load()
        self.lib.thdr_set_params(int(contrast), int(saturation),
                                 int(middle_gray), int(max_luminance))

    def process(self, bgr):
        if self.state is None:
            raise RuntimeError("TrueHDR 未初始化")
        r = self.lib.thdr_process(bgr.ctypes.data_as(ctypes.c_void_p),
                                  self._out.ctypes.data_as(ctypes.c_void_p))
        if r != 1:
            raise RuntimeError("TrueHDR 处理失败")
        return self._out

    def close(self):
        # The feature is released, but the NGX core is intentionally left
        # running: NVSDK_NGX_D3D11_Shutdown1 hangs after a successful init on
        # this driver, so the process simply exits with NGX still up.
        if self.lib is not None:
            try:
                self.lib.thdr_shutdown()
            except Exception:
                pass
        self.state = None
        self._out = None


vfx = _Vfx()
truehdr = _TrueHdr()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
class Pipeline:
    """Chains the enabled RTX Video stages around the existing DLSSNR pass.

    Settings keys (all optional):
        vsr_quality   0 = off, 1..4      -> upscale to (out_w, out_h)
        out_w/out_h   target size when vsr_quality > 0
        enhance       0 = off, 8..19     Denoise / Deblur / HighBitrate
        hdr           0/1                TrueHDR
        hdr_format    0 = FP16, 1 = R10G10B10A2
        hdr_contrast / hdr_saturation / hdr_middle_gray / hdr_max_luminance
    """

    def __init__(self):
        self.enabled = False
        self.w = self.h = 0
        self.ow = self.oh = 0
        self.out_fmt = "rgba"
        self.hdr10 = False
        self._q_vsr = self._q_enh = 0
        self._sid_vsr = self._sid_enh = 0
        self._key_vsr = self._key_enh = None
        self._dlss = None
        self._tmp_bgr = None
        self._buf_out = None
        self._hdr_fmt = 0

    @staticmethod
    def needs_rtx(settings):
        """True when any RTX Video stage is on, i.e. when the plain DLSSNR fast
        path cannot be used."""
        return (is_vsr(int(settings.get("vsr_quality", 0)))
                or is_enhance(int(settings.get("enhance", 0)))
                or bool(settings.get("hdr", 0)))

    def configure(self, w, h, settings, dlss_engine=None):
        """Resolve sizes, create/select sessions. Returns (final_w, final_h, in_fmt)."""
        self.w, self.h = w, h
        self._dlss = dlss_engine
        self._q_vsr = int(settings.get("vsr_quality", 0))
        self._q_enh = int(settings.get("enhance", 0))
        use_vsr = is_vsr(self._q_vsr)
        use_enh = is_enhance(self._q_enh)
        use_hdr = bool(settings.get("hdr", 0))
        self.enabled = use_vsr or use_enh or use_hdr

        if use_hdr and not truehdr.available():
            raise RuntimeError("此系统不支持 TrueHDR（需要 RTX 显卡与较新驱动）")

        # ---- size after the upscale stage ----
        # Stage handles are cleared first: a Pipeline instance is reused across
        # reconfigurations (the preview keeps one), so a stage that was just
        # switched off must not keep running on its stale session id.
        self._sid_vsr = self._sid_enh = 0
        self._key_vsr = self._key_enh = None
        self.ow, self.oh = w, h
        if use_vsr:
            ow = int(settings.get("out_w", w))
            oh = int(settings.get("out_h", h))
            if ow <= 0 or oh <= 0:
                raise RuntimeError("放大目标分辨率无效")
            self._key_vsr = (w, h, ow, oh)
            self._sid_vsr = vfx.session(*self._key_vsr, quality=self._q_vsr)
            self.ow, self.oh = ow, oh

        # ---- same-resolution enhancement runs at whatever size it receives ----
        if use_enh:
            self._key_enh = (self.ow, self.oh, self.ow, self.oh)
            self._sid_enh = vfx.session(*self._key_enh, quality=self._q_enh)

        # ---- TrueHDR is last and never changes the size ----
        if use_hdr:
            # R10G10B10A2 is the default and the correct choice for HDR10
            # delivery. Verified by round trip: encoding it straight to
            # yuv420p10le with PQ / BT.2020 tags reproduces the source SDR
            # faithfully (mean|diff| 5.95/255), whereas the FP16 output is
            # linear scRGB and would need a real linear->PQ conversion first
            # (tagging it as PQ directly gives 47.24/255). Its brightest code
            # also lands on exactly PQ(1000 nits) = 0.751 for max_luminance
            # 1000, confirming the encoding.
            fmt = int(settings.get("hdr_format", 1))
            truehdr.ensure(self.ow, self.oh, fmt)
            truehdr.set_params(settings.get("hdr_contrast", 100),
                               settings.get("hdr_saturation", 100),
                               settings.get("hdr_middle_gray", 50),
                               settings.get("hdr_max_luminance", 1000))
            self._hdr_fmt = fmt
            # R10G10B10A2 packs R in the LOW bits and B in the HIGH bits, which is
            # the opposite of ffmpeg's X2R10G10B10. Measured with pure-colour
            # probes: feeding a DXGI R10G10B10A2 buffer as x2rgb10le swaps red and
            # blue (pure red comes back as RGB(0,0,255)); x2bgr10le returns it
            # correctly. So the BGR-named format is the right one here.
            self.out_fmt = "rgbaf16le" if fmt == 0 else "x2bgr10le"
            self.hdr10 = True
        else:
            # Every non-HDR stage ends in BGR, which is what cv2 hands us and
            # what ffmpeg accepts as bgr24.
            self.out_fmt = "bgr24"
            self.hdr10 = False

        self._buf_out = None
        return self.ow, self.oh, self.out_fmt

    def _release_sessions(self):
        keys = [k for k in (self._key_vsr, self._key_enh) if k]
        if keys:
            vfx.release(*keys)
        self._key_vsr = self._key_enh = None
        self._sid_vsr = self._sid_enh = 0

    def process(self, bgr, reset=False):
        """One frame through every enabled stage.

        Returns (payload_bytes, kind) with kind in {'rgba', 'fp16', 'r10'}.
        """
        cur = bgr

        if self._sid_vsr:
            if self._tmp_bgr is None or self._tmp_bgr.shape[0] != self.oh:
                self._tmp_bgr = np.empty((self.oh, self.ow, 3), np.uint8)
            cur = vfx.process_bgr(self._sid_vsr, cur, self._tmp_bgr)

        if self._dlss is not None:
            self._dlss.ensure(cur.shape[1], cur.shape[0])
            cur = self._dlss.process(cur, reset=reset)

        if self._sid_enh:
            if self._tmp_bgr is None or self._tmp_bgr is cur:
                self._tmp_bgr = np.empty_like(cur)
            cur = vfx.process_bgr(self._sid_enh, cur, self._tmp_bgr)

        if self.hdr10:
            out = truehdr.process(cur)
            return out.tobytes(), ("fp16" if self._hdr_fmt == 0 else "r10")
        return np.ascontiguousarray(cur).tobytes(), "bgr"

    def hdr_reference_ok(self):
        """True when the HDR output is the linear-scRGB (FP16) form, which is
        what a colorimetrically correct PQ conversion needs as its input."""
        return self.hdr10 and self._hdr_fmt == 0

    def close(self):
        """Release the HDR feature but KEEP the vfx sessions cached.

        A session costs ~13 s to rebuild, and the GUI reconfigures whenever a
        setting changes, so dropping them here would make every adjustment feel
        like a hang. They are pooled by size and reused automatically; use
        rtx_video.close_all() for a real teardown.
        """
        truehdr.close()
        self._sid_vsr = self._sid_enh = 0
        self._key_vsr = self._key_enh = None


def close_all():
    """Full teardown: drop every vfx session and release the HDR feature."""
    vfx.close()
    truehdr.close()
