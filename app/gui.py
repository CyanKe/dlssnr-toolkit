#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — 简约 DLSS5 实时预览 + 导出 (test4)

功能：导入视频 → 实时预览(原图/DLSS/对比) → 调风格/强度/本地色调整/本地结构
      → 逐帧实时看出效果 → 导出 DLSS 视频。

零引导（Feature 18 神经渲染忽略光流/深度），无需 torch/模型，只需 NVIDIA 显卡。
运行： python gui.py

---------------------------------------------------------------------------
来源 / Provenance
    本文件衍生自 purkatyy/DLSS5- (MIT License, Copyright (c) 2026 ylso0)。
    上游原始版本约 571 行；本版本经 Cyanke 大幅改写与扩展（RTX Video 集成、
    预览上限、导出流水线、文件名规则、HDR10 支持等），现约 1500+ 行。
    本文件同样以 MIT 许可发布，完整条款见仓库根目录 LICENSE。

    Derived from purkatyy/DLSS5- (MIT License, Copyright (c) 2026 ylso0).
    Substantially modified and extended by Cyanke. Released under the MIT
    License; see LICENSE at the repository root.
---------------------------------------------------------------------------
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import deque

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import cv2
import numpy as np

import dlss_engine
import rtx_video

VIEWS = ["原图", "DLSS", "对比"]
STYLE_CHOICES = {"默认": 0, "自然": 1, "电影": 2, "风格3": 3}
OUTVIEW_CHOICES = {"处理": 0, "差异×10": 1, "左右对比": 2}

# Short tags for the exported file name, so the suffix says what was actually
# done to the clip rather than a bare "_dlss". Denoise / Deblur / HighBitrate all
# come from the same NVIDIA effect, so the prefix distinguishes them.
STAGE_TAG = {
    1: "VSR-Low", 2: "VSR-Med", 3: "VSR-High", 4: "VSR-Ultra",
    8: "DN-Low", 9: "DN-Med", 10: "DN-High", 11: "DN-Ultra",
    12: "DB-Low", 13: "DB-Med", 14: "DB-High", 15: "DB-Ultra",
    16: "HB-Low", 17: "HB-Med", 18: "HB-High", 19: "HB-Ultra",
}


def size_tag(label):
    """'×1.5' -> 'x1.5', '1080p' -> '1080p' (filename-safe)."""
    t = (label or "").replace("×", "x").replace(" ", "").strip()
    return "".join(c for c in t if c.isalnum() or c in "x.p-")


# ---------------------------------------------------------------------------
# RTX Video HDR contrast / saturation units
#
# The SDK works in 0..200 with 100 meaning "unchanged":
#     nvsdk_ngx_defs_truehdr.h  "0 to 200 for HDR Contrast"
# and nvVFXVideoSuperRes rejects anything outside that (Magpie's
# RTXVideoDenoiser.cpp shows the same check: contrast > 200 -> E_INVALIDARG).
# Magpie's own effect definition also declares MIN 0 / MAX 200 / DEFAULT 100.
#
# NVIDIA's user-facing UI presents that same range relative to neutral, as
# -100%..+100%, which is possible precisely because 0..200 is symmetric about
# 100 (100-100=0, 100+100=200). So:
#     UI -100%  ->  API   0
#     UI    0%  ->  API 100   (neutral)
#     UI +100%  ->  API 200
#
# Only this UI layer speaks percent; rtx_video.py and the C++ host keep working
# in API units, so the conversion lives in exactly one place.
HDR_ADJ_MIN = -100
HDR_ADJ_MAX = 100
HDR_ADJ_NEUTRAL = 100          # API value meaning "no change"


def hdr_adj_to_api(percent):
    """-100..+100 % -> the 0..200 value the SDK expects (clamped)."""
    try:
        v = int(round(float(percent)))
    except Exception:
        v = 0
    return max(0, min(200, HDR_ADJ_NEUTRAL + v))


def hdr_adj_to_ui(api):
    """Inverse of hdr_adj_to_api, for display and round-trip tests."""
    try:
        v = int(round(float(api)))
    except Exception:
        v = HDR_ADJ_NEUTRAL
    return max(HDR_ADJ_MIN, min(HDR_ADJ_MAX, v - HDR_ADJ_NEUTRAL))


def output_stem_suffix(dlss_on, rtx, out_w, out_h, src_w, src_h, size_label=""):
    """Describe the processing chain for the output file name.

    e.g. 'DLSS_VSR-Med_x2_DB-High_HDR10_2560x1440'. The resolution is included
    only when it changed, since that is the part worth seeing at a glance.
    """
    parts = []
    if dlss_on:
        parts.append("DLSS")
    q = int(rtx.get("vsr_quality", 0) or 0)
    if q:
        parts.append(STAGE_TAG.get(q, "VSR"))
        t = size_tag(size_label)
        if t:
            parts.append(t)
    e = int(rtx.get("enhance", 0) or 0)
    if e:
        parts.append(STAGE_TAG.get(e, "FX"))
    if rtx.get("hdr"):
        parts.append("HDR10")
    if (out_w, out_h) != (src_w, src_h):
        parts.append("%dx%d" % (out_w, out_h))
    if not parts:
        return "reencode"                 # nothing enabled: a plain re-encode
    # keep the name usable; drop the resolution before anything else
    if len("_".join(parts)) > 72 and len(parts) > 1:
        parts = [p for p in parts if not (p[0].isdigit() and "x" in p)]
    return "_".join(parts)[:96]

# Preview processing ceiling. The DLSS / RTX passes cost roughly linear time in
# pixel count, and the result is then scaled down to the canvas anyway (~850 px
# wide), so on a 4K source most of the time goes into pixels nobody ever sees.
# Lowering only the PREVIEW resolution is therefore nearly free visually while
# making 4K footage as responsive as 1080p. Export always runs at full size --
# see _export_job, which never consults this.
PREVIEW_CAPS = {"不限制": 0, "720p": 720, "1080p": 1080, "1440p": 1440}
PREVIEW_CAP_DEFAULT = "1080p"

# NVENC is several times faster than the software encoders at 1080p and is what
# makes the GPU-direct path pay off end to end.
#
# Each entry is (ffmpeg encoder, extra args, mp4 codec tag). The tag MUST match the
# codec family or ffmpeg refuses to mux: e.g. labelling an HEVC stream "avc1" makes
# it error out mid-write, which shows up as a hung export. It is spelled out
# explicitly here because deriving it from the encoder name is error-prone
# ("libx265" does not contain "hevc").
ENCODER_CHOICES = {
    "H.265 / NVENC (最快)": ("hevc_nvenc", [
        "-rc", "vbr", "-cq", "23", "-b:v", "0", "-preset", "p4", "-tune", "hq",
        "-spatial-aq", "1", "-temporal-aq", "1", "-multipass", "qres"], "hvc1"),
    "H.264 / NVENC": ("h264_nvenc", [
        "-rc", "vbr", "-cq", "21", "-b:v", "0", "-preset", "p4", "-tune", "hq",
        "-spatial-aq", "1", "-temporal-aq", "1", "-multipass", "qres"], "avc1"),
    "AV1 / NVENC": ("av1_nvenc", [
        "-rc", "vbr", "-cq", "25", "-b:v", "0", "-preset", "p4", "-tune", "hq"],
        "av01"),
    "H.264 / x264 (软编码)": ("libx264", ["-crf", "18", "-preset", "veryfast"],
                              "avc1"),
    # x265 logs heavily even at -loglevel error; silencing it keeps the stderr pipe
    # nearly empty (the drainer thread handles the rest).
    "H.265 / x265 (软编码, 慢)": ("libx265", ["-crf", "22", "-preset", "fast",
                                              "-x265-params", "log-level=error"],
                                  "hvc1"),
}


def encoder_of(label):
    """(encoder, args, tag) for a combo label; falls back to the first entry."""
    return ENCODER_CHOICES.get(label, list(ENCODER_CHOICES.values())[0])


def find_ffmpeg():
    """Locate ffmpeg: PATH -> known dirs -> imageio-ffmpeg bundle."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for c in (r"C:\ffmpeg\bin\ffmpeg.exe",
              r"D:\Programe\ffmpeg-7.1.1-full_build\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"):
        if os.path.isfile(c):
            return c
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None



class App:
    def __init__(self, root):
        self.root = root
        root.title("DLSS5 简约工具 — 实时预览 + 导出")
        # placeholder; _fit_window() replaces this with the measured content size
        root.geometry("860x720")
        self.video = None
        self.nframes = 0
        self.fps = 30.0
        self.vw = self.vh = 0
        self.thread = None
        self.split_x = 0.5
        self.playing = False
        self._exporting = False
        self._live = None
        self._live_cache = None
        self._last_dlss_frame = -1
        self._live_debounce = None
        self._pipe = None            # rtx_video.Pipeline for preview, made lazily

        # ---- video row ----
        t = ttk.Frame(root); t.pack(fill="x", padx=8, pady=6)
        ttk.Button(t, text="导入视频", command=self.import_video).pack(side="left")
        self.vlabel = ttk.Label(t, text="(未选择视频)", anchor="w")
        self.vlabel.pack(side="left", padx=8, fill="x", expand=True)

        # ---- preview canvas (aspect ratio preserved), default view = 对比 ----
        self.canvas = tk.Canvas(root, bg="#161616", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas.bind("<Configure>", lambda e: self.display_view())
        self.canvas.bind("<B1-Motion>", self.on_canvas_motion)
        self.canvas.bind("<Button-1>", self.on_canvas_motion)

        # ---- timeline / scrubbing bar (BELOW the video) ----
        self.fslider = tk.Scale(root, from_=0, to=1, orient="horizontal", showvalue=False,
                                resolution=1, command=lambda e: self.on_frame())
        self.fslider.pack(fill="x", padx=8, pady=(0, 2))
        self.fslider.bind("<Button-1>", self.on_timeline_click)
        self.fslider.bind("<B1-Motion>", self.on_timeline_drag)

        # ---- view + frame + playback (BELOW the video, under the timeline) ----
        v = ttk.Frame(root); v.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(v, text="显示:").pack(side="left")
        self.view_var = tk.StringVar(value="对比")
        self.view_cb = ttk.Combobox(v, textvariable=self.view_var, values=VIEWS, state="readonly", width=7)
        self.view_cb.pack(side="left", padx=3)
        self.view_cb.bind("<<ComboboxSelected>>", lambda e: self.on_view_change())
        ttk.Label(v, text="  帧:").pack(side="left")
        self.fentry = tk.Entry(v, width=7)
        self.fentry.pack(side="left", padx=3)
        self.fentry.insert(0, "0")
        self.fentry.bind("<Return>", self.on_frame_entry)
        self.fentry.bind("<FocusOut>", lambda e: self.sync_frame_entry())
        self.ftotal = ttk.Label(v, text="/ 0")
        self.ftotal.pack(side="left", padx=(0, 6))
        self.play_btn = ttk.Button(v, text="▶ 播放", command=self.toggle_play)
        self.play_btn.pack(side="left", padx=(8, 0))

        # ---- DLSS settings ----
        sf = ttk.LabelFrame(root, text="DLSS 设置")
        sf.pack(fill="x", padx=8, pady=4)
        self._settings = self._build_settings(sf)

        # ---- RTX Video settings (VSR / Deblur / HighBitrate / TrueHDR) ----
        rf = ttk.LabelFrame(root, text="RTX Video 增强")
        rf.pack(fill="x", padx=8, pady=4)
        self._rtx = self._build_rtx_settings(rf)

        # ---- export row ----
        e = ttk.Frame(root); e.pack(fill="x", padx=8, pady=4)
        ttk.Button(e, text="导出 DLSS 视频", command=self.export_dlss).pack(side="left", padx=3)
        ttk.Label(e, text="编码器:").pack(side="left", padx=(10, 2))
        self.enc_var = tk.StringVar(value=list(ENCODER_CHOICES)[0])
        ttk.Combobox(e, textvariable=self.enc_var, values=list(ENCODER_CHOICES),
                     state="readonly", width=22).pack(side="left")

        # ---- progress ----
        p = ttk.Frame(root); p.pack(fill="x", padx=8, pady=2)
        self.pbar = ttk.Progressbar(p, maximum=100)
        self.pbar.pack(fill="x", expand=True)
        self.status = ttk.Label(p, text="就绪")
        self.status.pack(fill="x")
        # second line: speed + elapsed + ETA (kept separate from the frame counter
        # so the counter stays stable while the timing numbers tick)
        self.stats = ttk.Label(p, text="  ", anchor="w")
        self.stats.pack(fill="x")

        # ---- log ----
        self.log = scrolledtext.ScrolledText(root, height=7, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=False, padx=8, pady=4)

        # ---- spacebar toggles play/pause (anywhere in the window, except text fields) ----
        self.root.bind_all("<space>", self.on_space)

        self._refresh_rtx_note()
        self._fit_window()

    # ---------- helpers ----------
    def _fit_window(self):
        """Size the window to fit its content.

        The stats line (speed / elapsed / ETA) is the last thing packed, so when
        the window is shorter than the content needs it is squeezed to a few
        pixels and effectively disappears. That is exactly what happened once the
        RTX Video panel was added: the content grew to ~846 px while the window
        stayed at the hard-coded 720, leaving the stats label 5 px tall instead
        of its normal 21.

        Hard-coding a taller window would just break again on the next added row,
        so the requested size is measured and clamped to the screen. If the screen
        cannot fit everything, space is reclaimed in order of least importance --
        log lines, then video area, then the log entirely -- because the progress
        block is the part that must survive.
        """
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        avail_h = max(sh - 90, 460)

        log_lines = int(self.log.cget("height"))
        line_h = max(self.log.winfo_reqheight() // max(log_lines, 1), 1)
        # Tracked with an explicit flag rather than winfo_ismapped(): this runs
        # before the window is mapped, where winfo_ismapped() is 0 for every
        # widget and would wrongly report the log as hidden.
        hidden = {"log": False}

        def hide_log():
            self.log.pack_forget()
            hidden["log"] = True

        # Applied only while the content is still too tall for the screen.
        reductions = [
            lambda: self.log.config(height=4),
            lambda: self.canvas.config(height=180),
            lambda: self.log.config(height=3),
            lambda: self.canvas.config(height=120),
            hide_log,
        ]
        for reduce in reductions:
            self.root.update_idletasks()
            if self.root.winfo_reqheight() <= avail_h:
                break
            try:
                reduce()
            except Exception:
                pass
        self.root.update_idletasks()
        log_hidden = hidden["log"]

        need_w = self.root.winfo_reqwidth()
        need_h = self.root.winfo_reqheight()
        w = min(max(need_w, 860), max(sw - 60, 640))
        h = min(max(need_h, 460), avail_h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, max((sw - w) // 2, 0),
                                            max((sh - h) // 3, 0)))
        # Floor: the progress block must stay visible, so the user cannot drag the
        # window down to the point where the numbers vanish again. The log is the
        # only thing allowed to give up space.
        if log_hidden:
            min_h = need_h
        else:
            cur = int(self.log.cget("height"))
            min_h = need_h - max(cur - 3, 0) * line_h
        self.root.minsize(min(760, w), min(min_h, avail_h))
    def set_status(self, msg):
        try:
            self.status.config(text=msg); self.root.update_idletasks()
        except Exception:
            pass

    def logln(self, msg):
        try:
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n"); self.log.see("end")
            self.log.config(state="disabled")
        except Exception:
            pass

    def set_progress(self, i, total, extra=""):
        try:
            if total:
                self.pbar["maximum"] = total
                self.pbar["value"] = i
                self.set_status(f"{extra} {i}/{total}")
            self.root.update_idletasks()
        except Exception:
            pass

    @staticmethod
    def _fmt_dur(sec):
        """Seconds -> 'M:SS' (or 'H:MM:SS' when it gets long)."""
        try:
            sec = float(sec)
        except Exception:
            return "--:--"
        if sec < 0 or sec != sec or sec == float("inf"):   # negative / NaN / inf
            return "--:--"
        sec = int(round(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def set_stats(self, done=None, total=None, fps=None, elapsed=None, eta=None):
        """Second status line: processing speed, elapsed, remaining and total ETA."""
        try:
            parts = []
            if fps is not None and fps > 0:
                parts.append(f"速度: {fps:.1f} fps ({1000.0 / fps:.0f} ms/帧)")
            elif elapsed is not None:
                parts.append("速度: 测速中…")      # first frame: no interval yet
            if elapsed is not None:
                parts.append(f"已用: {self._fmt_dur(elapsed)}")
            if eta is not None:
                parts.append(f"预计剩余: {self._fmt_dur(eta)}")
            if elapsed is not None and eta is not None:
                parts.append(f"预计总耗时: {self._fmt_dur(elapsed + eta)}")
            if total:
                parts.append(f"总帧数: {total}")
            self.stats.config(text="    ".join(parts) if parts else "  ")
        except Exception:
            pass

    def clear_stats(self):
        try:
            self.stats.config(text="  ")
        except Exception:
            pass

    def _read_frame(self, frame):
        """Decode one frame by index, with a one-frame cache.

        Sequential access is far cheaper than seeking: measured at 1280x720,
        cap.read() straight through costs 1.3 ms/frame, while
        cap.set(CAP_PROP_POS_FRAMES) before every read costs 23.4 ms/frame --
        17.8x slower. For H.264 an arbitrary seek has to locate the preceding
        keyframe and decode forward, and keyframe intervals are commonly 1-10 s.

        Playback asks for consecutive frames, so only seek when the caller
        actually jumps (scrubbing, stepping, a new video). That is precisely why
        export was always fast and the preview was not: export reads the capture
        sequentially, the preview went through here.

        The cache matters because the split view asks for the SAME frame twice --
        once for 原图 and once inside the DLSS stage. Without it the second
        request counts as a jump and pays the full seek again.
        """
        cap = getattr(self, "_cap", None)
        if cap is None:
            return None
        cached = getattr(self, "_fr_cache", None)
        if cached is not None and frame == getattr(self, "_cap_frame", None):
            return cached
        last = getattr(self, "_cap_frame", None)
        if last is None or frame != last + 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, f = cap.read()
        if ok:
            self._cap_frame = frame
            self._fr_cache = f
            return f
        self._cap_frame = None
        self._fr_cache = None
        return None

    # ---------- preview ----------
    def _build_settings(self, parent):
        d = {}
        d['v_dlss'] = tk.IntVar(value=1)
        d['v_style'] = tk.StringVar(value="默认")
        d['v_intensity'] = tk.DoubleVar(value=1.0)
        d['v_local_tone'] = tk.DoubleVar(value=1.0)
        d['v_local_struct'] = tk.DoubleVar(value=1.0)
        d['v_outview'] = tk.StringVar(value="处理")
        d['v_outmix'] = tk.DoubleVar(value=1.0)
        d['v_preview_cap'] = tk.StringVar(value=PREVIEW_CAP_DEFAULT)
        S = tk.Scale
        # Controls that only mean something while the neural pass runs. They get
        # dimmed when it is switched off, rather than being silently ignored.
        dimmed = []

        ttk.Checkbutton(parent, text="启用 DLSS 处理", variable=d['v_dlss'],
                        command=self.on_dlss_toggle
                        ).grid(row=0, column=0, columnspan=2, sticky="w",
                               padx=(6, 2), pady=2)

        ttk.Label(parent, text="风格:").grid(row=0, column=2, sticky="e", padx=(6, 2))
        w = ttk.Combobox(parent, textvariable=d['v_style'], values=list(STYLE_CHOICES),
                         state="readonly", width=8)
        w.grid(row=0, column=3, padx=(0, 8)); dimmed.append(w)

        ttk.Label(parent, text="强度:").grid(row=0, column=4, sticky="e", padx=(6, 2))
        w = S(parent, from_=0, to=1, resolution=0.05, orient="horizontal", showvalue=True,
              variable=d['v_intensity'], length=100)
        w.grid(row=0, column=5, padx=(0, 8)); dimmed.append(w)

        ttk.Label(parent, text="本地色调:").grid(row=0, column=6, sticky="e", padx=(6, 2))
        w = S(parent, from_=0, to=1, resolution=0.05, orient="horizontal", showvalue=True,
              variable=d['v_local_tone'], length=90)
        w.grid(row=0, column=7, padx=(0, 8)); dimmed.append(w)

        ttk.Label(parent, text="本地结构:").grid(row=0, column=8, sticky="e", padx=(6, 2))
        w = S(parent, from_=0, to=1, resolution=0.05, orient="horizontal", showvalue=True,
              variable=d['v_local_struct'], length=90)
        w.grid(row=0, column=9, padx=(0, 8)); dimmed.append(w)

        ttk.Label(parent, text="输出视图:").grid(row=1, column=0, sticky="e", padx=(6, 2))
        w = ttk.Combobox(parent, textvariable=d['v_outview'], values=list(OUTVIEW_CHOICES),
                         state="readonly", width=9)
        w.grid(row=1, column=1, padx=(0, 8)); dimmed.append(w)

        ttk.Label(parent, text="输出混合:").grid(row=1, column=2, sticky="e", padx=(6, 2))
        w = S(parent, from_=0, to=1, resolution=0.05, orient="horizontal", showvalue=True,
              variable=d['v_outmix'], length=100)
        w.grid(row=1, column=3, padx=(0, 8)); dimmed.append(w)

        d['hint'] = ttk.Label(parent, text="", foreground="#666")
        d['hint'].grid(row=1, column=4, columnspan=6, sticky="w", padx=(6, 2))

        # Preview-only working-resolution ceiling. Deliberately NOT in `dimmed`:
        # it still helps when DLSS is off but an RTX stage is on.
        ttk.Label(parent, text="预览处理上限:").grid(row=2, column=0, sticky="e",
                                                 padx=(6, 2), pady=2)
        w = ttk.Combobox(parent, textvariable=d['v_preview_cap'],
                         values=list(PREVIEW_CAPS), state="readonly", width=8)
        w.grid(row=2, column=1, padx=(0, 8), sticky="w")
        w.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())
        ttk.Label(parent, text="(超过时先缩小再处理，只影响预览；导出始终用原始分辨率)",
                  foreground="#666").grid(row=2, column=2, columnspan=8,
                                          sticky="w", padx=(6, 2))
        d['_dimmed'] = dimmed

        for child in dimmed:
            if isinstance(child, tk.Scale):
                child.config(command=lambda e: self.on_settings_change())
            elif isinstance(child, ttk.Combobox):
                child.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())
        return d

    def on_dlss_toggle(self):
        """Dim the neural-pass controls while DLSS is switched off."""
        on = bool(self._settings['v_dlss'].get())
        for w in self._settings.get('_dimmed', []):
            try:
                if on:
                    # a Combobox's enabled state is "readonly", not "normal"
                    w.config(state="readonly" if isinstance(w, ttk.Combobox) else "normal")
                else:
                    w.config(state="disabled")
            except Exception:
                pass
        self.on_settings_change()

    def _dlss_enabled(self):
        return bool(self._settings['v_dlss'].get())

    def _collect_settings(self):
        d = self._settings
        return {
            'dlss': int(d['v_dlss'].get()),
            'style': STYLE_CHOICES.get(d['v_style'].get(), 0),
            'intensity': float(d['v_intensity'].get()),
            'local_tone': float(d['v_local_tone'].get()),
            'local_struct': float(d['v_local_struct'].get()),
            'output_view': OUTVIEW_CHOICES.get(d['v_outview'].get(), 0),
            'output_mix': float(d['v_outmix'].get()),
        }

    # ---------- RTX Video settings ----------
    def _build_rtx_settings(self, parent):
        d = {}
        d['v_vsr'] = tk.StringVar(value="关闭")
        d['v_size'] = tk.StringVar(value="×2")
        d['v_enh'] = tk.StringVar(value="关闭")
        d['v_hdr'] = tk.IntVar(value=0)
        d['v_hdr_gray'] = tk.IntVar(value=50)
        d['v_hdr_nits'] = tk.IntVar(value=1000)
        d['v_hdr_contrast'] = tk.IntVar(value=0)      # percent, neutral = 0
        d['v_hdr_sat'] = tk.IntVar(value=0)

        ttk.Label(parent, text="AI 放大:").grid(row=0, column=0, sticky="e", padx=(6, 2), pady=2)
        ttk.Combobox(parent, textvariable=d['v_vsr'], values=list(rtx_video.VSR_CHOICES),
                     state="readonly", width=10).grid(row=0, column=1, padx=(0, 8), sticky="w")
        ttk.Label(parent, text="目标:").grid(row=0, column=2, sticky="e", padx=(6, 2))
        ttk.Combobox(parent, textvariable=d['v_size'], values=rtx_video.SIZE_CHOICES,
                     state="readonly", width=7).grid(row=0, column=3, padx=(0, 8), sticky="w")
        ttk.Label(parent, text="降噪/去模糊/高码率:").grid(row=0, column=4, sticky="e", padx=(6, 2))
        ttk.Combobox(parent, textvariable=d['v_enh'], values=list(rtx_video.ENHANCE_CHOICES),
                     state="readonly", width=12).grid(row=0, column=5, padx=(0, 8), sticky="w")

        ttk.Checkbutton(parent, text="SDR → HDR10 输出",
                        variable=d['v_hdr'], command=self.on_settings_change
                        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=(6, 2), pady=2)
        ttk.Label(parent, text="中灰:").grid(row=1, column=2, sticky="e", padx=(6, 2))
        S = tk.Scale
        S(parent, from_=10, to=100, orient="horizontal", showvalue=True,
          variable=d['v_hdr_gray'], length=84).grid(row=1, column=3, padx=(0, 8))
        ttk.Label(parent, text="峰值亮度:").grid(row=1, column=4, sticky="e", padx=(6, 2))
        S(parent, from_=400, to=2000, resolution=50, orient="horizontal", showvalue=True,
          variable=d['v_hdr_nits'], length=110).grid(row=1, column=5, padx=(0, 8))
        # Contrast / saturation are shown as NVIDIA does: a relative -100%..+100%
        # around neutral, converted to the SDK's 0..200 in _rtx_settings().
        ttk.Label(parent, text="对比%:").grid(row=1, column=6, sticky="e", padx=(6, 2))
        S(parent, from_=HDR_ADJ_MIN, to=HDR_ADJ_MAX, orient="horizontal", showvalue=True,
          variable=d['v_hdr_contrast'], length=84).grid(row=1, column=7, padx=(0, 8))
        ttk.Label(parent, text="饱和%:").grid(row=1, column=8, sticky="e", padx=(6, 2))
        S(parent, from_=HDR_ADJ_MIN, to=HDR_ADJ_MAX, orient="horizontal", showvalue=True,
          variable=d['v_hdr_sat'], length=84).grid(row=1, column=9, padx=(0, 8))

        d['note'] = ttk.Label(parent, text="", foreground="#666")
        d['note'].grid(row=2, column=0, columnspan=10, sticky="w", padx=6, pady=(2, 2))

        for child in parent.winfo_children():
            if isinstance(child, tk.Scale):
                child.config(command=lambda e: self.on_settings_change())
            elif isinstance(child, ttk.Combobox):
                child.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())
        return d

    def _rtx_settings(self):
        """RTX Video settings as the keys rtx_video.Pipeline expects."""
        d = self._rtx
        return {
            'vsr_quality': rtx_video.VSR_CHOICES.get(d['v_vsr'].get(), 0),
            'enhance': rtx_video.ENHANCE_CHOICES.get(d['v_enh'].get(), 0),
            'hdr': int(d['v_hdr'].get()),
            'hdr_format': 1,                      # R10: the verified HDR10 path
            'hdr_middle_gray': int(d['v_hdr_gray'].get()),
            'hdr_max_luminance': int(d['v_hdr_nits'].get()),
            # percent -> API units; everything downstream stays in API units
            'hdr_contrast': hdr_adj_to_api(d['v_hdr_contrast'].get()),
            'hdr_saturation': hdr_adj_to_api(d['v_hdr_sat'].get()),
        }

    def _rtx_size_label(self):
        return self._rtx['v_size'].get()

    def _settings_hash(self):
        s = self._collect_settings()
        return (s['dlss'], s['style'], s['intensity'], s['local_tone'],
                s['local_struct'], self._preview_cap())

    # ---------- preview working resolution ----------
    def _preview_cap(self):
        """Height ceiling for preview processing; 0 means unlimited."""
        return PREVIEW_CAPS.get(self._settings['v_preview_cap'].get(),
                                PREVIEW_CAPS[PREVIEW_CAP_DEFAULT])

    def _preview_scale(self, w, h, rtx):
        """Working size for the preview, plus the factor to apply to pipeline
        target sizes.

        Returns (pw, ph, k). k == 1.0 means "process at the source size".

        Only ever shrinks, so anything at or below the ceiling is processed
        untouched -- which is the requested behaviour: above 1080p becomes
        1080p, below stays as it is.

        The ceiling is judged on what comes OUT of the chain, not on the source,
        because a VSR stage can enlarge it: a 4K source with x2 upscaling would
        otherwise still preview at 8K. Scaling the input by the same factor keeps
        the final stage at the ceiling.
        """
        cap = self._preview_cap()
        if cap <= 0:
            return w, h, 1.0
        final_h = h
        if rtx_video.is_vsr(int(rtx.get('vsr_quality', 0))):
            final_h = int(rtx.get('out_h', h) or h)
        if final_h <= cap or w <= 0 or h <= 0:
            return w, h, 1.0
        k = cap / float(final_h)
        pw = max(2, int(round(w * k)))
        ph = max(2, int(round(h * k)))
        # even dimensions: yuv420 chroma is subsampled and some GPU paths prefer it
        return pw - (pw % 2), ph - (ph % 2), k

    @staticmethod
    def _scale_targets(rtx, k):
        """Copy of `rtx` with any VSR target size scaled by k and made even."""
        if k == 1.0:
            return rtx
        out = dict(rtx)
        for key in ("out_w", "out_h"):
            if out.get(key):
                v = max(2, int(round(int(out[key]) * k)))
                out[key] = v - (v % 2)
        return out

    def _ensure_engine(self, w, h):
        """One shared GPU-direct Feature 18 session for preview + export.

        dlss_engine.engine2 takes BGR (what cv2 gives us) and returns RGBA straight
        from the GPU readback, so no numpy channel juggling is needed per frame.
        """
        try:
            eng = dlss_engine.engine2
            eng.set_settings(self._collect_settings())
            if getattr(self, "_eng_wh", None) != (w, h):
                eng.ensure(w, h)
                self._eng_wh = (w, h)
                self._last_dlss_frame = -1
            return eng
        except Exception as ex:
            self.logln("[DLSS] " + str(ex))
            return None

    def _close_live(self):
        self._live_cache = None
        self._last_dlss_frame = -1

    def _live_dlss_image(self, frame):
        """Frame -> RGBA image (HxWx4) produced on the GPU, or None."""
        sk = (self._settings_hash(), tuple(sorted(self._rtx_settings().items())))
        if self._live_cache and self._live_cache[0] == frame and self._live_cache[1] == sk:
            return self._live_cache[2]
        fr = self._read_frame(frame)
        if fr is None:
            return None
        h, w = fr.shape[:2]
        rtx = self._rtx_settings()
        use_dlss = self._dlss_enabled()

        # Cap the working resolution (preview only -- export is untouched).
        pw, ph, k = self._preview_scale(w, h, rtx)
        if (pw, ph) != (w, h):
            fr = cv2.resize(fr, (pw, ph), interpolation=cv2.INTER_AREA)

        # A size change invalidates the neural pass's temporal state.
        if getattr(self, "_preview_wh", None) != (pw, ph):
            self._preview_wh = (pw, ph)
            self._last_dlss_frame = -1
        reset = 0 if frame == self._last_dlss_frame + 1 else 1

        o = None
        if rtx_video.Pipeline.needs_rtx(rtx) or not use_dlss:
            # Preview through the full RTX chain. HDR output is shown after a
            # simple display mapping so it is at least viewable; the preview is
            # not a colour-managed reference.
            try:
                if self._pipe is None:
                    self._pipe = rtx_video.Pipeline()
                pw2, ph2, _ = self._pipe.configure(
                    pw, ph, self._scale_targets(rtx, k),
                    dlss_engine=dlss_engine.engine2 if use_dlss else None)
                payload, kind = self._pipe.process(fr, reset=bool(reset))
                if kind == "bgr":
                    bgr = np.frombuffer(payload, np.uint8).reshape(ph2, pw2, 3)
                    o = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                elif kind == "r10":
                    packed = np.frombuffer(payload, np.uint32).reshape(ph2, pw2)
                    r = ((packed >> 0) & 0x3FF).astype(np.float32) / 1023.0
                    g = ((packed >> 10) & 0x3FF).astype(np.float32) / 1023.0
                    b = ((packed >> 20) & 0x3FF).astype(np.float32) / 1023.0
                    o = np.clip(np.dstack([r, g, b]) * 255.0, 0, 255).astype(np.uint8)
                    o = cv2.cvtColor(o, cv2.COLOR_RGB2RGBA)
                else:
                    f16 = np.frombuffer(payload, np.float16).reshape(ph2, pw2, 4)
                    lin = np.nan_to_num(f16[..., :3].astype(np.float32), nan=0.0)
                    disp = np.clip(np.power(np.clip(lin, 0, 1), 1 / 2.2) * 255.0,
                                   0, 255).astype(np.uint8)
                    o = cv2.cvtColor(disp, cv2.COLOR_RGB2RGBA)
            except Exception as ex:
                self.logln("[RTX 预览] " + str(ex))
                return None
        else:
            eng = self._ensure_engine(pw, ph)
            if eng is None:
                return None
            o = eng.process_rgba(fr, reset=reset)

        self._last_dlss_frame = frame
        if o is None:
            self._live_cache = None
            return None
        rgba = o.copy()          # the engine reuses its output buffer -> copy to cache
        self._live_cache = (frame, sk, rgba)
        return rgba

    def load_view_img(self, view, frame):
        """Both views are returned as RGBA so downstream code needs no conversion."""
        if view == "原图":
            bgr = self._read_frame(frame)
            if bgr is None:
                return None
            # Match the DLSS side's working resolution. The split view scales both
            # sides to the same canvas size, so comparing a full-resolution
            # original against a capped-resolution processed frame would show up
            # as a resolution difference rather than the processing difference.
            pw, ph, _ = self._preview_scale(bgr.shape[1], bgr.shape[0],
                                             self._rtx_settings())
            if (pw, ph) != (bgr.shape[1], bgr.shape[0]):
                bgr = cv2.resize(bgr, (pw, ph), interpolation=cv2.INTER_AREA)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
        if view == "DLSS":
            return self._live_dlss_image(frame)
        return None

    def display_view(self):
        if not self.video or getattr(self, "_exporting", False):
            return
        frame = int(self.fslider.get())
        view = self.view_var.get()
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width() or 780, 200)
        ch = max(self.canvas.winfo_height() or 400, 150)
        if view == "对比":
            self._draw_split(frame, cw, ch); return
        img = self.load_view_img(view, frame)
        if img is None:
            msg = f"{view}：帧 {frame} 读取失败" if view == "原图" else f"DLSS：帧 {frame} 生成失败"
            self.canvas.create_text(cw // 2, ch // 2, text=msg, fill="#888888", font=("Microsoft YaHei", 11))
            return
        self._draw_fit(img, cw, ch)

    def _draw_fit(self, img, cw, ch):
        ih, iw = img.shape[:2]
        scale = min(cw / iw, ch / ih)
        nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        nimg = cv2.resize(img, (nw, nh))
        from PIL import Image, ImageTk
        # images arrive as RGBA (straight from the GPU); PIL wants RGB
        self._pilimg = Image.fromarray(cv2.cvtColor(nimg, cv2.COLOR_RGBA2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.delete("all")
        self.canvas.create_image((cw - nw) // 2, (ch - nh) // 2, anchor="nw", image=self._photo)

    def _draw_split(self, frame, cw, ch):
        if getattr(self, "_split_frame", -1) != frame or getattr(self, "_split_size", None) != (cw, ch):
            orig = self.load_view_img("原图", frame)
            dlss = self.load_view_img("DLSS", frame)
            if orig is None:
                self.canvas.create_text(cw // 2, ch // 2, text=f"帧 {frame} 读取失败", fill="#888"); return
            if dlss is None:
                self._draw_fit(orig, cw, ch)
                self.canvas.create_text(cw // 2, 16, text="DLSS 生成失败", fill="#888"); return
            ih, iw = orig.shape[:2]
            scale = min(cw / iw, ch / ih)
            self._split_nw, self._split_nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
            self._split_orig = cv2.resize(orig, (self._split_nw, self._split_nh))
            self._split_dlss = cv2.resize(dlss, (self._split_nw, self._split_nh))
            self._split_frame = frame; self._split_size = (cw, ch)
        nw, nh = self._split_nw, self._split_nh
        o = self._split_orig.copy()
        sx = int(self.split_x * nw)
        o[:, sx:] = self._split_dlss[:, sx:]
        o[:, max(sx - 1, 0):min(sx + 1, nw)] = [0, 255, 255, 255]   # RGBA divider
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        self._drag_nw = nw; self._drag_offsetx = ox
        from PIL import Image, ImageTk
        self._pilimg = Image.fromarray(cv2.cvtColor(o, cv2.COLOR_RGBA2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)

    def on_canvas_motion(self, event):
        if self.view_var.get() != "对比":
            return
        if not hasattr(self, "_drag_nw") or not hasattr(self, "_drag_offsetx"):
            return
        frac = (event.x - self._drag_offsetx) / max(self._drag_nw, 1)
        self.split_x = max(0.0, min(1.0, frac))
        self.display_view()

    # ---------- frame / view ----------
    def on_frame(self):
        self.sync_frame_entry()
        self.display_view()

    def _timeline_value_from_x(self, x):
        f = int(self.fslider.cget("from")); t = int(self.fslider.cget("to"))
        if t <= f:
            return f
        try:
            x0 = self.fslider.coords(f)[0]
            x1 = self.fslider.coords(t)[0]
        except Exception:
            x0, x1 = 10.0, max(self.fslider.winfo_width() - 10.0, 11.0)
        span = x1 - x0
        if span <= 0:
            return f
        frac = max(0.0, min(1.0, (x - x0) / span))
        return int(round(f + frac * (t - f)))

    def on_timeline_click(self, event):
        self.fslider.set(self._timeline_value_from_x(event.x))
        self.on_frame()

    def on_timeline_drag(self, event):
        self.on_timeline_click(event)

    def on_frame_entry(self, event=None):
        txt = self.fentry.get().strip()
        try:
            f = int(float(txt))
        except ValueError:
            f = None
        if f is None:
            self.sync_frame_entry(); return
        to = int(self.fslider.cget("to"))
        f = max(0, min(to, f))
        self.fslider.set(f)
        self.on_frame()

    def sync_frame_entry(self):
        try:
            txt = str(int(self.fslider.get()))
            if self.fentry.get().strip() != txt:
                self.fentry.delete(0, "end")
                self.fentry.insert(0, txt)
        except Exception:
            pass

    def on_view_change(self):
        if self.view_var.get() == "对比":
            self.split_x = 0.5
        self.display_view()

    def on_settings_change(self, event=None):
        if self._live_debounce:
            self.root.after_cancel(self._live_debounce)
        self._live_debounce = self.root.after(60, self._refresh_dlss)

    def _refresh_rtx_note(self):
        """Explain what the current DLSS + RTX Video selection will do.

        Creating an RTX Video session costs a full model load (~13 s), so the
        note warns about that rather than letting it look like a hang.
        """
        h = self._settings.get('hint')
        if h is not None:
            msg = ("(风格/强度/本地色调/本地结构会实时生效)"
                   if self._dlss_enabled() else "(DLSS 已关闭)")
            if self.video and self.vw and self.vh:
                pw, ph, k = self._preview_scale(self.vw, self.vh, self._rtx_settings())
                if k != 1.0:
                    msg += "   预览 %d×%d → %d×%d" % (self.vw, self.vh, pw, ph)
            h.config(text=msg)
        lbl = self._rtx.get('note')
        if lbl is None:
            return
        s = self._rtx_settings()
        parts = []
        if s['vsr_quality']:
            if self.video:
                ow, oh = rtx_video.target_size(self._rtx_size_label(), self.vw, self.vh)
                parts.append("放大 → %dx%d（%s）"
                             % (ow, oh, rtx_video.QUALITY_NAMES[s['vsr_quality']]))
            else:
                parts.append("放大：%s" % rtx_video.QUALITY_NAMES[s['vsr_quality']])
        if s['enhance']:
            parts.append(rtx_video.QUALITY_NAMES[s['enhance']])
        if s['hdr']:
            if rtx_video.truehdr.available():
                parts.append("SDR → HDR10（中灰 %d，峰值 %d nits）"
                             % (s['hdr_middle_gray'], s['hdr_max_luminance']))
            else:
                parts.append("HDR 不可用（此显卡/驱动不支持 TrueHDR）")
        if not self._dlss_enabled():
            parts.insert(0, "DLSS 关闭")
        if parts:
            lbl.config(text="  启用：" + " + ".join(parts) + "    (首次启用需加载模型，约 13 秒)")
        else:
            lbl.config(text="  当前仅使用 DLSS（GPU 直连路径，最快）")

    def _refresh_dlss(self):
        self._live_debounce = None
        try:
            dlss_engine.engine2.set_settings(self._collect_settings())
        except Exception as ex:
            self.logln("[DLSS 参数] " + str(ex))
        self._refresh_rtx_note()
        self._live_cache = None
        self._split_frame = -1
        if self.view_var.get() in ("DLSS", "对比"):
            self.display_view()
    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def on_space(self, event=None):
        # If a text field or a button has focus, let IT handle the space (type a space /
        # activate the button) — don't hijack it. Otherwise space toggles play/pause.
        w = self.root.focus_get()
        if w is not None:
            cls = w.winfo_class()
            if cls in ("Entry", "TEntry", "Text", "Combobox", "TCombobox", "Spinbox", "TSpinbox",
                       "Button", "TButton", "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton"):
                return None
        self.toggle_play()
        return "break"

    def _set_play_btn(self, playing):
        try:
            self.play_btn.config(text="⏸ 暂停" if playing else "▶ 播放")
        except Exception:
            pass

    def play(self):
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频"); return
        self.playing = True
        self._set_play_btn(True)
        if getattr(self, "_play_after", None):
            self.root.after_cancel(self._play_after)
        self._play_after = None
        # fresh pacing state: leftovers from a previous run would make the first
        # few frames use a stale overhead estimate
        self._play_start = None
        self._play_overhead = 0.0
        self._play_work = 0.0
        self._play_delay = 0
        self._play()

    def _play(self):
        if not self.playing:
            return
        now = time.perf_counter()
        nxt = int(self.fslider.get()) + 1
        if nxt > int(self.fslider.cget("to")):
            nxt = 0
        self.fslider.set(nxt)
        self.on_frame()
        work_ms = (time.perf_counter() - now) * 1000.0

        # Pacing. root.after() starts its countdown only after this method
        # returns, so a fixed delay yields a period of work + delay: 12 ms of
        # work on 30 fps content gave 22 fps, not 30.
        #
        # Beyond our own work there is also the cost of Tk actually rendering
        # the new image, which happens in the event loop after we return and so
        # cannot be timed directly here. Instead it is MEASURED: comparing when
        # this frame started against when the previous one did gives the real
        # period, and whatever is unaccounted for is the overhead. Subtracting
        # that converges on the video's own frame rate.
        target_ms = 1000.0 / max(self.fps, 1.0)
        overhead = getattr(self, "_play_overhead", 0.0)
        prev = getattr(self, "_play_start", None)
        if prev is not None:
            actual = (now - prev) * 1000.0
            measured = (actual - getattr(self, "_play_work", 0.0)
                        - getattr(self, "_play_delay", 0.0))
            # clamp: a spike (GC, a resize) must not push the next delay to 0
            overhead = max(0.0, min(measured, target_ms))
        self._play_overhead = overhead
        self._play_start = now
        self._play_work = work_ms

        delay = int(round(target_ms - work_ms - overhead))
        delay = max(4, min(delay, 250))       # 4 ms floor caps the redraw rate
        self._play_delay = delay
        self._play_after = self.root.after(delay, self._play)

    def pause(self):
        self.playing = False
        self._set_play_btn(False)
        if getattr(self, "_play_after", None):
            self.root.after_cancel(self._play_after)
            self._play_after = None

    # ---------- import ----------
    def import_video(self):
        p = filedialog.askopenfilename(filetypes=[("视频", "*.mp4 *.avi *.mov *.mkv"), ("所有文件", "*.*")])
        if not p:
            return
        self.pause()
        # Do NOT tear the engine down here: the NGX core init is one-time per
        # process. _ensure_engine() reuses the same session for a same-size video and
        # rebuilds only the feature + textures when the resolution changes.
        self._live_cache = None
        self._last_dlss_frame = -1
        self._split_frame = -1
        self.video = os.path.abspath(p)
        if getattr(self, "_cap", None):
            self._cap.release()
        self._cap = cv2.VideoCapture(self.video)
        self._cap_frame = None        # new capture: nothing decoded yet
        self._fr_cache = None
        n, fps, w, h = self._video_info(self.video)
        self.nframes, self.fps = n, fps
        self.vw, self.vh = w, h
        self.vlabel.config(text=f"{os.path.basename(self.video)}  ({n} 帧 @ {fps:.0f}fps {w}x{h})")
        self.fslider.config(to=max(n - 1, 1))
        self.fslider.set(0)
        self.ftotal.config(text=f"/ {max(n - 1, 1)}")
        self.sync_frame_entry()
        try:
            self.display_view()
        except Exception as ex:
            self.logln(f"[preview] {ex}")
        self.logln(f"已导入: {self.video}  ({n} 帧)")
        self._refresh_rtx_note()
        self.set_status("就绪")
        self.clear_stats()          # drop the previous export's timing line

    @staticmethod
    def _video_info(path):
        cap = cv2.VideoCapture(path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return n, fps, w, h

    # ---------- export ----------
    @staticmethod
    def _unique_path(path):
        """First free name of the form stem-1.ext, stem-2.ext, ..."""
        stem, ext = os.path.splitext(path)
        for i in range(1, 1000):
            cand = "%s-%d%s" % (stem, i, ext)
            if not os.path.exists(cand):
                return cand
        return "%s-%d%s" % (stem, int(time.time()), ext)

    def _confirm_overwrite(self, path):
        """Ask before replacing an existing file.

        Returns (proceed, final_path); final_path may differ when the user picks
        auto-rename. Must run on the main thread -- the export worker cannot open
        dialogs, which is why this happens before the thread starts.
        """
        if not os.path.exists(path):
            return True, path
        ans = messagebox.askyesnocancel(
            "文件已存在",
            os.path.basename(path) + "\n\n"
            "是 = 覆盖它\n"
            "否 = 自动改名（加 -1、-2 …）\n"
            "取消 = 不导出")
        if ans is None:
            return False, path
        if ans:
            return True, path
        return True, self._unique_path(path)

    def export_dlss(self):
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频"); return
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("忙", "上一个任务还没结束"); return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("缺少 ffmpeg",
                "未找到 ffmpeg。\n请安装 ffmpeg 并加入 PATH，或 pip install imageio-ffmpeg。")
            return
        settings = self._collect_settings()
        settings.update(self._rtx_settings())
        enc, enc_args, tag = encoder_of(self.enc_var.get())
        n, fps, w, h = self._video_info(self.video)
        s = self._rtx_settings()
        ow, oh = w, h
        if s['vsr_quality']:
            ow, oh = rtx_video.target_size(self._rtx_size_label(), w, h)
            settings['out_w'], settings['out_h'] = ow, oh

        suffix = output_stem_suffix(self._dlss_enabled(), s, ow, oh, w, h,
                                    self._rtx_size_label())
        out_path = os.path.splitext(self.video)[0] + "_" + suffix + ".mp4"

        # Never silently overwrite. The worker thread cannot show dialogs, so the
        # question is asked here on the main thread before it starts.
        proceed, out_path = self._confirm_overwrite(out_path)
        if not proceed:
            self.set_status("已取消")
            self.logln("已取消导出（目标文件已存在）")
            return

        self._exporting = True
        self.pbar["maximum"] = max(n, 1); self.pbar["value"] = 0
        self.set_status("导出中...")
        self.set_stats(total=n)          # show the frame count before the first tick
        chain = []
        if self._dlss_enabled():
            chain.append("DLSS")
        if s['vsr_quality']:
            chain.append(rtx_video.QUALITY_NAMES[s['vsr_quality']])
        if s['enhance']:
            chain.append(rtx_video.QUALITY_NAMES[s['enhance']])
        if s['hdr']:
            chain.append("TrueHDR")
        self.logln(f"导出: {enc}  {w}x{h} → {ow}x{oh} @ {fps:.2f}fps  {n} 帧")
        self.logln(f"  输出: {os.path.basename(out_path)}")
        self.logln(f"  处理链: {' + '.join(chain) if chain else '无（仅重新编码）'}")
        # The worker thread must never touch Tk: calling root.after()/widget methods
        # off the main thread makes Tcl block, which cost ~1.3 s per progress update.
        # Report through a plain Queue and let the main thread poll it instead.
        self._prog_q = queue.Queue()
        self.thread = threading.Thread(
            target=self._export_job,
            args=(ffmpeg, enc, enc_args, tag, out_path, settings, n, fps, w, h),
            daemon=True)
        self.thread.start()
        self.root.after(100, self._poll_progress)

    def _poll_progress(self):
        """Main-thread progress pump (drains the worker's queue)."""
        done = False
        try:
            while True:
                msg = self._prog_q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, i, total, speed, elapsed, eta = msg
                    self.set_progress(i, total, "导出")
                    self.set_stats(done=i, total=total, fps=speed,
                                   elapsed=elapsed, eta=eta)
                elif kind == "log":
                    self.logln(str(msg[1]))
                elif kind == "done":
                    _, ok, out_path = msg
                    done = True
                    self._exporting = False
                    self.pbar["value"] = 0
                    if ok:
                        self.set_status("完成")
                        self.logln("已导出: " + out_path)
                        messagebox.showinfo("导出", "已导出:\n" + out_path)
                    else:
                        self.set_status("导出失败")
                        self.clear_stats()
                        messagebox.showerror("导出失败", "导出失败，详见日志。")
        except queue.Empty:
            pass
        if not done:
            self.root.after(100, self._poll_progress)

    def _export_job(self, ffmpeg, enc, enc_args, tag, out_path, settings, n, fps, w, h):
        """Pipelined export: decode -> [RTX Video stages] -> DLSS -> ffmpeg stdin.

        Two paths, chosen by whether any RTX Video stage is on:

        * plain DLSS (default): unchanged, and the fastest. Frames leave Python as
          RGBA straight from the GPU readback and ffmpeg consumes -pix_fmt rgba,
          so there is no CPU channel conversion anywhere.
        * with RTX stages: the chain runs synchronously per frame and ends in BGR,
          or in 10-bit HDR data when TrueHDR is on.

        An HDR export additionally needs 10-bit 4:2:0 and PQ / BT.2020 tagging.
        The TrueHDR R10 output is already PQ-encoded, so it is fed straight in
        with no colour conversion. Its pixel format is x2bgr10le, not
        x2rgb10le -- DXGI's R10G10B10A2 puts R in the low bits, so the BGR-named
        ffmpeg format is the one that matches (verified with pure-colour probes).
        """
        import time
        view = int(settings['output_view']); mix = float(settings['output_mix'])
        use_dlss = bool(settings.get('dlss', 1))
        # The chained path is used whenever any RTX stage is on, and also when
        # DLSS is switched off entirely (a pure pass-through re-encode).
        chained = rtx_video.Pipeline.needs_rtx(settings) or not use_dlss

        pipe = None
        out_w, out_h = w, h
        in_fmt = "rgba"
        if chained:
            try:
                pipe = rtx_video.Pipeline()
                out_w, out_h, in_fmt = pipe.configure(
                    w, h, settings,
                    dlss_engine=dlss_engine.engine2 if use_dlss else None)
            except Exception as ex:
                self._prog_q.put(("log", "[RTX Video] " + str(ex)))
                self._prog_q.put(("done", False, out_path))
                return
            stages = [x for x in [
                rtx_video.QUALITY_NAMES.get(int(settings.get('vsr_quality', 0)), "")
                if int(settings.get('vsr_quality', 0)) else "",
                "DLSS" if use_dlss else "",
                rtx_video.QUALITY_NAMES.get(int(settings.get('enhance', 0)), "")
                if int(settings.get('enhance', 0)) else "",
                "TrueHDR" if settings.get('hdr') else ""] if x]
            self._prog_q.put(("log", "处理链路: %s → %dx%d (%s)"
                              % (" + ".join(stages) if stages else "无处理（仅重新编码）",
                                 out_w, out_h, in_fmt)))

        hdr = bool(settings.get('hdr', 0))
        peak = int(settings.get('hdr_max_luminance', 1000))
        vf_args = []
        if hdr:
            # 10-bit 4:2:0 plus PQ / BT.2020 signalling. Without the tags players
            # treat the file as SDR and it looks washed out.
            #
            # The tags go through the setparams FILTER, not the -color_primaries
            # output options: hevc_nvenc ignores those and would write only the
            # matrix (measured: bt2020nc / unknown / unknown). setparams stamps
            # the frames themselves, which every encoder then passes into the
            # VUI, and it works for x265 too.
            vf_args = ["-vf",
                       "setparams=color_primaries=bt2020:color_trc=smpte2084"
                       ":colorspace=bt2020nc",
                       "-pix_fmt", "yuv420p10le"]
            enc_args = list(enc_args)
            if enc in ("hevc_nvenc", "av1_nvenc"):
                enc_args += ["-profile:v", "main10"]
            elif enc == "libx265":
                # x265 additionally writes the HDR10 static metadata (mastering
                # display + MaxCLL), which nvenc cannot emit at all.
                enc_args += ["-x265-params",
                             "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
                             ":master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                             "WP(15635,16450)L(%d,%d)"
                             ":max-cll=%d,%d:log-level=error"
                             % (peak * 10000, peak * 100, peak, peak // 4)]
            elif enc == "libx264":
                enc_args += ["-profile:v", "high10"]
        else:
            vf_args = ["-pix_fmt", "yuv420p"]

        args = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
                "-i", self.video,
                "-f", "rawvideo", "-pix_fmt", in_fmt, "-s", f"{out_w}x{out_h}",
                "-r", f"{fps:.6f}", "-i", "pipe:0",
                "-map", "1:v:0", "-map", "0:a?", "-c:a", "copy",
                "-c:v", enc] + list(enc_args) + vf_args + [
                "-movflags", "+faststart", "-tag:v", tag,
                "-threads", "0", "-shortest", out_path]
        proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        write_q = queue.Queue(maxsize=4)
        state = {"err": None, "done_write": False}

        # ffmpeg's stderr MUST be drained continuously. Some encoders (libx265 in
        # particular) log heavily even at -loglevel error; if nobody reads the pipe
        # it fills up (~64 KB), the encoder blocks writing to it, ffmpeg stops
        # consuming stdin, and the writer thread deadlocks -> export hangs forever.
        stderr_chunks = []
        stderr_bytes = [0]

        def stderr_loop():
            try:
                while True:
                    chunk = proc.stderr.read(8192)
                    if not chunk:
                        break
                    if stderr_bytes[0] < 128 * 1024:      # keep the tail, cap memory
                        stderr_chunks.append(chunk)
                        stderr_bytes[0] += len(chunk)
            except Exception:
                pass

        st_err_t = threading.Thread(target=stderr_loop, daemon=True)
        st_err_t.start()

        def writer_loop():
            try:
                while True:
                    b = write_q.get()
                    if b is None:
                        break
                    proc.stdin.write(b)
            except Exception as ex:
                state["err"] = str(ex)
            finally:
                state["done_write"] = True

        wt = threading.Thread(target=writer_loop, daemon=True)
        wt.start()

        eng = None
        i = 0
        t0 = time.time()
        err_msg = None
        emitted = 0
        # timing stats for the progress line: the clock starts at the first emitted
        # frame so engine init / first-frame latency does not poison the estimate
        t_stat0 = None
        last_report = 0.0
        recent_t = deque(maxlen=60)      # completion time of each recent frame

        def report_progress(force=False):
            """Called after every emitted frame; throttled to ~5 UI updates/sec.

            recent_t must receive EVERY frame (not just the reported ones), otherwise
            the sliding window would treat 5-frame gaps as 1-frame gaps and report a
            speed 5x too low.
            """
            nonlocal t_stat0, last_report
            now = time.time()
            if t_stat0 is None:
                t_stat0 = now
            recent_t.append(now)

            if not n:
                self._prog_q.put(("progress", emitted, n, None, None, None))
                return
            if not force and (now - last_report) < 0.2 and emitted < n:
                return
            last_report = now

            elapsed = max(now - t_stat0, 1e-6)
            # sliding-window speed: responsive to current conditions.
            # Needs >= 2 samples, else there is no interval to divide by (a single
            # sample would divide by ~0 and print an absurd number).
            span = recent_t[-1] - recent_t[0]
            if len(recent_t) >= 2 and span > 1e-6:
                speed = (len(recent_t) - 1) / span
            else:
                speed = None
            # ETA from the overall average: stable and monotone, unlike the window
            avg = emitted / elapsed
            eta = (n - emitted) / avg if avg > 1e-6 else None
            self._prog_q.put(("progress", emitted, n, speed, elapsed, eta))

        def emit(o, src_bgr, idx):
            """Post-process one finished frame and hand it to the writer.

            The plain DLSS path works in RGBA; the RTX path ends in BGR (or in
            raw HDR bytes when TrueHDR is on, in which case there is nothing to
            post-process and it is passed through untouched).
            """
            if pipe is not None:
                try:
                    payload, kind = pipe.process(src_bgr, reset=(idx == 0))
                except Exception as ex:
                    raise RuntimeError("RTX Video 处理失败: %s" % ex)
                if kind != "bgr" or view == 0:
                    write_q.put(payload)
                    return
                o = cv2.cvtColor(np.frombuffer(payload, np.uint8).reshape(
                    out_h, out_w, 3), cv2.COLOR_BGR2RGBA)
            if view == 0 and mix >= 1.0:
                frame = o                      # zero conversions
            else:
                src = cv2.cvtColor(src_bgr, cv2.COLOR_BGR2RGBA)
                if src.shape[0] != o.shape[0]:
                    src = cv2.resize(src, (o.shape[1], o.shape[0]),
                                     interpolation=cv2.INTER_CUBIC)
                if view == 1:
                    d = cv2.absdiff(o, src)
                    d = cv2.multiply(d, 10, dst=d)
                    cv2.add(d, 128, dst=d)
                    frame = d
                elif view == 2:
                    frame = o.copy()
                    frame[:, :o.shape[1] // 2] = src[:, :o.shape[1] // 2]
                else:
                    frame = cv2.addWeighted(o, mix, src, 1.0 - mix, 0)
            # fetch reuses its buffer, so serialise immediately
            write_q.put(frame.tobytes())

        try:
            cap = cv2.VideoCapture(self.video)
            if pipe is not None:
                # Chained path: every stage must run in order per frame, and the
                # RTX stages own their own GPU state, so frames go through
                # synchronously rather than through the DLSSNR double buffer.
                while True:
                    ok, fr = cap.read()
                    if not ok:
                        break
                    try:
                        emit(None, fr, i)
                    except Exception as ex:
                        err_msg = "帧 %d: %s" % (i, ex)
                        break
                    emitted += 1
                    if state["err"]:
                        err_msg = "ffmpeg 管道中断: " + state["err"]
                        break
                    if proc.poll() is not None:
                        tail = b"".join(stderr_chunks).decode("utf-8", "replace")[-600:]
                        err_msg = (f"ffmpeg 提前退出 (code={proc.returncode})，"
                                   f"已处理 {emitted} 帧\n" + tail)
                        break
                    report_progress()
                    i += 1
                cap.release()
                if err_msg is None:
                    report_progress(force=True)
            else:
                # One source frame is kept alive so the post-processing of frame
                # N-1 can run while frame N is already on the GPU (double
                # buffering).
                prev_fr = None
                prev_idx = -1
                while True:
                    ok, fr = cap.read()
                    if not ok:
                        break
                    if eng is None:
                        eng = dlss_engine.engine2
                        eng.set_settings(settings)
                        eng.ensure(w, h)
                    if not eng.submit_rgba(fr, reset=(i == 0)):
                        err_msg = f"帧 {i}: 提交失败，终止导出"
                        break
                    if prev_fr is not None:
                        o = eng.fetch_rgba()
                        if o is None:
                            err_msg = f"帧 {prev_idx}: 取回失败，终止导出"
                            break
                        emit(o, prev_fr, prev_idx)
                        emitted += 1
                        if state["err"]:
                            err_msg = "ffmpeg 管道中断: " + state["err"]
                            break
                        # If ffmpeg died early (bad args, unsupported codec, bad
                        # tag) keep going and the writer blocks forever. Catch it
                        # right away and surface its stderr instead of hanging.
                        if proc.poll() is not None:
                            tail = b"".join(stderr_chunks).decode("utf-8", "replace")[-600:]
                            err_msg = (f"ffmpeg 提前退出 (code={proc.returncode})，"
                                       f"已处理 {emitted} 帧\n" + tail)
                            break
                        report_progress()
                    prev_fr = fr
                    prev_idx = i
                    i += 1
                cap.release()

                # flush the pipeline: the last frame is still on the GPU
                if err_msg is None and eng is not None:
                    while True:
                        o = eng.fetch_rgba()
                        if o is None:
                            break
                        emit(o, prev_fr, prev_idx)
                        emitted += 1
                        if state["err"]:
                            err_msg = "ffmpeg 管道中断: " + state["err"]
                            break
                        report_progress(force=(emitted >= n))
                # final update with the exact totals
                if err_msg is None:
                    report_progress(force=True)
        except Exception as ex:
            traceback.print_exc()
            err_msg = "导出错误: " + str(ex)
        finally:
            write_q.put(None)
            wt.join(timeout=60)
            try:
                proc.stdin.close()
            except Exception:
                pass
            code = proc.wait()
            st_err_t.join(timeout=5)          # let the stderr drainer finish
            err = b"".join(stderr_chunks)
            ok = (code == 0)
            if not ok:
                err_msg = f"ffmpeg 退出码 {code}: " + err.decode("utf-8", "replace")[-800:]
            if err_msg:
                self._prog_q.put(("log", err_msg))
            self._prog_q.put(("done", ok, out_path))

    def _iter_frames(self):
        cap = cv2.VideoCapture(self.video)
        i = 0
        while True:
            ok, f = cap.read()
            if not ok:
                break
            yield i, f
            i += 1
        cap.release()


def main():
    if "--selftest" in sys.argv:
        # headless sanity check of the GPU-direct host (writes _selftest.txt)
        import dlss_engine
        try:
            f = np.full((360, 640, 3), 90, np.uint8)
            cv2.circle(f, (320, 180), 80, (255, 0, 0), -1)
            eng = dlss_engine.engine2
            eng.set_settings({'style': 1, 'intensity': 0.9})
            o = eng.process_rgba(f, reset=True)
            ok = "DLSS_OK " + (str(o.shape) if o is not None else "None")
        except Exception as e:
            ok = "DLSS_FAIL " + repr(e)[:300]
        try:
            outdir = os.path.dirname(os.path.abspath(sys.argv[0]))
            with open(os.path.join(outdir, "_selftest.txt"), "w", encoding="utf-8") as fh:
                fh.write(ok)
        except Exception:
            pass
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
