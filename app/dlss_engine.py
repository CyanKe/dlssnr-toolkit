#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dlss_engine.py — ctypes wrapper for the test4 DLSS5 Feature 18 host DLL (zero-guidance).

The Feature 18 ("neural render") ignores depth/flow guidance in this config, so this
engine always feeds ZERO guidance. It only needs the colour frames from the video.

---------------------------------------------------------------------------
来源 / Provenance
    本文件衍生自 purkatyy/DLSS5- (MIT License, Copyright (c) 2026 ylso0)。
    上游原始版本 179 行，封装 dlssnr_host.dll（RGBA 四指针契约）。
    Cyanke 改写为 GPU 直通版本：新增 Engine2 类，封装 dlssnr_host2.dll
    （BGR 进 / RGBA 出，双槽流水线，省掉 Python 侧两次 numpy 转换）。
    本文件同样以 MIT 许可发布，完整条款见仓库根目录 LICENSE。

    Derived from purkatyy/DLSS5- (MIT License, Copyright (c) 2026 ylso0).
    Extended by Cyanke with the GPU-direct Engine2 path. Released under the
    MIT License; see LICENSE at the repository root.
---------------------------------------------------------------------------
"""
import ctypes
import os
import sys
import numpy as np

# resolve the bundle dir: PyInstaller (frozen) -> _MEIPASS, else the script's own dir
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
HOST_DLL = os.path.join(BASE, "dlssnr_host.dll")
DLSSNR_DLL = os.path.join(BASE, "nvngx_dlssnr.dll")
LOG_PATH = os.path.join(BASE, "dlss_run.log")

_lib = None


def _load():
    global _lib
    if _lib is None:
        if not os.path.exists(HOST_DLL):
            raise FileNotFoundError("missing %s" % HOST_DLL)
        _lib = ctypes.CDLL(HOST_DLL)
        _lib.dlssnr_init.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_wchar_p]
        _lib.dlssnr_init.restype = ctypes.c_int
        _lib.dlssnr_create_feature.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        _lib.dlssnr_create_feature.restype = ctypes.c_int
        _lib.dlssnr_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _lib.dlssnr_process.restype = ctypes.c_int
        _lib.dlssnr_set_options.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_float, ctypes.c_float]
        _lib.dlssnr_set_options.restype = None
        _lib.dlssnr_shutdown.argtypes = []
        _lib.dlssnr_resize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        _lib.dlssnr_resize.restype = ctypes.c_int
    return _lib


# settings dict keys that actually change the output on this Feature 18 config:
#   style (int), intensity (0..1), local_tone (0..1), local_struct (0..1),
#   output_view (0/1/2), output_mix (0..1). preset/guidance/depth/flow are inert.
def _set_options(lib, s):
    lib.dlssnr_set_options(
        int(s.get('preset', 1)),          # preset: inert at same-res, keep 1
        int(s.get('style', 0)),
        float(s.get('intensity', 1.0)),
        float(s.get('local_tone', 1.0)),
        float(s.get('local_struct', 1.0)),
        float(s.get('skin_struct', 1.0)),  # inert
        int(s.get('use_auto_mask', 0)),    # inert
        int(s.get('ui_correction', 0)),    # inert
        0,                                 # guidance_mode ALWAYS 0 (off) — NR ignores guidance
        int(s.get('depth_convention', 2)), # inert (depth ignored)
        float(s.get('motion_scale_x', 1.0)),
        float(s.get('motion_scale_y', 1.0)))


def _apply_output_view(processed, color, view, mix, w, h):
    """Post-process the DLSS RGBA8 output per Output View (0=Processed,1=DiffX10,2=L/R Compare)."""
    out = []
    for pr, co in zip(processed, color):
        cof = co[..., :3].astype(np.float32) / 255.0
        prf = pr[..., :3].astype(np.float32) / 255.0
        if view == 1:      # Difference x10
            r = np.clip(0.5 + (prf - cof) * 10.0, 0, 1)
        elif view == 2:    # Left / Right compare
            r = prf.copy()
            r[:, :w // 2] = cof[:, :w // 2]
            if w % 2 == 1:
                r[:, w // 2] = 1.0
        else:              # Processed, blended by mix
            r = cof + (prf - cof) * mix
        res = np.dstack([r, np.ones((h, w), np.float32)])
        out.append((res * 255.0).clip(0, 255).astype(np.uint8))
    return out


class Live:
    """Persistent single-frame DLSS session for realtime preview. init+create the feature
    once, then process() per frame. Style/intensity/local_* apply at the next process;
    changing 'preset' recreates the feature. close() releases the D3D12 device."""
    def __init__(self, w, h, settings=None):
        self._w, self._h = w, h
        self.settings = dict(settings or {})
        self._lib = _load()
        self._open()

    def _open(self):
        s = self.settings
        _set_options(self._lib, s)          # push preset before create
        try:
            self._lib.dlssnr_shutdown()
        except Exception:
            pass
        if not self._lib.dlssnr_init(self._w, self._h, int(s.get('preset', 1)), DLSSNR_DLL, LOG_PATH):
            raise RuntimeError("dlssnr_init failed (D3D12/gate). See dlss_run.log")
        if not self._lib.dlssnr_create_feature(self._w, self._h, int(s.get('preset', 1))):
            log = open(LOG_PATH).read() if os.path.exists(LOG_PATH) else ""
            raise RuntimeError("Feature 18 create failed.\n" + log[-800:])

    def update(self, settings):
        old_preset = self.settings.get('preset')
        self.settings.update(settings)
        if self.settings.get('preset') != old_preset:
            self._open()

    def resize(self, w, h, preset=None):
        """Re-create the Feature 18 for a new frame size WITHOUT re-running the NGX core
        init (which is one-time per process and crashes if re-initialized)."""
        if preset is None:
            preset = int(self.settings.get('preset', 1))
        self.settings['preset'] = preset
        _set_options(self._lib, self.settings)
        if not self._lib.dlssnr_resize(w, h, preset):
            log = open(LOG_PATH).read() if os.path.exists(LOG_PATH) else ""
            raise RuntimeError("Feature 18 resize failed.\n" + log[-800:])
        self._w, self._h = w, h

    def process(self, rgba, reset=False):
        _set_options(self._lib, self.settings)
        h, w = rgba.shape[:2]
        mv = np.zeros((h, w, 2), np.float32)
        dp = np.zeros((h, w), np.float32)
        o = np.zeros_like(rgba)
        ok = self._lib.dlssnr_process(
            rgba.ctypes.data_as(ctypes.c_void_p),
            mv.ctypes.data_as(ctypes.c_void_p),
            dp.ctypes.data_as(ctypes.c_void_p),
            o.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0)
        return o if ok else None

    def close(self):
        try:
            self._lib.dlssnr_shutdown()
        except Exception:
            pass


def run_dlss(rgba_frames, settings=None, reset=True, progress=None):
    """Batch-generate DLSS for a list of HxWx4 rgba frames (zero guidance). Returns HxWx4 list."""
    settings = settings or {}
    lib = _load()
    h, w = rgba_frames[0].shape[:2]
    _set_options(lib, settings)
    if not lib.dlssnr_init(w, h, int(settings.get('preset', 1)), DLSSNR_DLL, LOG_PATH):
        raise RuntimeError("dlssnr_init failed. See dlss_run.log")
    if not lib.dlssnr_create_feature(w, h, int(settings.get('preset', 1))):
        log = open(LOG_PATH).read() if os.path.exists(LOG_PATH) else ""
        raise RuntimeError("Feature 18 create failed.\n" + log[-800:])
    out = []
    for i, rgba in enumerate(rgba_frames):
        mv = np.zeros((h, w, 2), np.float32)
        dp = np.zeros((h, w), np.float32)
        o = np.zeros_like(rgba)
        lib.dlssnr_process(
            rgba.ctypes.data_as(ctypes.c_void_p),
            mv.ctypes.data_as(ctypes.c_void_p),
            dp.ctypes.data_as(ctypes.c_void_p),
            o.ctypes.data_as(ctypes.c_void_p),
            1 if (reset and i == 0) else 0)
        if progress:
            progress(i, len(rgba_frames), "ok")
        out.append(o)
    lib.dlssnr_shutdown()
    view = settings.get('output_view', 0)
    mix = float(settings.get('output_mix', 1.0))
    if view != 0 or mix < 1.0:
        out = _apply_output_view(out, rgba_frames, view, mix, w, h)
    return out


# ===========================================================================
# Engine2 — GPU-direct DLSS Feature 18 host (dlssnr_host2.dll).
#
# Difference vs the legacy host above: this one takes **BGR** (exactly what cv2
# hands us) and can return **RGBA** straight from the GPU readback. That removes
# the per-frame numpy work the legacy path needs:
#     dstack(alpha)      ~3.6 ms/frame  (720p)
#     RGBA->BGR cvtColor ~4.0 ms/frame  (720p)
# and it keeps its D3D12 buffers/command list alive across frames instead of
# rebuilding numpy scratch arrays on every call.
# ===========================================================================
HOST2_DLL = os.path.join(BASE, "dlssnr_host2.dll")
_lib2 = None


def _load2():
    global _lib2
    if _lib2 is None:
        if not os.path.exists(HOST2_DLL):
            raise FileNotFoundError("missing %s" % HOST2_DLL)
        _lib2 = ctypes.CDLL(HOST2_DLL)
        _lib2.dlssnr2_init.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        _lib2.dlssnr2_init.restype = ctypes.c_int
        _lib2.dlssnr2_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _lib2.dlssnr2_process.restype = ctypes.c_int
        _lib2.dlssnr2_process_rgba.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _lib2.dlssnr2_process_rgba.restype = ctypes.c_int
        _lib2.dlssnr2_submit.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib2.dlssnr2_submit.restype = ctypes.c_int
        _lib2.dlssnr2_fetch.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _lib2.dlssnr2_fetch.restype = ctypes.c_int
        _lib2.dlssnr2_pending.argtypes = []
        _lib2.dlssnr2_pending.restype = ctypes.c_int
        _lib2.dlssnr2_drain.argtypes = []
        _lib2.dlssnr2_drain.restype = ctypes.c_int
        _lib2.dlssnr2_set_options.argtypes = [
            ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_int, ctypes.c_int]
        _lib2.dlssnr2_set_options.restype = None
        _lib2.dlssnr2_get_sizes.argtypes = [ctypes.POINTER(ctypes.c_int),
                                            ctypes.POINTER(ctypes.c_int)]
        _lib2.dlssnr2_get_sizes.restype = None
        _lib2.dlssnr2_shutdown.argtypes = []
    return _lib2


class Engine2:
    """GPU-direct Feature 18 (neural render) session.

    process_rgba(bgr) -> HxWx4 uint8 RGBA   (fast path: no Python conversion)
    process(bgr)      -> HxWx3 uint8 BGR    (legacy-compatible)
    """

    def __init__(self):
        self._lib = None
        self._w = self._h = 0
        self._ready = False
        self._settings = {}
        self._out4 = None
        self._out3 = None

    # ---------------------------------------------------------------- setup
    def ensure(self, w, h, settings=None):
        if settings is not None:
            self.set_settings(settings)
        if self._ready and self._w == w and self._h == h:
            return self._lib
        lib = _load2()
        log = os.path.join(BASE, "host2_log.txt")
        if lib.dlssnr2_init(w, h, log) != 1:
            tail = ""
            try:
                tail = open(log, encoding="utf-8", errors="replace").read()[-600:]
            except Exception:
                pass
            raise RuntimeError("dlssnr2_init failed (%dx%d)\n%s" % (w, h, tail))
        self._lib, self._w, self._h, self._ready = lib, w, h, True
        self._out4 = np.zeros((h, w, 4), np.uint8)
        self._out3 = np.zeros((h, w, 3), np.uint8)
        self._push()
        return lib

    def set_settings(self, settings):
        self._settings.update(settings or {})
        if self._ready:
            self._push()

    def _push(self):
        s = self._settings
        self._lib.dlssnr2_set_options(
            int(s.get('style', 0)),
            float(s.get('intensity', 1.0)),
            float(s.get('local_tone', 1.0)),
            float(s.get('local_struct', 1.0)),
            float(s.get('skin_struct', -1.0)),
            int(s.get('use_auto_mask', 0)),
            int(s.get('ui_correction', 0)))

    # ---------------------------------------------------------------- run
    def process_rgba(self, bgr, reset=False):
        """BGR (HxWx3) in -> RGBA (HxWx4) out. No Python-side conversion."""
        h, w = bgr.shape[:2]
        if not (self._ready and self._w == w and self._h == h):
            self.ensure(w, h)
        out = self._out4
        ok = self._lib.dlssnr2_process_rgba(
            bgr.ctypes.data_as(ctypes.c_void_p),
            out.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0)
        return out if ok else None

    def process(self, bgr, reset=False):
        """BGR in -> BGR out (keeps legacy call sites working)."""
        h, w = bgr.shape[:2]
        if not (self._ready and self._w == w and self._h == h):
            self.ensure(w, h)
        out = self._out3
        ok = self._lib.dlssnr2_process(
            bgr.ctypes.data_as(ctypes.c_void_p),
            out.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0)
        return out if ok else None

    # ------------------------------------------------------- pipelined path
    # submit() queues a frame and returns immediately; fetch() waits for the
    # OLDEST queued frame. Because the host keeps two slots, the CPU work for
    # frame N overlaps the GPU pass of frame N-1, which is where the remaining
    # per-frame CPU cost (staging + command recording) gets hidden.
    def submit_rgba(self, bgr, reset=False):
        h, w = bgr.shape[:2]
        if not (self._ready and self._w == w and self._h == h):
            self.ensure(w, h)
        ok = self._lib.dlssnr2_submit(bgr.ctypes.data_as(ctypes.c_void_p),
                                      1 if reset else 0)
        return ok == 1

    def fetch_rgba(self):
        """Return the oldest queued frame as HxWx4 RGBA, or None if nothing is
        in flight. The returned array is REUSED by the next fetch, so consume it
        (e.g. .tobytes()) before fetching again."""
        if not self._ready or self._lib.dlssnr2_pending() <= 0:
            return None
        left = self._lib.dlssnr2_fetch(self._out4.ctypes.data_as(ctypes.c_void_p), None)
        if left < 0:
            return None
        return self._out4

    def pending(self):
        return self._lib.dlssnr2_pending() if self._ready else 0

    def drain(self):
        if self._ready:
            self._lib.dlssnr2_drain()

    def close(self):
        # intentionally does not tear down NGX (unsafe to re-init in-process);
        # the device/feature get reused by the next ensure()
        self._ready = False
        self._w = self._h = 0

    # ------------------------------------------------------ multi-layer chain
    def process_chain(self, bgr, layers, reset=False, out_rgba=False):
        """Run several parameter sets back to back on ONE frame.

        Each layer is a complete round trip (upload -> inference -> readback),
        so N layers costs roughly N times a single pass -- the inference itself
        dominates. The host's channel-count is fixed at 3 for the BGR path, so
        the intermediates ping-pong between two of our own buffers by passing
        both pointers to dlssnr2_process; no Python-side copy is involved. When
        out_rgba is set the LAST layer writes RGBA straight out, so a chain never
        pays an extra colour conversion.

        Caveat worth knowing: all layers share this one NGX feature, i.e. one
        temporal history. So this is "the same network run twice per frame with
        different settings", not two independent filters with separate state.

        Returns the final image (RGBA if out_rgba else BGR, both reusing an
        internal buffer -- consume before the next call), or None on failure.
        """
        layers = [dict(l) for l in (layers or []) if l]
        if not layers:
            return None
        h, w = bgr.shape[:2]
        if not (self._ready and self._w == w and self._h == h):
            self.ensure(w, h)
        if len(layers) == 1:
            self.set_settings(layers[0])
            return (self.process_rgba(bgr, reset=reset) if out_rgba
                    else self.process(bgr, reset=reset))
        if (getattr(self, "_chain_a", None) is None
                or self._chain_a.shape[:2] != (h, w)):
            self._chain_a = np.empty((h, w, 3), np.uint8)
            self._chain_b = np.empty((h, w, 3), np.uint8)
        flag = 1 if reset else 0
        src = bgr
        n = len(layers)
        for i, layer in enumerate(layers):
            self.set_settings(layer)
            if i == n - 1 and out_rgba:
                out = self._out4
                ok = self._lib.dlssnr2_process_rgba(
                    src.ctypes.data_as(ctypes.c_void_p),
                    out.ctypes.data_as(ctypes.c_void_p), flag)
                return out if ok else None
            dst = self._chain_a if (i % 2 == 0) else self._chain_b
            ok = self._lib.dlssnr2_process(
                src.ctypes.data_as(ctypes.c_void_p),
                dst.ctypes.data_as(ctypes.c_void_p), flag)
            if not ok:
                return None
            src = dst            # next layer reads what this one wrote
        return src


class ChainEngine:
    """Adapter so rtx_video.Pipeline can drive a multi-layer chain.

    Pipeline only needs ensure()/process(), so wrapping the layer list here keeps
    rtx_video.py unaware of layering. It also closes a latent bug: the RTX chain
    never pushed its settings to the engine at all, so a queued task with
    different parameters exported with whatever the last preview had left behind.
    Now the job's own settings are applied before its first frame.
    """

    def __init__(self, settings, engine=None):
        self.engine = engine if engine is not None else engine2
        self._wh = None
        self.set_settings(settings)

    def set_settings(self, settings):
        self.settings = dict(settings or {})
        self.layers = [dict(l) for l in (self.settings.get("layers") or []) if l]
        self._wh = None

    def ensure(self, w, h):
        if self._wh != (w, h):
            self.engine.set_settings(self.settings)
            self._wh = (w, h)
        return self.engine.ensure(w, h)

    def process(self, bgr, reset=False):
        if len(self.layers) > 1:
            return self.engine.process_chain(bgr, self.layers, reset=reset)
        return self.engine.process(bgr, reset=reset)

    def pending(self):
        return 0

    def drain(self):
        pass


engine2 = Engine2()

