#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — DLSS5 神经渲染预览与导出

界面：单窗口深色主题，顶部四个分页（预览 / 导出 / 转换队列 / 日志）。
      处理参数是一块浮在预览画面上的可拖动卡片；导入界面像 HandBrake 那样
      盖在主窗口上，不另开窗口。预览支持 原图 / DLSS / 分屏 / 并排 与缩放平移。

功能：导入视频 → 实时预览 → 调风格/强度/本地色调/本地结构 → 逐帧看出效果
      → 导出 DLSS 视频，或把多个视频连同各自的参数快照攒成队列批量转换。

零引导（Feature 18 神经渲染忽略光流/深度），无需 torch/模型，只需 NVIDIA 显卡。
运行： python gui.py            （--no-picker 空窗口启动；也可直接给视频路径）

---------------------------------------------------------------------------
来源 / Provenance
    本文件衍生自 purkatyy/DLSS5- (MIT License, Copyright (c) 2026 ylso0)。
    上游原始版本约 571 行；本版本经 Cyanke 大幅改写与扩展（RTX Video 集成、
    预览上限、导出流水线、文件名规则、HDR10 支持、转换队列与本次界面改版）。
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

VIEWS = ["原图", "DLSS", "分屏", "并排"]
# 旧名"对比" == 现在的"分屏"，仅做兼容（比如用户习惯、旧文档）。
VIEW_ALIAS = {"对比": "分屏"}
ZOOM_DEFAULT = "适应窗口"

# Mouse-wheel zoom ladder. Deliberately does NOT contain 适应窗口: fit is a
# one-off action (the 复位 button or the dropdown), and having the wheel land on
# it made zooming feel broken -- scrolling down from 25% landed on "fit", which
# at that point was zooming back IN. Steps are geometric-ish so each notch is a
# similar visual change instead of one huge 25% jump.
ZOOM_LADDER = [0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.33, 0.5, 0.67,
               0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]


def zoom_label(ratio):
    return "%d%%" % int(round(ratio * 100))


ZOOM_CHOICES = [ZOOM_DEFAULT] + [zoom_label(r) for r in ZOOM_LADDER]

# Width of the gutter between the two halves of the 并排 view.
SIDE_GAP = 8

# One DLSSNR layer: the parameters the network consumes, with the same meaning
# and defaults as a single-pass setup. Multi-layer runs a LIST of these in order.
LAYER_KEYS = ("style", "intensity", "local_tone", "local_struct",
              "skin_struct", "use_auto_mask")


def default_layer():
    return {"style": 0, "intensity": 1.0, "local_tone": 1.0,
            "local_struct": 1.0, "skin_struct": 0.0, "use_auto_mask": 0}
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


VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".m2ts", ".ts",
              ".wmv", ".flv", ".webm")
VIDEO_FILETYPES = [("视频", "*.mp4 *.avi *.mov *.mkv *.m2ts *.ts *.wmv *.flv *.webm"),
                   ("所有文件", "*.*")]


# ---------------------------------------------------------------------------
# Modern dark theme
#
# ttk's native Windows theme cannot be recoloured and looks two decades old, so
# everything runs on the "clam" theme (fully styleable) plus classic tk widgets
# (Frame / Label / Scale / Listbox / Entry) whose colours we set directly. Every
# colour comes from this palette, so retuning the whole look is one edit.
# ---------------------------------------------------------------------------
C_BG        = "#17171C"   # window + top bar
C_PANEL     = "#212129"   # cards and every control background
C_PANEL_HI  = "#2B2B35"   # hover
C_BORDER    = "#34343F"
C_TEXT      = "#EAEAEF"
C_TEXT_DIM  = "#9A9AA6"
C_ACCENT    = "#E5484D"
C_ACCENT_HI = "#F2555A"
C_OK        = "#46C46A"
C_WARN      = "#E5A83B"
C_ERR       = "#E5544E"
C_CANVAS    = "#0D0D11"   # the video stage: darker than the chrome
C_TROUGH    = "#12121A"

FONT_UI    = ("Microsoft YaHei UI", 9)
FONT_SMALL = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_H2    = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO  = ("Cascadia Mono", 9)


def flat_button(parent, text, command=None, accent=False):
    """A flat, hover-highlighted button.

    ttk.Button keeps the OS chrome (bevels, native colours) and tk.Button only
    drops it with relief=flat / bd=0, which is what this wraps -- plus the hover
    feedback ttk would have given us. Keeps handles for set_button_enabled().
    """
    base = C_ACCENT if accent else C_PANEL
    hover = C_ACCENT_HI if accent else C_PANEL_HI
    b = tk.Button(parent, text=text, command=command,
                  bg=base, fg="#FFFFFF" if accent else C_TEXT,
                  activebackground=hover,
                  activeforeground="#FFFFFF" if accent else C_TEXT,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  font=FONT_UI, padx=13, pady=5)
    b._base_bg = base
    b._hover_bg = hover
    b.bind("<Enter>", lambda e: b.config(bg=hover)
           if str(b["state"]) != "disabled" else None)
    b.bind("<Leave>", lambda e: b.config(bg=base)
           if str(b["state"]) != "disabled" else None)
    return b


def set_button_enabled(btn, enabled):
    """Enable / disable a flat_button, keeping the colours readable."""
    try:
        if enabled:
            btn.config(state="normal", bg=btn._base_bg, cursor="hand2")
        else:
            btn.config(state="disabled", bg=C_PANEL, cursor="arrow")
    except Exception:
        pass


def slider_row(parent, row, label, var, lo, hi, res, fmt, on_change=None,
               dimmed=None, width=168, col0=0, updaters=None):
    """One `label  [slider]  value` line on a dark card.

    ttk.Scale (not tk.Scale: see the style note in _init_theme) plus our own
    value label, because a ttk scale has no showvalue box and the built-in one
    is painted in the widget's own colours anyway. ttk.Scale is continuous, so
    the value is snapped to `res` on every callback -- otherwise an HDR slider
    would sit at 49.7 and truncate to 49 on export.
    """
    tk.Label(parent, text=label, bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL,
             anchor="w").grid(row=row, column=col0, sticky="w",
                              padx=(12, 6), pady=3)
    s = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal", variable=var,
                  length=width, style="Card.Horizontal.TScale")
    s.grid(row=row, column=col0 + 1, sticky="w", pady=3)
    val = tk.Label(parent, text=fmt % float(var.get()), bg=C_PANEL, fg=C_TEXT,
                   font=FONT_SMALL, width=5, anchor="e")
    val.grid(row=row, column=col0 + 2, sticky="w", padx=(4, 10), pady=3)

    busy = {"b": False}

    def _upd(_=None):
        if busy["b"]:
            return
        try:
            v = float(var.get())
        except Exception:
            return
        if res:
            snapped = round((v - lo) / res) * res + lo
            if abs(snapped - v) > 1e-9:
                busy["b"] = True
                try:
                    var.set(snapped)
                finally:
                    busy["b"] = False
        try:
            val.config(text=fmt % float(var.get()))
        except Exception:
            pass
        if on_change:
            try:
                on_change()
            except Exception:
                pass

    s.config(command=_upd)
    if dimmed is not None:
        dimmed.append(s)
    if updaters is not None:
        updaters.append(_upd)
    return s


def list_videos_in_folder(folder):
    """Flat scan for playable videos (no recursion, like a source picker)."""
    try:
        names = sorted(os.listdir(folder))
    except Exception:
        return []
    out = []
    for n in names:
        p = os.path.join(folder, n)
        try:
            if os.path.isfile(p) and n.lower().endswith(VIDEO_EXTS):
                out.append(p)
        except Exception:
            pass
    return out


class ImportOverlay(tk.Frame):
    """HandBrake-style source picker, drawn OVER the main window.

    It is a Frame placed with place(relwidth=1, relheight=1) inside the root --
    NOT a Toplevel. The tool is visible behind it from the very first paint and
    dismissing the overlay simply reveals it: no second window, no
    withdraw/deiconify dance, and no way to end up waiting on a dialog that
    never maps. Chosen paths land in `result` and are handed to `on_done`.
    """

    def __init__(self, master, on_done):
        super().__init__(master, bg=C_BG)
        self.on_done = on_done
        self.result = []
        self._done = False

        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=36, pady=(30, 0))
        tk.Label(head, text="选择源视频", bg=C_BG, fg=C_TEXT, font=FONT_TITLE,
                 anchor="w").pack(anchor="w")
        tk.Label(head,
                 text="单个文件、整个文件夹，或把文件拖放到此处。"
                      "全部进入可切换的文件列表，第一个立即预览。",
                 bg=C_BG, fg=C_TEXT_DIM, font=FONT_UI, anchor="w"
                 ).pack(anchor="w", pady=(6, 16))

        row = tk.Frame(self, bg=C_BG)
        row.pack(fill="x", padx=36)
        flat_button(row, "＋ 打开文件", self._pick_file, accent=True).pack(side="left")
        flat_button(row, "打开文件夹", self._pick_folder).pack(side="left", padx=(8, 0))

        # drop target: a big outlined box, like HandBrake's
        box = tk.Frame(self, bg=C_PANEL, highlightbackground=C_BORDER,
                       highlightthickness=1)
        box.pack(fill="both", expand=True, padx=36, pady=16)
        self.drop = tk.Listbox(box, selectmode="extended", bg=C_PANEL, fg=C_TEXT,
                               relief="flat", bd=0, highlightthickness=0,
                               activestyle="none", font=FONT_UI,
                               selectbackground=C_ACCENT,
                               selectforeground="#FFFFFF")
        self.drop.pack(fill="both", expand=True, padx=14, pady=14)
        self.drop.insert("end", "   或将视频文件 / 文件夹拖放到此处…")

        self._dnd = False
        try:
            from tkinterdnd2 import DND_FILES
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
            self._dnd = True
        except Exception:
            self._dnd = False

        foot = tk.Frame(self, bg=C_BG)
        foot.pack(fill="x", padx=36, pady=(0, 26))
        self.status = tk.Label(foot, text="", bg=C_BG, fg=C_TEXT_DIM,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        flat_button(foot, "打开选中", self._confirm, accent=True).pack(side="right")
        flat_button(foot, "取消", self._cancel).pack(side="right", padx=(0, 8))

    # -- lifecycle --
    def show(self):
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()
        try:
            self.focus_set()
        except Exception:
            pass

    # -- actions --
    def _pick_file(self):
        p = filedialog.askopenfilename(filetypes=VIDEO_FILETYPES)
        if p:
            self.result = [os.path.abspath(p)]
            self._finish()

    def _pick_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        vids = list_videos_in_folder(folder)
        if not vids:
            self.status.config(text="该文件夹下没有可识别的视频文件")
            return
        # let the user pick which ones (multi-select in the listbox)
        self.drop.delete(0, "end")
        for p in vids:
            self.drop.insert("end", p)
        self.status.config(text=f"文件夹中共 {len(vids)} 个视频，可多选后点“打开选中”")

    def _on_drop(self, event):
        paths = self.tk.splitlist(getattr(event, "data", "") or "")
        vids = []
        for p in paths:
            p = os.path.abspath(p.strip("{}"))
            if os.path.isdir(p):
                vids.extend(list_videos_in_folder(p))
            elif os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS):
                vids.append(p)
        if not vids:
            self.status.config(text="拖放的内容中没有可识别的视频")
            return
        self.result = vids
        self._finish()

    def _confirm(self):
        sel = list(self.drop.curselection())
        if not sel:
            # click-through: if the list holds real paths, take them all
            paths = [self.drop.get(i) for i in range(self.drop.size())]
            paths = [p for p in paths if os.path.isfile(p)]
            if not paths:
                self.status.config(text="请先选择一个视频文件")
                return
            self.result = [os.path.abspath(p) for p in paths]
        else:
            paths = [self.drop.get(i) for i in sel]
            paths = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
            if not paths:
                self.status.config(text="选中的不是有效文件")
                return
            self.result = paths
        self._finish()

    def _cancel(self):
        self.result = []
        self._finish()

    def _finish(self):
        if self._done:
            return
        self._done = True
        try:
            self.place_forget()
            self.destroy()
        except Exception:
            pass
        try:
            self.on_done(list(self.result or []))
        except Exception:
            pass



class App:
    def __init__(self, root):
        self.root = root
        root.title("DLSS5 工具 — 神经渲染预览与导出")
        # placeholder; _fit_window() picks the real size once the shell exists
        root.geometry("1180x760")
        self.video = None
        self.nframes = 0
        self.fps = 30.0
        self.vw = self.vh = 0
        self.thread = None
        self.split_x = 0.5
        self.playing = False
        # ---- conversion queue (HandBrake-style) ----
        # Each task snapshots its own params, so every video can convert with
        # different settings. The worker runs in the background; while a task
        # is actively on the GPU (_queue_active) the preview shows the
        # original frame only -- engine2 / vfx / truehdr are process-wide
        # singletons, and interleaving two videos would corrupt both the
        # export output and the temporal state. Pause the queue to get the
        # live DLSS preview back instantly.
        self._queue = []
        self._queue_seq = 0
        self._queue_running = False
        self._queue_active = False
        self._queue_current = None
        self._queue_stop = None
        self._queue_pause = None
        # Every file imported in one go becomes a switchable source (a folder
        # import can bring in dozens); source_i is the one on the stage.
        self.sources = []
        self.source_i = -1
        # ---- multi-layer DLSSNR -------------------------------------------
        # A chain of parameter sets, applied in order to every frame. The
        # on-screen sliders always edit the SELECTED layer; _layers mirrors them.
        self._layers = [default_layer()]
        self._layer_i = 0
        # fit = 整帧适应窗口；float = 缩放倍率；拖动平移偏移（显示坐标）
        self.zoom_var = None
        self.zoom_ratio = None
        self.pan_x = 0
        self.pan_y = 0
        self._pan_drag = None
        self._exporting = False
        self._export_opts = None   # built with the export section (see below)
        self._live = None
        self._live_cache = None
        self._last_dlss_frame = -1
        self._live_debounce = None
        self._pipe = None            # rtx_video.Pipeline for preview, made lazily

        # ---- theme, then the app shell -----------------------------------
        self._init_theme()
        root.configure(bg=C_BG)

        self._tab = "preview"
        self._tabs = {}
        self._pages = {}
        self._overlay = None

        # ---- top bar: brand + tabs + import -------------------------------
        topbar = tk.Frame(root, bg=C_BG, height=50)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        tk.Label(topbar, text="DLSS5", bg=C_BG, fg=C_ACCENT,
                 font=FONT_TITLE).pack(side="left", padx=(16, 4), pady=9)
        self.vlabel = tk.Label(topbar, text="未选择视频", bg=C_BG, fg=C_TEXT_DIM,
                               font=FONT_SMALL, anchor="e")
        self.vlabel.pack(side="right", padx=(8, 6), pady=14)
        flat_button(topbar, "＋ 导入视频", self.import_batch, accent=True
                    ).pack(side="right", padx=(8, 16), pady=10)

        self._tabbar = tk.Frame(topbar, bg=C_BG)
        self._tabbar.pack(side="left", padx=(20, 0))
        for key, text in (("preview", "预览"), ("export", "导出 / 队列"),
                          ("log", "日志")):
            self._make_tab(self._tabbar, key, text)

        tk.Frame(root, bg=C_BORDER, height=1).pack(fill="x", side="top")

        # ---- bottom status bar (visible on every tab) ----------------------
        self._build_statusbar(root)

        # ---- stacked pages: only the active tab is raised ------------------
        content = tk.Frame(root, bg=C_BG)
        content.pack(fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        for key in ("preview", "export", "log"):
            pg = tk.Frame(content, bg=C_BG)
            pg.grid(row=0, column=0, sticky="nsew")
            self._pages[key] = pg

        self._build_preview_page(self._pages["preview"])
        self._build_export_page(self._pages["export"])
        self._build_log_page(self._pages["log"])

        self._select_tab("preview")

        # ---- spacebar toggles play/pause (anywhere, except text fields) ----
        self.root.bind_all("<space>", self.on_space)
        # Ctrl+Left / Ctrl+Right walk the imported file list
        self.root.bind_all("<Control-Left>", lambda e: self._source_key(-1))
        self.root.bind_all("<Control-Right>", lambda e: self._source_key(1))
        self._enable_main_dnd()

        self._refresh_rtx_note()
        self._fit_window()

    # ---------- theme ----------
    def _init_theme(self):
        """Configure the one style set the whole app draws from.

        'clam' is the only built-in theme whose colours are fully honoured on
        Windows; the native one ignores background/foreground entirely, which
        is why ttk normally looks like Windows 95 next to a dark UI.
        """
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass

        st.configure(".", background=C_PANEL, foreground=C_TEXT,
                     fieldbackground=C_PANEL, bordercolor=C_BORDER,
                     lightcolor=C_PANEL, darkcolor=C_PANEL,
                     troughcolor=C_TROUGH, focuscolor=C_ACCENT, font=FONT_UI)

        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)

        # checkbuttons: dark box + accent tick
        for name, bg in (("TCheckbutton", C_PANEL), ("Bar.TCheckbutton", C_BG)):
            st.configure(name, background=bg, foreground=C_TEXT,
                         focuscolor=bg, indicatorcolor=C_PANEL,
                         bordercolor=C_BORDER)
            st.map(name,
                   background=[("active", bg)],
                   foreground=[("disabled", C_TEXT_DIM), ("active", C_TEXT)],
                   indicatorcolor=[("selected", C_ACCENT),
                                   ("!selected", C_TROUGH)])

        st.configure("TCombobox", fieldbackground=C_PANEL, background=C_PANEL,
                     foreground=C_TEXT, arrowcolor=C_TEXT_DIM,
                     bordercolor=C_BORDER, lightcolor=C_PANEL,
                     darkcolor=C_PANEL, selectbackground=C_ACCENT,
                     selectforeground="#FFFFFF", padding=3)
        st.map("TCombobox",
               fieldbackground=[("readonly", C_PANEL), ("disabled", C_BG)],
               background=[("active", C_PANEL_HI)],
               foreground=[("disabled", C_TEXT_DIM)])
        # the dropdown itself is a plain Tk listbox -> colour it via options
        self.root.option_add("*TCombobox*Listbox.background", C_PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        st.configure("Horizontal.TProgressbar", background=C_ACCENT,
                     troughcolor=C_TROUGH, bordercolor=C_TROUGH,
                     lightcolor=C_ACCENT, darkcolor=C_ACCENT, thickness=6)

        # Scales: classic tk.Scale paints its thumb with the widget background,
        # so a dark thumb disappears into a dark trough. ttk's slider element
        # takes its colour from the style background instead, which lets the
        # thumb be light while the card stays dark.
        st.configure("Card.Horizontal.TScale", background=C_TEXT_DIM,
                     troughcolor=C_TROUGH, bordercolor=C_BORDER,
                     lightcolor="#F2F2F6", darkcolor=C_TEXT_DIM,
                     sliderlength=16, sliderthickness=13)
        st.configure("Rail.Horizontal.TScale", background=C_TEXT_DIM,
                     troughcolor=C_PANEL, bordercolor=C_BORDER,
                     lightcolor="#F2F2F6", darkcolor=C_TEXT_DIM,
                     sliderlength=14, sliderthickness=15)

        st.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL,
                     foreground=C_TEXT, bordercolor=C_BORDER, rowheight=27,
                     font=FONT_UI)
        st.configure("Treeview.Heading", background=C_BG,
                     foreground=C_TEXT_DIM, relief="flat", font=FONT_SMALL,
                     padding=6)
        st.map("Treeview",
               background=[("selected", C_ACCENT)],
               foreground=[("selected", "#FFFFFF")])
        st.map("Treeview.Heading", background=[("active", C_PANEL_HI)])

        st.configure("Vertical.TScrollbar", background=C_PANEL,
                     troughcolor=C_BG, bordercolor=C_BG, arrowcolor=C_TEXT_DIM,
                     lightcolor=C_PANEL, darkcolor=C_PANEL, width=11)
        st.map("Vertical.TScrollbar", background=[("active", C_PANEL_HI)])

    # ---------- tabs ----------
    def _make_tab(self, parent, key, text):
        """Flat text tab with an accent underline when active."""
        holder = tk.Frame(parent, bg=C_BG)
        holder.pack(side="left", padx=(0, 2))
        lbl = tk.Label(holder, text=text, bg=C_BG, fg=C_TEXT_DIM, font=FONT_UI,
                       padx=15, pady=8, cursor="hand2")
        lbl.pack()
        bar = tk.Frame(holder, bg=C_BG, height=2)
        bar.pack(fill="x")
        lbl.bind("<Button-1>", lambda e: self._select_tab(key))
        lbl.bind("<Enter>", lambda e: lbl.config(fg=C_TEXT)
                 if self._tab != key else None)
        lbl.bind("<Leave>", lambda e: lbl.config(fg=C_TEXT_DIM)
                 if self._tab != key else None)
        self._tabs[key] = (lbl, bar)

    def _select_tab(self, key):
        if key not in self._pages:
            return
        self._tab = key
        for k, (lbl, bar) in self._tabs.items():
            active = (k == key)
            lbl.config(fg=C_TEXT if active else C_TEXT_DIM)
            bar.config(bg=C_ACCENT if active else C_BG)
        try:
            self._pages[key].tkraise()
        except Exception:
            pass
        if key == "preview":
            try:
                self.display_view()
            except Exception:
                pass

    # ---------- page builders ----------
    def _card(self, parent, title=None, expand=False):
        """A bordered panel -- the app's only grouping device."""
        outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                         highlightthickness=1)
        outer.pack(fill="both" if expand else "x", expand=expand, pady=(0, 12))
        if title:
            tk.Label(outer, text=title, bg=C_PANEL, fg=C_TEXT, font=FONT_H2,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 6))
        return outer

    def _build_preview_page(self, pg):
        """Preview tab: the video stage, the floating params, the transport."""
        self.canvas = tk.Canvas(pg, bg=C_CANVAS, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self.canvas.bind("<Configure>", lambda e: self.display_view())
        self.canvas.bind("<B1-Motion>", self.on_canvas_motion)
        self.canvas.bind("<Button-1>", self.on_canvas_press)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<MouseWheel>", self.on_canvas_wheel)
        self.canvas.bind("<Motion>", self.on_canvas_hover)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_step(1))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_step(-1))

        # the parameter controls float over the stage
        self._build_float_card(self.canvas)

        # timeline
        self.fslider = ttk.Scale(pg, from_=0, to=1, orient="horizontal",
                                 command=lambda v: self.on_frame(),
                                 style="Rail.Horizontal.TScale")
        self.fslider.pack(fill="x", padx=12, pady=(0, 4))
        self.fslider.bind("<Button-1>", self.on_timeline_click)
        self.fslider.bind("<B1-Motion>", self.on_timeline_drag)

        # transport row
        v = tk.Frame(pg, bg=C_BG)
        v.pack(fill="x", padx=12, pady=(0, 10))
        # source switcher: a folder import can bring in many files, and the
        # compare view is where you want to flip through them
        tk.Label(v, text="文件", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        self.src_var = tk.StringVar(value="")
        self.src_cb = ttk.Combobox(v, textvariable=self.src_var, values=[],
                                   state="readonly", width=19)
        self.src_cb.pack(side="left", padx=(5, 4))
        self.src_cb.bind("<<ComboboxSelected>>", self._on_source_pick)
        self.btn_prev = flat_button(v, "◀", lambda: self.step_source(-1))
        self.btn_prev.pack(side="left", padx=(0, 2))
        self.btn_next = flat_button(v, "▶", lambda: self.step_source(1))
        self.btn_next.pack(side="left", padx=(0, 16))
        tk.Label(v, text="显示", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        self.view_var = tk.StringVar(value="分屏")
        self.view_cb = ttk.Combobox(v, textvariable=self.view_var, values=VIEWS,
                                    state="readonly", width=6)
        self.view_cb.pack(side="left", padx=(5, 16))
        self.view_cb.bind("<<ComboboxSelected>>", lambda e: self.on_view_change())
        tk.Label(v, text="缩放", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        self.zoom_var = tk.StringVar(value=ZOOM_DEFAULT)
        self.zoom_cb = ttk.Combobox(v, textvariable=self.zoom_var,
                                    values=ZOOM_CHOICES, state="readonly", width=8)
        self.zoom_cb.pack(side="left", padx=(5, 6))
        self.zoom_cb.bind("<<ComboboxSelected>>", lambda e: self.on_zoom_change())
        flat_button(v, "复位", self.reset_view_transform
                    ).pack(side="left", padx=(0, 16))
        tk.Label(v, text="帧", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        self.fentry = tk.Entry(v, width=6, bg=C_PANEL, fg=C_TEXT,
                               insertbackground=C_TEXT, relief="flat",
                               highlightthickness=1,
                               highlightbackground=C_BORDER,
                               highlightcolor=C_ACCENT, font=FONT_UI,
                               justify="center")
        self.fentry.pack(side="left", padx=5, ipady=3)
        self.fentry.insert(0, "0")
        self.fentry.bind("<Return>", self.on_frame_entry)
        self.fentry.bind("<FocusOut>", lambda e: self.sync_frame_entry())
        self.ftotal = tk.Label(v, text="/ 0", bg=C_BG, fg=C_TEXT_DIM,
                               font=FONT_SMALL)
        self.ftotal.pack(side="left", padx=(0, 16))
        self.play_btn = flat_button(v, "▶ 播放", self.toggle_play, accent=True)
        self.play_btn.pack(side="left")
        self.panel_var = tk.IntVar(value=1)
        ttk.Checkbutton(v, text="参数面板", variable=self.panel_var,
                        command=self.on_panel_toggle,
                        style="Bar.TCheckbutton").pack(side="right")

    def _build_export_page(self, pg):
        """Export tab: output options, the two ways to write a file, the queue."""
        d = self._export_opts = {}
        d['v_outview'] = tk.StringVar(value="处理")
        d['v_outmix'] = tk.DoubleVar(value=1.0)
        d['v_preview_cap'] = tk.StringVar(value=PREVIEW_CAP_DEFAULT)

        wrap = tk.Frame(pg, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        c = self._card(wrap, "输出")
        row = tk.Frame(c, bg=C_PANEL); row.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(row, text="编码器", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        self.enc_var = tk.StringVar(value=list(ENCODER_CHOICES)[0])
        ttk.Combobox(row, textvariable=self.enc_var, values=list(ENCODER_CHOICES),
                     state="readonly", width=24).pack(side="left", padx=(6, 22))
        tk.Label(row, text="输出视图", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        w = ttk.Combobox(row, textvariable=d['v_outview'],
                         values=list(OUTVIEW_CHOICES), state="readonly", width=8)
        w.pack(side="left", padx=(6, 22))
        w.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())
        tk.Label(row, text="预览上限", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        w = ttk.Combobox(row, textvariable=d['v_preview_cap'],
                         values=list(PREVIEW_CAPS), state="readonly", width=7)
        w.pack(side="left", padx=(6, 0))
        w.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())

        row2 = tk.Frame(c, bg=C_PANEL); row2.pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(row2, text="输出混合", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        mix_val = tk.Label(row2, text="%.2f" % d['v_outmix'].get(), bg=C_PANEL,
                           fg=C_TEXT, font=FONT_SMALL, width=5, anchor="w")
        ttk.Scale(row2, from_=0, to=1, orient="horizontal",
                  variable=d['v_outmix'], length=150,
                  style="Card.Horizontal.TScale",
                  command=lambda v: (mix_val.config(text="%.2f" % float(v)),
                                     self.on_settings_change())
                  ).pack(side="left", padx=(8, 6))
        mix_val.pack(side="left")
        tk.Label(row2, text="（只影响预览画面；导出始终用原始分辨率与完整处理链）",
                 bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL).pack(side="left",
                                                                  padx=(10, 0))

        c2 = self._card(wrap, "操作")
        acts = tk.Frame(c2, bg=C_PANEL); acts.pack(fill="x", padx=14, pady=(0, 14))
        flat_button(acts, "导出到文件", self.export_dlss, accent=True
                    ).pack(side="left")
        flat_button(acts, "加入转换队列", self.enqueue_current
                    ).pack(side="left", padx=(8, 0))
        flat_button(acts, "列表全部加入队列", self.enqueue_all_sources
                    ).pack(side="left", padx=(8, 0))
        tk.Label(acts,
                 text="「导出到文件」立刻转换当前视频；两个队列按钮把参数存成任务，"
                      "稍后批量跑。",
                 bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL
                 ).pack(side="left", padx=16)

        # The queue shares this page on purpose: tune -> add -> run is a single
        # workflow, and the export controls alone left the page half empty.
        cq = self._card(wrap, "转换队列", expand=True)
        self._build_queue_panel(cq)

    def _build_log_page(self, pg):
        wrap = tk.Frame(pg, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)
        self._build_log_panel(wrap)

    def _build_statusbar(self, root):
        """Progress + status + timing, pinned to the bottom on every tab."""
        bar = tk.Frame(root, bg=C_BG)
        bar.pack(fill="x", side="bottom")
        tk.Frame(bar, bg=C_BORDER, height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=C_BG)
        inner.pack(fill="x", padx=14, pady=8)
        self.pbar = ttk.Progressbar(inner, maximum=100)
        self.pbar.pack(fill="x")
        line = tk.Frame(inner, bg=C_BG)
        line.pack(fill="x", pady=(5, 0))
        self.status = tk.Label(line, text="就绪", bg=C_BG, fg=C_TEXT,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="left")
        self.stats = tk.Label(line, text="", bg=C_BG, fg=C_TEXT_DIM,
                              font=FONT_SMALL, anchor="e")
        self.stats.pack(side="right")

    # ---------- floating parameter card ----------
    def _build_float_card(self, canvas):
        """The parameter controls live in a card that FLOATS over the video.

        A tab page would hide them (you cannot see the picture and the sliders
        at once) and the previous pack/forget panel stole height from the image.
        A draggable, collapsible card keeps the stage full-size and the controls
        in reach; it is the same idea as the filter's own native panel.
        """
        card = tk.Frame(canvas, bg=C_PANEL, highlightbackground=C_BORDER,
                        highlightthickness=1)
        self._float_card = card

        head = tk.Frame(card, bg=C_PANEL)
        head.pack(fill="x")
        grip = tk.Label(head, text="⠿", bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_UI,
                        padx=10, pady=7, cursor="fleur")
        grip.pack(side="left")
        title = tk.Label(head, text="处理参数", bg=C_PANEL, fg=C_TEXT,
                         font=FONT_H2, cursor="fleur")
        title.pack(side="left", pady=7)
        self._float_toggle = tk.Label(head, text="▾", bg=C_PANEL, fg=C_TEXT_DIM,
                                      font=FONT_UI, padx=10, cursor="hand2")
        self._float_toggle.pack(side="right")
        for w in (head, grip, title):
            w.bind("<Button-1>", self._float_drag_start)
            w.bind("<B1-Motion>", self._float_drag_move)
        self._float_toggle.bind("<Button-1>", lambda e: self._float_collapse())

        body = tk.Frame(card, bg=C_PANEL)
        body.pack(fill="x", pady=(0, 6))
        self._float_body = body
        self._float_open = True

        tk.Label(body, text="DLSS 神经渲染", bg=C_PANEL, fg=C_ACCENT,
                 font=FONT_H2, anchor="w").pack(fill="x", padx=12, pady=(4, 2))
        g1 = tk.Frame(body, bg=C_PANEL); g1.pack(fill="x")
        self._settings = self._build_settings(g1)

        tk.Label(body, text="RTX Video 增强", bg=C_PANEL, fg=C_ACCENT,
                 font=FONT_H2, anchor="w").pack(fill="x", padx=12, pady=(12, 2))
        g2 = tk.Frame(body, bg=C_PANEL); g2.pack(fill="x")
        self._rtx = self._build_rtx_settings(g2)

        card.place(x=18, y=18)
        card.lift()

    def _float_drag_start(self, event):
        self._float_origin = (event.x_root, event.y_root,
                              self._float_card.winfo_x(),
                              self._float_card.winfo_y())

    def _float_drag_move(self, event):
        o = getattr(self, "_float_origin", None)
        if not o:
            return
        x0, y0, cx, cy = o
        nx = cx + (event.x_root - x0)
        ny = cy + (event.y_root - y0)
        # keep a grabbable sliver of the card on the stage
        cw = max(self.canvas.winfo_width(), 120)
        chh = max(self.canvas.winfo_height(), 60)
        nx = max(min(nx, cw - 60), -self._float_card.winfo_width() + 60)
        ny = max(min(ny, chh - 30), 0)
        self._float_card.place(x=nx, y=ny)

    def _float_collapse(self):
        # explicit flag, not winfo_ismapped(): that reports 0 for every widget
        # until the toplevel is actually mapped, which would make the first
        # click a no-op.
        self._float_open = not getattr(self, "_float_open", True)
        if self._float_open:
            self._float_body.pack(fill="x", pady=(0, 6))
            self._float_toggle.config(text="▾")
        else:
            self._float_body.pack_forget()
            self._float_toggle.config(text="▸")

    def on_panel_toggle(self):
        """Show / hide the whole floating parameter card."""
        try:
            if bool(self.panel_var.get()):
                self._float_card.place(x=18, y=18)
                self._float_card.lift()
            else:
                self._float_card.place_forget()
        except Exception:
            pass

    def _build_log_panel(self, parent):
        self.log = scrolledtext.ScrolledText(
            parent, height=12, state="disabled", font=FONT_MONO,
            bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, relief="flat",
            bd=0, highlightthickness=1, highlightbackground=C_BORDER,
            wrap="none")
        self.log.pack(fill="both", expand=True)

    # ---------- conversion queue ----------
    def _build_queue_panel(self, parent):
        bar = tk.Frame(parent, bg=C_PANEL)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        self.q_start_btn = flat_button(bar, "▶ 开始队列", self.start_queue,
                                       accent=True)
        self.q_start_btn.pack(side="left")
        self.q_pause_btn = flat_button(bar, "⏸ 暂停", self.toggle_queue_pause)
        self.q_pause_btn.pack(side="left", padx=(8, 0))
        self.q_stop_btn = flat_button(bar, "⏹ 停止", self.stop_queue)
        self.q_stop_btn.pack(side="left", padx=(8, 0))
        flat_button(bar, "上移", lambda: self._queue_move(-1)
                    ).pack(side="left", padx=(20, 0))
        flat_button(bar, "下移", lambda: self._queue_move(1)
                    ).pack(side="left", padx=(8, 0))
        flat_button(bar, "移除选中", self._queue_remove_selected
                    ).pack(side="left", padx=(8, 0))
        flat_button(bar, "清空已完成", self._queue_clear_done
                    ).pack(side="left", padx=(8, 0))
        self.q_count = tk.Label(bar, text="0 个任务", bg=C_PANEL, fg=C_TEXT_DIM,
                                font=FONT_SMALL)
        self.q_count.pack(side="right")
        for b in (self.q_pause_btn, self.q_stop_btn):
            set_button_enabled(b, False)

        holder = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                          highlightthickness=1)
        holder.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        cols = ("name", "params", "status")
        self.qtree = ttk.Treeview(holder, columns=cols, show="headings",
                                  selectmode="extended")
        self.qtree.heading("name", text="视频")
        self.qtree.heading("params", text="参数")
        self.qtree.heading("status", text="状态")
        self.qtree.column("name", width=280, stretch=True)
        self.qtree.column("params", width=420, stretch=True)
        self.qtree.column("status", width=120, stretch=False)
        qscroll = ttk.Scrollbar(holder, orient="vertical",
                                command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qscroll.set)
        qscroll.pack(side="right", fill="y")
        self.qtree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self.qtree.bind("<Double-1>", self._queue_load_selected)

    # -- task snapshots --
    def _snapshot_task(self, path):
        """Freeze the current UI params into a queue task for `path`."""
        path = os.path.abspath(path)
        settings = self._collect_settings()
        settings.update(self._rtx_settings())
        enc_label = self.enc_var.get()
        s = self._rtx_settings()
        size_label = self._rtx_size_label()
        try:
            n, fps, w, h = self._video_info(path)
        except Exception:
            n, fps, w, h = 0, 30.0, 0, 0
        ow, oh = w, h
        if s['vsr_quality']:
            try:
                ow, oh = rtx_video.target_size(size_label, w, h)
            except Exception:
                pass
            settings['out_w'], settings['out_h'] = ow, oh
        suffix = output_stem_suffix(bool(settings.get('dlss', 1)), s,
                                    ow, oh, w, h, size_label)
        out_path = os.path.splitext(path)[0] + "_" + suffix + ".mp4"
        # avoid clobbering an existing file or a sibling task's output
        taken = {t["out_path"] for t in self._queue}
        base, cand = out_path, out_path
        if os.path.exists(cand) or cand in taken:
            stem, ext = os.path.splitext(base)
            for i in range(1, 1000):
                cand = "%s-%d%s" % (stem, i, ext)
                if not os.path.exists(cand) and cand not in taken:
                    break
        self._queue_seq += 1
        return {"id": self._queue_seq, "src": path,
                "name": os.path.basename(path),
                "settings": settings, "rtx": s,
                "enc_label": enc_label, "size_label": size_label,
                "n": n, "fps": fps, "w": w, "h": h,
                "ow": ow, "oh": oh, "out_path": cand,
                "status": "等待", "progress": 0.0}

    @staticmethod
    def _task_summary(task):
        s = task["settings"]
        parts = []
        lay = [l for l in (s.get('layers') or []) if l]
        n = len(lay)
        if s.get('dlss'):
            if n > 1:
                desc = " → ".join("风%d/强%.2g/色%.2g/构%.2g"
                                  % (L['style'], L['intensity'], L['local_tone'],
                                     L['local_struct'])
                                  for L in lay[:3])
                if n > 3:
                    desc += " → …"
                parts.append("DLSS %d层: %s" % (n, desc))
                sk = [L['skin_struct'] for L in lay if L.get('skin_struct')]
                if sk:
                    parts.append("皮肤" + ",".join("%.2g" % v for v in sk[:3]))
                if any(L.get('use_auto_mask') for L in lay):
                    parts.append("遮罩")
            else:
                parts.append("DLSS:风格%d/强度%.2g/色调%.2g/结构%.2g"
                             % (s.get('style', 0), s.get('intensity', 1.0),
                                s.get('local_tone', 1.0), s.get('local_struct', 1.0)))
                if s.get('skin_struct'):
                    parts.append("皮肤%.2g" % s['skin_struct'])
                if s.get('use_auto_mask'):
                    parts.append("遮罩")
        else:
            parts.append("DLSS关")
        q = int(task["rtx"].get("vsr_quality", 0) or 0)
        if q:
            parts.append("放大%s(%s)" % (rtx_video.QUALITY_NAMES.get(q, q),
                                         task["size_label"]))
        e = int(task["rtx"].get("enhance", 0) or 0)
        if e:
            parts.append(rtx_video.QUALITY_NAMES.get(e, e))
        if task["rtx"].get("hdr"):
            parts.append("HDR10")
        parts.append(task["enc_label"].split(" (")[0])
        return " + ".join(parts)

    def enqueue_current(self):
        """Add the previewed video with the on-screen params to the queue."""
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频"); return
        task = self._snapshot_task(self.video)
        self._queue.append(task)
        self._queue_refresh()
        self.logln(f"已加入队列: {task['name']} → {os.path.basename(task['out_path'])}")

    def enqueue_path(self, path):
        """Add a non-previewed video with the current UI params."""
        try:
            task = self._snapshot_task(path)
        except Exception as ex:
            self.logln(f"[队列] 跳过 {path}: {ex}")
            return
        self._queue.append(task)
        self._queue_refresh()
        self.logln(f"已加入队列: {task['name']}")

    def _queue_refresh(self):
        try:
            self.qtree.delete(*self.qtree.get_children())
            for t in self._queue:
                tag = ("running",) if t["status"] == "转换中" else (
                    ("done",) if t["status"] == "完成" else (
                        ("failed",) if t["status"] in ("失败", "已取消") else ()))
                self.qtree.insert("", "end", iid=str(t["id"]),
                                  values=(t["name"], self._task_summary(t),
                                          "%s%s" % (t["status"],
                                                    (" %.0f%%" % (t["progress"] * 100))
                                                    if t["status"] == "转换中" else "")),
                                  tags=tag)
            try:
                self.qtree.tag_configure("running", foreground="#4ACC4A")
                self.qtree.tag_configure("done", foreground="#888888")
                self.qtree.tag_configure("failed", foreground="#E65454")
            except Exception:
                pass
            waiting = sum(1 for t in self._queue if t["status"] == "等待")
            done = sum(1 for t in self._queue if t["status"] == "完成")
            self.q_count.config(text=f"{len(self._queue)} 个任务（等待 {waiting} / 完成 {done}）")
        except Exception:
            pass

    def _queue_selected_ids(self):
        try:
            return [int(i) for i in self.qtree.selection()]
        except Exception:
            return []

    def _queue_move(self, delta):
        ids = set(self._queue_selected_ids())
        if not ids:
            return
        q = self._queue
        order = list(range(len(q)))
        # move running/done tasks is allowed but pointless; restrict to waiting
        movable = [i for i in order if q[i]["status"] == "等待"]
        if delta < 0:
            for i in movable:
                if i > 0 and q[i]["id"] in ids and q[i - 1]["status"] == "等待" \
                        and q[i - 1]["id"] not in ids:
                    q[i - 1], q[i] = q[i], q[i - 1]
        else:
            for i in reversed(movable):
                if i + 1 < len(q) and q[i]["id"] in ids and q[i + 1]["status"] == "等待" \
                        and q[i + 1]["id"] not in ids:
                    q[i + 1], q[i] = q[i], q[i + 1]
        self._queue_refresh()
        for t in q:
            if t["id"] in ids:
                try:
                    self.qtree.selection_add(str(t["id"]))
                except Exception:
                    pass

    def _queue_remove_selected(self):
        ids = set(self._queue_selected_ids())
        if not ids:
            return
        cur = getattr(self, "_queue_current", None)
        self._queue = [t for t in self._queue
                       if not (t["id"] in ids and t is not cur)]
        self._queue_refresh()

    def _queue_clear_done(self):
        cur = getattr(self, "_queue_current", None)
        self._queue = [t for t in self._queue
                       if t["status"] not in ("完成", "失败", "已取消") or t is cur]
        self._queue_refresh()

    def _queue_load_selected(self, event=None):
        """Double-click a waiting task: preview it with ITS params."""
        ids = self._queue_selected_ids()
        if len(ids) != 1:
            return
        task = next((t for t in self._queue if t["id"] == ids[0]), None)
        if task is None or task["status"] == "转换中":
            return
        self._apply_task_params(task)
        self._load_path(task["src"])
        self.logln(f"已载入队列任务参数: {task['name']}")

    def _apply_task_params(self, task):
        """Push a task's snapshot back into the UI controls."""
        s = task["settings"]
        try:
            self._settings['v_dlss'].set(int(s.get('dlss', 1)))
            inv_style = {v: k for k, v in STYLE_CHOICES.items()}
            self._settings['v_style'].set(inv_style.get(int(s.get('style', 0)), "默认"))
            self._settings['v_intensity'].set(float(s.get('intensity', 1.0)))
            self._settings['v_local_tone'].set(float(s.get('local_tone', 1.0)))
            self._settings['v_local_struct'].set(float(s.get('local_struct', 1.0)))
            self._settings['v_skin'].set(float(s.get('skin_struct', 0.0)))
            self._settings['v_mask'].set(int(s.get('use_auto_mask', 0)))
        except Exception:
            pass
        r = task["rtx"]
        try:
            inv_vsr = {v: k for k, v in rtx_video.VSR_CHOICES.items()}
            inv_enh = {v: k for k, v in rtx_video.ENHANCE_CHOICES.items()}
            self._rtx['v_vsr'].set(inv_vsr.get(int(r.get('vsr_quality', 0)), "关闭"))
            self._rtx['v_size'].set(task.get("size_label", "×2"))
            self._rtx['v_enh'].set(inv_enh.get(int(r.get('enhance', 0)), "关闭"))
            self._rtx['v_hdr'].set(int(r.get('hdr', 0)))
            self._rtx['v_hdr_gray'].set(int(r.get('hdr_middle_gray', 50)))
            self._rtx['v_hdr_nits'].set(int(r.get('hdr_max_luminance', 1000)))
            self._rtx['v_hdr_contrast'].set(hdr_adj_to_ui(r.get('hdr_contrast', 100)))
            self._rtx['v_hdr_sat'].set(hdr_adj_to_ui(r.get('hdr_saturation', 100)))
        except Exception:
            pass
        try:
            self.enc_var.set(task.get("enc_label", list(ENCODER_CHOICES)[0]))
            inv_out = {v: k for k, v in OUTVIEW_CHOICES.items()}
            self._export_opts['v_outview'].set(inv_out.get(int(s.get('output_view', 0)), "处理"))
            self._export_opts['v_outmix'].set(float(s.get('output_mix', 1.0)))
        except Exception:
            pass
        # restore the layer chain, then show its first layer in the controls
        try:
            got = [dict(l) for l in (s.get('layers') or []) if l]
            if got:
                self._layers = got
                self._load_layer_to_ui(min(self._layer_i, len(got) - 1))
        except Exception:
            pass
        try:
            self.on_dlss_toggle()
        except Exception:
            pass
        # controls were set programmatically; repaint their value labels
        for fn in getattr(self, "_slider_updaters", []):
            try:
                fn()
            except Exception:
                pass

    # ---------- helpers ----------
    def _fit_window(self):
        """Pick the initial window size.

        The layout is tab-based now: pages are stacked in one grid cell and only
        the active one is raised, so nothing can overlap and there is no content
        height worth measuring. A comfortable size that fits the screen is
        enough, and the user resizes freely from there.
        """
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        w = min(1240, max(sw - 140, 900))
        h = min(820, max(sh - 140, 600))
        self.root.geometry("%dx%d+%d+%d" % (w, h, max((sw - w) // 2, 0),
                                            max((sh - h) // 3, 0)))
        self.root.minsize(900, 600)
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
        """DLSS controls -- the seven parameters the engine actually consumes.

        Laid out for the floating card: one slider per line, full width. Export
        and preview-debug knobs live on the export tab, so this block is purely
        "tune the look".

        Ranges mirror the filter's native panel, which is where they were
        measured: intensity saturates at 1.0 (1.0 vs 2.0 is bit-identical, so the
        slider stops at 100%), while local tone / local structure / skin
        structure stay effective up to 2.0 and therefore run 0-2.
        Skin structure additionally does nothing unless 自动遮罩 is on -- that is
        a measured property of the network, not a UI restriction.
        """
        d = {}
        d['v_dlss'] = tk.IntVar(value=1)
        d['v_style'] = tk.StringVar(value="默认")
        d['v_intensity'] = tk.DoubleVar(value=1.0)
        d['v_local_tone'] = tk.DoubleVar(value=1.0)
        d['v_local_struct'] = tk.DoubleVar(value=1.0)
        d['v_skin'] = tk.DoubleVar(value=0.0)      # 0 = off, like the filter
        d['v_mask'] = tk.IntVar(value=0)           # 自动遮罩
        # Controls that only mean something while the neural pass runs. They get
        # dimmed when it is switched off, rather than being silently ignored.
        dimmed = []
        self._slider_updaters = []

        top = tk.Frame(parent, bg=C_PANEL)
        top.grid(row=0, column=0, columnspan=3, sticky="we", pady=(2, 6))
        ttk.Checkbutton(top, text="启用 DLSS 处理", variable=d['v_dlss'],
                        command=self.on_dlss_toggle
                        ).pack(side="left", padx=(12, 12))
        tk.Label(top, text="风格", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left")
        w = ttk.Combobox(top, textvariable=d['v_style'],
                         values=list(STYLE_CHOICES), state="readonly", width=6)
        w.pack(side="left", padx=(6, 12)); dimmed.append(w)
        w.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())

        # --- layer strip: each layer keeps its own parameter set -----------
        lrow = tk.Frame(parent, bg=C_PANEL)
        lrow.grid(row=1, column=0, columnspan=3, sticky="we", pady=(0, 4))
        tk.Label(lrow, text="层", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left", padx=(12, 6))
        self._layer_bar = tk.Frame(lrow, bg=C_PANEL)
        self._layer_bar.pack(side="left")
        self._layer_note = tk.Label(lrow, text="", bg=C_PANEL, fg=C_TEXT_DIM,
                                    font=FONT_SMALL)
        self._layer_note.pack(side="left", padx=(10, 8))
        self._layer_btns = []

        slider_row(parent, 2, "强度", d['v_intensity'], 0, 1, 0.05, "%.2f",
                   self.on_settings_change, dimmed,
                   updaters=self._slider_updaters)
        slider_row(parent, 3, "本地色调", d['v_local_tone'], 0, 2, 0.05, "%.2f",
                   self.on_settings_change, dimmed,
                   updaters=self._slider_updaters)
        slider_row(parent, 4, "本地结构", d['v_local_struct'], 0, 2, 0.05, "%.2f",
                   self.on_settings_change, dimmed,
                   updaters=self._slider_updaters)
        slider_row(parent, 5, "皮肤结构", d['v_skin'], 0, 2, 0.05, "%.2f",
                   self.on_settings_change, dimmed,
                   updaters=self._slider_updaters)

        mask_row = tk.Frame(parent, bg=C_PANEL)
        mask_row.grid(row=6, column=0, columnspan=3, sticky="we", pady=(2, 2))
        cb = ttk.Checkbutton(mask_row, text="自动遮罩", variable=d['v_mask'],
                             command=self.on_settings_change)
        cb.pack(side="left", padx=(12, 8)); dimmed.append(cb)

        d['hint'] = tk.Label(parent, text="", bg=C_PANEL, fg=C_TEXT_DIM,
                             font=FONT_SMALL, anchor="w", justify="left",
                             wraplength=390)
        d['hint'].grid(row=7, column=0, columnspan=3, sticky="we", padx=12,
                       pady=(2, 4))
        d['_dimmed'] = dimmed
        self._refresh_layer_ui()
        return d

    # ---------- multi-layer DLSSNR ----------
    def _refresh_layer_ui(self):
        """Rebuild the layer strip: one button per layer, plus + / -."""
        try:
            for b in self._layer_btns:
                b.destroy()
            self._layer_btns = []
            for i in range(len(self._layers)):
                b = flat_button(self._layer_bar, str(i + 1),
                                lambda idx=i: self._select_layer(idx))
                b.pack(side="left", padx=(0, 3))
                self._layer_btns.append(b)
                active = (i == self._layer_i)
                b.config(bg=C_ACCENT if active else C_PANEL,
                         fg="#FFFFFF" if active else C_TEXT,
                         font=FONT_H2 if active else FONT_UI)
                b._base_bg = C_ACCENT if active else C_PANEL
                b._hover_bg = C_ACCENT_HI if active else C_PANEL_HI
            b = flat_button(self._layer_bar, "＋", self.add_layer)
            b.pack(side="left", padx=(6, 2)); self._layer_btns.append(b)
            b = flat_button(self._layer_bar, "－", self.remove_layer)
            b.pack(side="left", padx=(0, 0)); self._layer_btns.append(b)
            n = len(self._layers)
            self._layer_note.config(
                text=("每帧按 1→%d 依次处理，每层参数独立" % n) if n > 1
                else "单层（点＋加一层）")
            if hasattr(self, "_refresh_rtx_note"):
                self._refresh_rtx_note()
        except Exception:
            pass

    def _sync_current_layer(self):
        """Mirror the on-screen controls into the selected layer."""
        if not getattr(self, "_layers", None):
            return
        d = self._settings
        try:
            self._layers[self._layer_i] = {
                "style": STYLE_CHOICES.get(d['v_style'].get(), 0),
                "intensity": float(d['v_intensity'].get()),
                "local_tone": float(d['v_local_tone'].get()),
                "local_struct": float(d['v_local_struct'].get()),
                "skin_struct": float(d['v_skin'].get()),
                "use_auto_mask": int(d['v_mask'].get()),
            }
        except Exception:
            pass

    def _load_layer_to_ui(self, i):
        """Show layer `i` in the controls."""
        if not (0 <= i < len(self._layers)):
            return False
        self._layer_i = i
        L = self._layers[i]
        inv_style = {v: k for k, v in STYLE_CHOICES.items()}
        try:
            self._settings['v_style'].set(inv_style.get(int(L["style"]), "默认"))
            self._settings['v_intensity'].set(float(L["intensity"]))
            self._settings['v_local_tone'].set(float(L["local_tone"]))
            self._settings['v_local_struct'].set(float(L["local_struct"]))
            self._settings['v_skin'].set(float(L["skin_struct"]))
            self._settings['v_mask'].set(int(L["use_auto_mask"]))
        except Exception:
            return False
        for fn in getattr(self, "_slider_updaters", []):
            try:
                fn()
            except Exception:
                pass
        self._refresh_layer_ui()
        return True

    def _select_layer(self, i):
        if i == self._layer_i:
            return
        self._sync_current_layer()
        if self._load_layer_to_ui(i):
            self.on_settings_change()

    def add_layer(self):
        """Append a layer, seeded from the current one so you can just tweak it."""
        self._sync_current_layer()
        if len(self._layers) >= 8:
            return
        self._layers.append(dict(self._layers[self._layer_i]))
        self._load_layer_to_ui(len(self._layers) - 1)
        self.on_settings_change()

    def remove_layer(self):
        """Drop the selected layer (never the last remaining one)."""
        self._sync_current_layer()
        if len(self._layers) <= 1:
            return
        del self._layers[self._layer_i]
        self._load_layer_to_ui(min(self._layer_i, len(self._layers) - 1))
        self.on_settings_change()

    def _dlss_layers(self):
        """The layer list as the engine should receive it."""
        self._sync_current_layer()
        return [dict(l) for l in self._layers]

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
        e = self._export_opts
        layers = self._dlss_layers() if getattr(self, "_layers", None) else []
        cur = layers[self._layer_i] if layers else default_layer()
        s = {
            'dlss': int(d['v_dlss'].get()),
            'style': int(cur["style"]),
            'intensity': float(cur["intensity"]),
            'local_tone': float(cur["local_tone"]),
            'local_struct': float(cur["local_struct"]),
            # dlssnr2_set_options() takes these two as its 5th / 6th arguments;
            # its default for skin is the -1 "unspecified" sentinel, so we send
            # the real 0..2 value (0 = off) exactly like the filter panel does.
            'skin_struct': float(cur["skin_struct"]),
            'use_auto_mask': int(cur["use_auto_mask"]),
            'output_view': OUTVIEW_CHOICES.get(e['v_outview'].get(), 0),
            'output_mix': float(e['v_outmix'].get()),
            # The full chain, in order. A single-element list means "exactly the
            # old behaviour"; the flat keys above stay the first/current layer
            # so every existing code path keeps working unchanged.
            'layers': layers,
            'n_layers': len(layers),
        }
        return s

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

        r0 = tk.Frame(parent, bg=C_PANEL)
        r0.grid(row=0, column=0, columnspan=6, sticky="we", pady=(2, 5))
        tk.Label(r0, text="AI 放大", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left", padx=(12, 6))
        cb_vsr = ttk.Combobox(r0, textvariable=d['v_vsr'],
                              values=list(rtx_video.VSR_CHOICES),
                              state="readonly", width=8)
        cb_vsr.pack(side="left")
        tk.Label(r0, text="目标", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left", padx=(14, 6))
        cb_size = ttk.Combobox(r0, textvariable=d['v_size'],
                               values=rtx_video.SIZE_CHOICES,
                               state="readonly", width=5)
        cb_size.pack(side="left")

        r1 = tk.Frame(parent, bg=C_PANEL)
        r1.grid(row=1, column=0, columnspan=6, sticky="we", pady=(0, 5))
        tk.Label(r1, text="降噪 / 去模糊 / 高码率", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=FONT_SMALL).pack(side="left", padx=(12, 6))
        cb_enh = ttk.Combobox(r1, textvariable=d['v_enh'],
                              values=list(rtx_video.ENHANCE_CHOICES),
                              state="readonly", width=10)
        cb_enh.pack(side="left")
        # the combos now sit inside sub-frames, so bind each one explicitly
        for cb in (cb_vsr, cb_size, cb_enh):
            cb.bind("<<ComboboxSelected>>",
                    lambda e: self.on_settings_change())

        r2 = tk.Frame(parent, bg=C_PANEL)
        r2.grid(row=2, column=0, columnspan=6, sticky="we", pady=(0, 5))
        ttk.Checkbutton(r2, text="SDR → HDR10 输出", variable=d['v_hdr'],
                        command=self.on_settings_change).pack(side="left",
                                                              padx=(12, 8))

        # Two HDR sliders per row keeps the card narrow enough to sit over the
        # picture. Contrast / saturation are shown the way NVIDIA does: relative
        # -100..+100% around neutral, converted to the SDK's 0..200 downstream.
        slider_row(parent, 3, "中灰", d['v_hdr_gray'], 10, 100, 1, "%.0f",
                   self.on_settings_change, None, width=104, col0=0,
                   updaters=self._slider_updaters)
        slider_row(parent, 3, "峰值", d['v_hdr_nits'], 400, 2000, 50, "%.0f",
                   self.on_settings_change, None, width=104, col0=3,
                   updaters=self._slider_updaters)
        slider_row(parent, 4, "对比%", d['v_hdr_contrast'],
                   HDR_ADJ_MIN, HDR_ADJ_MAX, 1, "%d",
                   self.on_settings_change, None, width=104, col0=0,
                   updaters=self._slider_updaters)
        slider_row(parent, 4, "饱和%", d['v_hdr_sat'],
                   HDR_ADJ_MIN, HDR_ADJ_MAX, 1, "%d",
                   self.on_settings_change, None, width=104, col0=3,
                   updaters=self._slider_updaters)

        d['note'] = tk.Label(parent, text="", bg=C_PANEL, fg=C_TEXT_DIM,
                             font=FONT_SMALL, anchor="w", justify="left",
                             wraplength=390)
        d['note'].grid(row=5, column=0, columnspan=6, sticky="we", padx=12,
                       pady=(4, 4))
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
        chain = tuple(tuple(L[k] for k in LAYER_KEYS) for L in s.get('layers', []))
        return (s['dlss'], s['style'], s['intensity'], s['local_tone'],
                s['local_struct'], s['skin_struct'], s['use_auto_mask'], chain,
                s['output_view'], s['output_mix'], self._preview_cap())

    # ---------- preview working resolution ----------
    def _preview_cap(self):
        """Height ceiling for preview processing; 0 means unlimited."""
        opts = getattr(self, "_export_opts", None) or {}
        get = opts.get('v_preview_cap')
        try:
            cur = get.get() if get is not None else PREVIEW_CAP_DEFAULT
        except Exception:
            cur = PREVIEW_CAP_DEFAULT
        return PREVIEW_CAPS.get(cur, PREVIEW_CAPS[PREVIEW_CAP_DEFAULT])

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
        """Frame -> RGB image (HxWx3) produced on the GPU, or None.

        RGB and not RGBA: Tk's photo image wants RGB, so handing out RGBA meant
        every paint paid a second full-frame conversion. The engine's buffer is
        RGBA, so the one unavoidable conversion happens here -- once, and then
        the result is what gets cached.
        """
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
                eng = (dlss_engine.ChainEngine(self._collect_settings())
                       if use_dlss else None)
                pw2, ph2, _ = self._pipe.configure(
                    pw, ph, self._scale_targets(rtx, k), dlss_engine=eng)
                payload, kind = self._pipe.process(fr, reset=bool(reset))
                if kind == "bgr":
                    bgr = np.frombuffer(payload, np.uint8).reshape(ph2, pw2, 3)
                    o = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                elif kind == "r10":
                    packed = np.frombuffer(payload, np.uint32).reshape(ph2, pw2)
                    r = ((packed >> 0) & 0x3FF).astype(np.float32) / 1023.0
                    g = ((packed >> 10) & 0x3FF).astype(np.float32) / 1023.0
                    b = ((packed >> 20) & 0x3FF).astype(np.float32) / 1023.0
                    o = np.clip(np.dstack([r, g, b]) * 255.0, 0, 255).astype(np.uint8)
                else:
                    f16 = np.frombuffer(payload, np.float16).reshape(ph2, pw2, 4)
                    lin = np.nan_to_num(f16[..., :3].astype(np.float32), nan=0.0)
                    o = np.clip(np.power(np.clip(lin, 0, 1), 1 / 2.2) * 255.0,
                                0, 255).astype(np.uint8)
            except Exception as ex:
                self.logln("[RTX 预览] " + str(ex))
                return None
        else:
            eng = self._ensure_engine(pw, ph)
            if eng is None:
                return None
            layers = self._dlss_layers()
            if len(layers) > 1:
                # one round trip per layer; the last one writes RGBA
                rgba2 = eng.process_chain(fr, layers, reset=reset, out_rgba=True)
                o = None if rgba2 is None else cv2.cvtColor(rgba2, cv2.COLOR_RGBA2RGB)
            else:
                rgba = eng.process_rgba(fr, reset=reset)
                o = None if rgba is None else cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)

        self._last_dlss_frame = frame
        if o is None:
            self._live_cache = None
            return None
        self._live_cache = (frame, sk, o)      # fresh array: safe to cache
        return o

    def load_view_img(self, view, frame):
        """Both views are returned as RGB -- the format Tk's photo wants.

        Doing it once here means no paint ever needs a second full-frame
        conversion, and the 原图 side goes straight BGR -> RGB instead of the old
        BGR -> RGBA -> RGB round trip (~9 ms -> ~1.6 ms for a 1080p frame).
        """
        if view == "DLSS" and getattr(self, "_queue_active", False):
            # the queue owns the GPU right now: degrade to the original frame
            # instead of fighting the worker over engine2's temporal state
            view = "原图"
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
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if view == "DLSS":
            return self._live_dlss_image(frame)
        return None

    def display_view(self):
        if not self.video or getattr(self, "_exporting", False):
            return
        frame = int(self.fslider.get())
        view = VIEW_ALIAS.get(self.view_var.get(), self.view_var.get())
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width() or 780, 200)
        ch = max(self.canvas.winfo_height() or 400, 150)
        # Reset the interaction state on every paint. A divider handle left over
        # from 分屏, or a pan range from a bigger zoom level, would otherwise let
        # a drag grab the wrong thing in the new view/zoom.
        self._divider_pt = None
        self._pan_max = (0.0, 0.0)
        if view in ("分屏", "并排"):
            self._draw_compare(frame, cw, ch, side_by_side=(view == "并排"))
            return
        img = self.load_view_img(view, frame)
        if img is None:
            msg = f"{view}：帧 {frame} 读取失败" if view == "原图" else f"DLSS：帧 {frame} 生成失败"
            self.canvas.create_text(cw // 2, ch // 2, text=msg, fill="#888888", font=("Microsoft YaHei", 11))
            return
        self._draw_fit(img, cw, ch)

    # ---------- zoom / pan ----------
    def _zoom_value(self):
        """Current zoom as float, or None for fit-to-window."""
        if self.zoom_var is None:
            return None
        t = (self.zoom_var.get() or "").strip()
        if t == ZOOM_DEFAULT or t == "适应窗口":
            return None
        try:
            return max(0.05, float(t.replace("%", "")) / 100.0)
        except Exception:
            return None

    def _view_scale(self, iw, ih, cw, ch):
        """Pixels of image per pixel of canvas, honouring the zoom setting."""
        z = self._zoom_value()
        if z is not None:
            return z
        if iw <= 0 or ih <= 0:
            return 1.0
        s = min(cw / iw, ch / ih)
        # remembered so the first wheel notch out of "fit" starts from what the
        # user is actually looking at instead of jumping to a fixed level
        self._last_fit_scale = s
        return s

    def _paint_rgb(self, rgb, x, y):
        """Hand one RGB array to the canvas at (x, y).

        Tk's own PhotoImage is fed a PPM buffer rather than going through
        PIL -> ImageTk: one fewer full-frame copy, and `-data` accepts the raw
        P6 bytes directly. (Do NOT use PhotoImage.put() for this -- on Tk 8.6 it
        costs ~44 ms per 1080p frame because it validates pixel by pixel.)
        """
        h, w = rgb.shape[:2]
        rgb = np.ascontiguousarray(rgb)
        data = b"P6\n%d %d\n255\n" % (w, h) + rgb.tobytes()
        try:
            self._photo = tk.PhotoImage(data=data, master=self.canvas)
        except Exception:
            from PIL import Image, ImageTk       # fallback for odd Tk builds
            self._pilimg = Image.fromarray(rgb)
            self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.create_image(x, y, anchor="nw", image=self._photo)

    def _visible_source(self, iw, ih, scale, off_x, off_y, cw, ch):
        """Which part of a scaled image actually lands on the canvas.

        Returns (vx0, vy0, vx1, vy1, ix0, iy0, ix1, iy1) or None, where v* is
        the canvas rect to fill and i* the matching source rect. Only that slice
        is ever scaled and uploaded: uploading the whole zoomed frame is what
        made 2x cost ~80 ms and 3x ~180 ms per paint, even though most of it was
        off-screen.
        """
        dw, dh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        vx0, vy0 = max(0, off_x), max(0, off_y)
        vx1, vy1 = min(cw, off_x + dw), min(ch, off_y + dh)
        if vx1 <= vx0 or vy1 <= vy0:
            return None
        ix0 = max(0, int((vx0 - off_x) / scale))
        iy0 = max(0, int((vy0 - off_y) / scale))
        ix1 = min(iw, int(np.ceil((vx1 - off_x) / scale)) + 1)
        iy1 = min(ih, int(np.ceil((vy1 - off_y) / scale)) + 1)
        if ix1 <= ix0 or iy1 <= iy0:
            return None
        return (vx0, vy0, vx1, vy1, ix0, iy0, ix1, iy1)

    def _blit(self, rgb_img, cw, ch):
        """Paint one RGB image with the current zoom/pan; remember the mapping."""
        ih, iw = rgb_img.shape[:2]
        scale = self._view_scale(iw, ih, cw, ch)
        nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        # pan limits come from the FULL scaled size, not the visible slice
        self._pan_max = (max(0.0, (nw - cw) / 2.0), max(0.0, (nh - ch) / 2.0))
        self._clamp_pan()
        ox = (cw - nw) // 2 + int(self.pan_x)
        oy = (ch - nh) // 2 + int(self.pan_y)
        self._view_map = (ox, oy, nw, nh, cw, ch)
        r = self._visible_source(iw, ih, scale, ox, oy, cw, ch)
        self.canvas.delete("all")
        if r is None:
            return
        vx0, vy0, vx1, vy1, ix0, iy0, ix1, iy1 = r
        patch = cv2.resize(rgb_img[iy0:iy1, ix0:ix1], (vx1 - vx0, vy1 - vy0))
        self._paint_rgb(patch, vx0, vy0)

    def _draw_fit(self, img, cw, ch):
        # images arrive as RGB already
        self._blit(img, cw, ch)

    def _draw_compare(self, frame, cw, ch, side_by_side=False):
        key = (frame, cw, ch, self._zoom_value(), side_by_side)
        if getattr(self, "_split_key", None) != key:
            orig = self.load_view_img("原图", frame)
            dlss = self.load_view_img("DLSS", frame)
            if orig is None:
                self.canvas.create_text(cw // 2, ch // 2,
                                        text=f"帧 {frame} 读取失败", fill="#888")
                return
            if dlss is None:
                self._draw_fit(orig, cw, ch)
                self.canvas.create_text(cw // 2, 16, text="DLSS 生成失败", fill="#888")
                return
            ih, iw = orig.shape[:2]
            z = self._zoom_value()
            if side_by_side:
                # Two fixed half-width viewports. The split is pinned at 50%, so
                # what a drag changes is the REGION; both halves are cropped
                # identically, which is what keeps the comparison honest.
                half_w = max((cw - SIDE_GAP) // 2, 8)
                if z is None:
                    s = min(half_w / max(iw, 1), ch / max(ih, 1))
                    self._last_fit_scale = s
                else:
                    s = z
                self._split_src = (orig, dlss)
                self._split_sbs = (iw, ih, s, half_w)
            else:
                s = self._view_scale(iw, ih, cw, ch)
                # keep the source at full size: _compose_split crops, then scales
                self._split_src = (orig, dlss)
                self._split_sbs = (iw, ih, s, None)
                self._split_nw = max(int(iw * s), 1)
                self._split_nh = max(int(ih * s), 1)
            self._split_key = key
            self._split_frame = frame
        # Compose every paint, cache hit or not: the layout depends on the pan
        # offset, and the mode must follow THIS call rather than the cached one.
        if side_by_side:
            self._compose_side_by_side(cw, ch)
        else:
            self._compose_split(cw, ch)

    def _compose_split(self, cw, ch):
        """分屏: one picture, the right of the divider taken from the DLSS pass.

        Only the visible slice of each side is scaled and blended, so the cost
        tracks the canvas, not the zoom level.
        """
        orig, dlss = self._split_src
        iw, ih, s, _ = self._split_sbs
        nw, nh = self._split_nw, self._split_nh
        self._pan_max = (max(0.0, (nw - cw) / 2.0), max(0.0, (nh - ch) / 2.0))
        self._clamp_pan()
        ox = (cw - nw) // 2 + int(self.pan_x)
        oy = (ch - nh) // 2 + int(self.pan_y)
        self._drag_nw = nw
        self._drag_offsetx = ox
        self._view_map = (ox, oy, nw, nh, cw, ch)
        self.canvas.delete("all")
        r = self._visible_source(iw, ih, s, ox, oy, cw, ch)
        if r is not None:
            vx0, vy0, vx1, vy1, ix0, iy0, ix1, iy1 = r
            dst_w, dst_h = vx1 - vx0, vy1 - vy0
            o = cv2.resize(orig[iy0:iy1, ix0:ix1], (dst_w, dst_h))
            d = cv2.resize(dlss[iy0:iy1, ix0:ix1], (dst_w, dst_h))
            # the divider is at image column split_x*iw -> this column on canvas
            sx = int(round(ox + self.split_x * nw)) - vx0
            sx = max(0, min(dst_w, sx))
            o[:, sx:] = d[:, sx:]
            o[:, max(sx - 1, 0):min(sx + 1, dst_w)] = [0, 255, 255]   # divider
            self._paint_rgb(o, vx0, vy0)
        self._draw_divider_handle(ox + int(round(self.split_x * nw)),
                                  oy + nh // 2, cw, ch)

    def _compose_side_by_side(self, cw, ch):
        """并排: two fixed halves showing the SAME source region.

        The pan moves a window over the frame, and both halves sample that one
        window -- so what is compared is always the same pixels.
        """
        orig, dlss = self._split_src
        iw, ih, s, half_w = self._split_sbs
        view_w = min(float(iw), half_w / s)
        view_h = min(float(ih), ch / s)
        # pan_* is in screen pixels; turn it into an image-space centre
        cx = iw / 2.0 - self.pan_x / s
        cy = ih / 2.0 - self.pan_y / s
        x0 = int(round(min(max(cx - view_w / 2.0, 0.0), max(iw - view_w, 0.0))))
        y0 = int(round(min(max(cy - view_h / 2.0, 0.0), max(ih - view_h, 0.0))))
        x1 = min(max(x0 + int(round(view_w)), x0 + 1), iw)
        y1 = min(max(y0 + int(round(view_h)), y0 + 1), ih)
        self._pan_max = (max(0.0, (iw * s - half_w) / 2.0),
                         max(0.0, (ih * s - ch) / 2.0))
        self._clamp_pan()
        left = cv2.resize(orig[y0:y1, x0:x1], (half_w, ch))
        right = cv2.resize(dlss[y0:y1, x0:x1], (half_w, ch))
        out = np.empty((ch, cw, 3), np.uint8)
        out[:, :] = (13, 13, 17)                       # stage background (RGB)
        out[:, :half_w] = left
        out[:, cw - half_w:] = right
        self.canvas.delete("all")
        self._paint_rgb(out, 0, 0)
        self._view_map = (0, 0, cw, ch, cw, ch)
        self._drag_nw = None
        self.canvas.create_text(10, 12, anchor="nw", text="原图",
                                fill="#DDDDDD", font=("Microsoft YaHei", 10))
        self.canvas.create_text(cw - 10, 12, anchor="ne", text="DLSS",
                                fill="#DDDDDD", font=("Microsoft YaHei", 10))

    def _draw_divider_handle(self, hx, hy, cw, ch):
        """The grab handle on the divider.

        Dragging the split line is only possible through this handle; anywhere
        else on the picture starts a pan. Without the handle the two gestures
        were indistinguishable, so panning the zoomed image kept moving the
        comparison line instead.
        """
        hx = int(min(max(hx, 18), max(cw - 18, 18)))
        hy = int(min(max(hy, 18), max(ch - 18, 18)))
        self._divider_pt = (hx, hy)
        r = 11
        self.canvas.create_oval(hx - r, hy - r, hx + r, hy + r,
                                fill="#1B1B22", outline="#9A9AA6", width=1)
        # two triangles rather than a glyph: no font dependence
        self.canvas.create_polygon(hx - 5, hy, hx - 1, hy - 4, hx - 1, hy + 4,
                                   fill="#EAEAEF", outline="")
        self.canvas.create_polygon(hx + 5, hy, hx + 1, hy - 4, hx + 1, hy + 4,
                                   fill="#EAEAEF", outline="")

    def _clamp_split_cache(self):
        self._split_key = None

    def _near_divider(self, x, y):
        p = getattr(self, "_divider_pt", None)
        if not p:
            return False
        return abs(x - p[0]) <= 16 and abs(y - p[1]) <= 16

    def on_canvas_press(self, event):
        if self._near_divider(event.x, event.y):
            # grabbed the handle: the offset keeps the line from jumping to
            # the cursor
            self._drag_mode = "divider"
            self._pan_drag = None
            axis = self._drag_offsetx + self.split_x * max(
                getattr(self, "_drag_nw", 1), 1)
            self._divider_grab = event.x - axis
            return
        # anywhere else drags the picture (only does something when zoomed in
        # far enough that it overflows the canvas)
        self._drag_mode = "pan"
        self._pan_drag = (event.x, event.y, self.pan_x, self.pan_y)

    def on_canvas_motion(self, event):
        if getattr(self, "_drag_mode", None) == "divider" and \
                getattr(self, "_drag_nw", None):
            x = event.x - getattr(self, "_divider_grab", 0.0)
            frac = (x - self._drag_offsetx) / max(self._drag_nw, 1)
            self.split_x = max(0.0, min(1.0, frac))
            self.display_view()
            return
        if self._pan_drag is not None and getattr(self, "_pan_max",
                                                  (0.0, 0.0)) != (0.0, 0.0):
            x0, y0, px0, py0 = self._pan_drag
            self.pan_x = px0 + (event.x - x0)
            self.pan_y = py0 + (event.y - y0)
            self._clamp_pan()
            self.display_view()

    def on_canvas_release(self, event=None):
        self._pan_drag = None
        self._drag_mode = None

    def on_canvas_hover(self, event):
        """Cursor feedback: the handle and the pannable stage look different."""
        if self._near_divider(event.x, event.y):
            cur = "sb_h_double_arrow"
        elif getattr(self, "_pan_max", (0.0, 0.0)) != (0.0, 0.0):
            cur = "fleur"
        else:
            cur = ""
        try:
            if str(self.canvas.cget("cursor")) != cur:
                self.canvas.config(cursor=cur)
        except Exception:
            pass

    def _clamp_pan(self):
        """Pan only on axes where the picture actually overflows the canvas.

        A picture narrower than the canvas is locked horizontally, one shorter
        than the canvas is locked vertically, and only when BOTH overflow can it
        be dragged freely. The range also stops at the edges instead of letting
        the picture be flung off screen.
        """
        mx, my = getattr(self, "_pan_max", (0.0, 0.0))
        self.pan_x = 0.0 if mx <= 0 else max(min(float(self.pan_x), mx), -mx)
        self.pan_y = 0.0 if my <= 0 else max(min(float(self.pan_y), my), -my)

    def on_canvas_wheel(self, event):
        self._zoom_step(1 if getattr(event, "delta", 0) > 0 else -1)

    def _zoom_index(self):
        """Ladder index for the current zoom, or None when fitting / off-ladder."""
        if self._zoom_value() is None:
            return None
        lbl = (self.zoom_var.get() or "").strip() if self.zoom_var else ""
        for i, r in enumerate(ZOOM_LADDER):
            if zoom_label(r) == lbl:
                return i
        return None

    def _zoom_step(self, direction):
        """One wheel notch: move between concrete magnifications only.

        Never lands on 适应窗口 -- from fit the first notch anchors on the
        magnification nearest the current effective scale, so it neither jumps
        to a wildly different size nor accidentally zooms back out.
        """
        idx = self._zoom_index()
        if idx is None:
            cur = self._zoom_value()
            if cur is None:
                cur = getattr(self, "_last_fit_scale", 1.0) or 1.0
            idx = min(range(len(ZOOM_LADDER)),
                      key=lambda i: abs(ZOOM_LADDER[i] - cur))
        idx = max(0, min(len(ZOOM_LADDER) - 1, idx + direction))
        self._apply_zoom(ZOOM_LADDER[idx])

    def _apply_zoom(self, ratio):
        if ratio is None:
            if self.zoom_var is not None:
                self.zoom_var.set(ZOOM_DEFAULT)
            self.zoom_ratio = None
            self.pan_x = self.pan_y = 0
        else:
            if self.zoom_var is not None:
                self.zoom_var.set(zoom_label(ratio))
            self.zoom_ratio = ratio
            self.pan_x = self.pan_y = 0
        self._clamp_split_cache()
        self.display_view()

    def on_zoom_change(self):
        self.pan_x = self.pan_y = 0
        self._pan_drag = None
        self._clamp_split_cache()
        self.display_view()

    def reset_view_transform(self):
        self.split_x = 0.5
        if self.zoom_var is not None:
            self.zoom_var.set(ZOOM_DEFAULT)
        self.zoom_ratio = None
        self.pan_x = self.pan_y = 0
        self._pan_drag = None
        self._clamp_split_cache()
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
        if VIEW_ALIAS.get(self.view_var.get(), self.view_var.get()) == "分屏":
            self.split_x = 0.5
        self._pan_drag = None
        self.display_view()

    def on_settings_change(self, event=None):
        # keep the selected layer in step with the controls before anything
        # downstream reads the settings
        self._sync_current_layer()
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
            msg = ("(参数改完立刻生效；皮肤结构需勾选「自动遮罩」才有作用)"
                   if self._dlss_enabled()
                   else "(DLSS 已关闭，以下参数不起作用)")
            n = len(getattr(self, "_layers", []) or [])
            if self._dlss_enabled() and n > 1:
                msg += "    共 %d 层，每帧依次执行（耗时约为单层的 %d 倍）" % (n, n)
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

    def _current_view(self):
        return VIEW_ALIAS.get(self.view_var.get(), self.view_var.get())

    def _refresh_dlss(self):
        self._live_debounce = None
        try:
            dlss_engine.engine2.set_settings(self._collect_settings())
        except Exception as ex:
            self.logln("[DLSS 参数] " + str(ex))
        self._refresh_rtx_note()
        self._live_cache = None
        self._split_frame = -1
        self._split_key = None
        if self._current_view() in ("DLSS", "分屏", "并排"):
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
    def import_batch(self):
        """Open the HandBrake-style source picker over the main window."""
        self.open_import_overlay()

    def open_import_overlay(self):
        """Mask the window with the source picker.

        Deliberately not a second window: the tool is already built and visible
        underneath, so dismissing the overlay just reveals it. Returns nothing;
        the chosen paths arrive through _on_import_done.
        """
        try:
            if getattr(self, "_overlay", None) is not None:
                self._overlay.destroy()
        except Exception:
            pass
        self._overlay = ImportOverlay(self.root, self._on_import_done)
        self._overlay.show()

    def _on_import_done(self, paths):
        self._overlay = None
        if paths:
            self._open_sources(paths)

    def _open_sources(self, paths):
        """Load a batch: all of it becomes switchable, the first shows now.

        It deliberately does NOT queue the rest any more: a folder import is
        usually "let me look through these", and the queue is one explicit click
        away (「加入转换队列」/「列表全部加入队列」).
        """
        paths = [os.path.abspath(p) for p in paths
                 if os.path.isfile(p)]
        if not paths:
            return
        self.sources = paths
        self.source_i = -1
        self._load_source(0)
        if len(paths) > 1:
            self.logln(f"已导入 {len(paths)} 个文件，"
                       f"可用「文件」下拉或 Ctrl+← / Ctrl+→ 切换")

    def enqueue_all_sources(self):
        """Queue every file of the imported list with the current parameters."""
        if not self.sources:
            messagebox.showinfo("队列", "还没有导入文件"); return
        added = 0
        for p in self.sources:
            before = len(self._queue)
            self.enqueue_path(p)
            if len(self._queue) > before:
                added += 1
        self.logln(f"已按当前参数把 {added} 个文件加入队列")

    def _enable_main_dnd(self):
        """Accept file drops onto the main window (needs TkinterDnD root)."""
        try:
            from tkinterdnd2 import DND_FILES
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_main_drop)
        except Exception:
            pass

    def _on_main_drop(self, event):
        try:
            paths = self.root.tk.splitlist(getattr(event, "data", "") or "")
        except Exception:
            return
        vids = []
        for p in paths:
            p = os.path.abspath(p.strip("{}"))
            if os.path.isdir(p):
                vids.extend(list_videos_in_folder(p))
            elif os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS):
                vids.append(p)
        if vids:
            self._open_sources(vids)

    def _load_path(self, p):
        self.pause()
        # Do NOT tear the engine down here: the NGX core init is one-time per
        # process. _ensure_engine() reuses the same session for a same-size video and
        # rebuilds only the feature + textures when the resolution changes.
        self._live_cache = None
        self._last_dlss_frame = -1
        self._split_frame = -1
        # _split_key is what actually caches the composed compare picture, and
        # its key is (frame, canvas size, zoom, mode) -- all of which can match
        # the previous video exactly (every video starts at frame 0). Without
        # this reset the new video kept showing the old one's composite until
        # the timeline was touched.
        self._split_key = None
        self._view_map = None
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

    # ---------- multiple sources ----------
    def _source_label(self, i):
        try:
            name = os.path.basename(self.sources[i])
        except Exception:
            return ""
        return "%d/%d  %s" % (i + 1, len(self.sources), name)

    def _refresh_source_ui(self):
        try:
            self.src_cb.config(values=[self._source_label(i)
                                       for i in range(len(self.sources))])
            if 0 <= self.source_i < len(self.sources):
                self.src_var.set(self._source_label(self.source_i))
            else:
                self.src_var.set("")
            many = len(self.sources) > 1
            set_button_enabled(self.btn_prev, many)
            set_button_enabled(self.btn_next, many)
        except Exception:
            pass

    def _load_source(self, i):
        """Switch the stage to sources[i]."""
        if not self.sources:
            return
        i = max(0, min(len(self.sources) - 1, i))
        self.source_i = i
        self._load_path(self.sources[i])
        self._refresh_source_ui()

    def step_source(self, delta):
        if len(self.sources) < 2:
            return
        self._load_source(self.source_i + delta)

    def _on_source_pick(self, event=None):
        try:
            idx = self.src_cb.current()
        except Exception:
            return
        if idx >= 0 and idx != self.source_i:
            self._load_source(idx)

    def _source_key(self, delta):
        """Ctrl+Left / Ctrl+Right switch files, unless a text field has focus."""
        w = self.root.focus_get()
        if w is not None and w.winfo_class() in (
                "Entry", "TEntry", "Text", "Combobox", "TCombobox",
                "Spinbox", "TSpinbox"):
            return None
        self.step_source(delta)
        return "break"

    # ---------- queue runner ----------
    def start_queue(self):
        waiting = [t for t in self._queue if t["status"] == "等待"]
        if not waiting:
            messagebox.showinfo("队列", "没有等待中的任务"); return
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("忙", "有单独导出正在进行，先等它结束"); return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("缺少 ffmpeg",
                "未找到 ffmpeg。\n请安装 ffmpeg 并加入 PATH，或 pip install imageio-ffmpeg。")
            return
        self._queue_ffmpeg = ffmpeg
        self._queue_running = True
        self._queue_stop = threading.Event()
        self._queue_pause = threading.Event()
        self._queue_pause.set()        # not paused
        self._queue_active = False
        self._queue_current = None
        self._prog_q = queue.Queue()
        self.thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.thread.start()
        self.q_start_btn.config(state="disabled")
        set_button_enabled(self.q_start_btn, False)
        set_button_enabled(self.q_pause_btn, True)
        set_button_enabled(self.q_stop_btn, True)
        self.q_pause_btn.config(text="⏸ 暂停")
        self.logln(f"[队列] 开始，共 {len(waiting)} 个等待任务")
        self.root.after(100, self._poll_queue)

    def toggle_queue_pause(self):
        if not self._queue_running:
            return
        if self._queue_pause.is_set():
            self._queue_pause.clear()
            self.q_pause_btn.config(text="▶ 继续")
            self.logln("[队列] 已暂停（当前帧完成后停住；预览恢复实时 DLSS）")
        else:
            self._queue_pause.set()
            self.q_pause_btn.config(text="⏸ 暂停")
            self.logln("[队列] 继续")

    def stop_queue(self):
        if not self._queue_running:
            return
        self._queue_stop.set()
        self._queue_pause.set()        # unblock if paused
        self.logln("[队列] 正在停止（当前任务完成后停）…")

    def _poll_queue(self):
        """Main-thread pump for the queue worker's messages."""
        idle = False
        try:
            while True:
                msg = self._prog_q.get_nowait()
                kind = msg[0]
                if kind == "task_start":
                    _, tid = msg
                    t = self._task_by_id(tid)
                    if t is not None:
                        t["status"] = "转换中"; t["progress"] = 0.0
                        self._queue_current = t
                        self._queue_active = True
                        self._live_cache = None
                        self._split_key = None
                        self.pbar["maximum"] = max(t["n"], 1); self.pbar["value"] = 0
                        self.set_status(f"队列 {t['name']} …")
                        self.set_stats(total=t["n"])
                        self.logln(f"[队列] 开始: {t['name']} → "
                                   f"{os.path.basename(t['out_path'])}")
                    self._queue_refresh()
                elif kind == "progress":
                    _, tid, i, total, speed, elapsed, eta = msg
                    t = self._task_by_id(tid)
                    if t is not None:
                        t["progress"] = (i / total) if total else 0.0
                    if self._queue_current is not None and \
                            self._queue_current["id"] == tid:
                        self.set_progress(i, total, "队列")
                        self.set_stats(done=i, total=total, fps=speed,
                                       elapsed=elapsed, eta=eta)
                    self._queue_refresh_light(tid)
                elif kind == "log":
                    self.logln(str(msg[1]))
                elif kind == "task_done":
                    _, tid, result, out_path = msg
                    t = self._task_by_id(tid)
                    if t is not None:
                        if result == "cancelled":
                            t["status"] = "已取消"
                        else:
                            t["status"] = "完成" if result else "失败"
                            t["progress"] = 1.0 if result else t["progress"]
                        self.logln(("已导出: " if t["status"] == "完成" else
                                    ("已取消: " if t["status"] == "已取消" else "失败: "))
                                   + str(out_path))
                    if self._queue_current is not None and \
                            self._queue_current["id"] == tid:
                        self._queue_current = None
                        self._queue_active = False
                        self._live_cache = None
                        self._split_key = None
                        self.pbar["value"] = 0
                    self._queue_refresh()
                    try:
                        self.display_view()
                    except Exception:
                        pass
                elif kind == "queue_done":
                    _, completed, failed, cancelled = msg
                    self._queue_running = False
                    self._queue_active = False
                    self._queue_current = None
                    self._live_cache = None
                    self._split_key = None
                    self.pbar["value"] = 0
                    self.set_status(f"队列完成（成功 {completed} / 失败 {failed}"
                                    + (f" / 取消 {cancelled}" if cancelled else "") + "）")
                    self.q_start_btn.config(state="normal")
                    set_button_enabled(self.q_start_btn, True)
                    set_button_enabled(self.q_pause_btn, False)
                    set_button_enabled(self.q_stop_btn, False)
                    self.q_pause_btn.config(text="⏸ 暂停")
                    self._queue_refresh()
                    messagebox.showinfo("队列完成",
                                        f"成功 {completed} / 失败 {failed}"
                                        + (f" / 取消 {cancelled}" if cancelled else ""))
                    idle = True
                elif kind == "queue_idle":
                    # paused with nothing active: release the GPU for preview
                    if self._queue_active:
                        self._queue_active = False
                        self._live_cache = None
                        self._split_key = None
                        try:
                            self.display_view()
                        except Exception:
                            pass
        except queue.Empty:
            pass
        if not idle and self._queue_running:
            self.root.after(150, self._poll_queue)

    def _task_by_id(self, tid):
        return next((t for t in self._queue if t["id"] == tid), None)

    def _queue_refresh_light(self, tid):
        """Update just one row's progress text (cheap; called per tick)."""
        try:
            t = self._task_by_id(tid)
            if t is None or not self.qtree.exists(str(tid)):
                return
            self.qtree.set(str(tid), "status",
                           "转换中 %.0f%%" % (t["progress"] * 100))
        except Exception:
            pass

    def _queue_worker(self):
        """Background: run waiting tasks serially on the shared GPU engine."""
        q = self._prog_q
        completed = failed = cancelled = 0
        while not self._queue_stop.is_set():
            nxt = next((t for t in self._queue if t["status"] == "等待"), None)
            if nxt is None:
                break
            # honour pause BEFORE claiming the GPU
            self._queue_pause.wait()
            if self._queue_stop.is_set():
                break
            q.put(("task_start", nxt["id"]))
            # verdict comes back synchronously from the tagger ("ok" / "fail"
            # / "cancelled"); the matching task_done message is already queued
            # for _poll_queue, which assigns the same status + log line there.
            verdict = self._run_queue_task(nxt)
            if verdict == "ok":
                completed += 1
            elif verdict == "cancelled":
                cancelled += 1
            else:
                failed += 1
            # between tasks: drop the GPU claim so the preview breathes
            self._queue_active = False
            q.put(("queue_idle",))
        # mark leftovers cancelled only when the user hit stop
        if self._queue_stop.is_set():
            for t in self._queue:
                if t["status"] == "等待":
                    pass    # keep them waiting for the next run
        q.put(("queue_done", completed, failed, cancelled))

    def _run_queue_task(self, task):
        """Run one queue task through the shared export implementation.

        The impl writes ("progress"/"log"/"done") into self._prog_q; here it
        is temporarily swapped for a tagger that routes them to the shared
        queue with the task id attached. The single-export path is untouched
        because the queue worker never runs concurrently with it (guarded in
        export_dlss / start_queue).
        """
        q = self._prog_q
        ffmpeg = self._queue_ffmpeg
        settings = dict(task["settings"])
        enc, enc_args, tag = encoder_of(task["enc_label"])
        n, fps, w, h = task["n"], task["fps"], task["w"], task["h"]
        ow, oh, out_path = task["ow"], task["oh"], task["out_path"]
        tid = task["id"]
        box = {}
        paused_note = {"shown": False}

        def wait_if_paused():
            if not self._queue_pause.is_set():
                if not paused_note["shown"]:
                    q.put(("log", f"[队列] {task['name']} 已暂停等待"))
                    paused_note["shown"] = True
                self._queue_pause.wait()
                paused_note["shown"] = False
            return not self._queue_stop.is_set()

        class _Tagger:
            def put(self, m):
                if not m:
                    return
                kind = m[0]
                if kind == "progress":
                    _, i, total, speed, elapsed, eta = m
                    q.put(("progress", tid, i, total, speed, elapsed, eta))
                elif kind == "log":
                    q.put(m)
                elif kind == "done":
                    _, ok, path = m
                    if self._queue_stop.is_set() and not ok:
                        box["verdict"] = "cancelled"
                        q.put(("task_done", tid, "cancelled", path))
                    else:
                        box["verdict"] = "ok" if ok else "fail"
                        q.put(("task_done", tid, bool(ok), path))

        saved = self._prog_q
        self._prog_q = _Tagger()
        try:
            self._export_job_impl(ffmpeg, enc, enc_args, tag, out_path,
                                  settings, n, fps, w, h, task["src"],
                                  pause_hook=wait_if_paused,
                                  stop_hook=lambda: self._queue_stop.is_set())
        finally:
            self._prog_q = saved
        return box.get("verdict", "fail")

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
        if self._queue_running:
            messagebox.showinfo("忙", "队列正在转换，先停止队列再单独导出"); return
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
            nlay = len(self._dlss_layers())
            chain.append("DLSS×%d层" % nlay if nlay > 1 else "DLSS")
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
            target=self._export_job_impl,
            args=(ffmpeg, enc, enc_args, tag, out_path, settings, n, fps, w, h,
                  self.video),
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

    def _export_job_impl(self, ffmpeg, enc, enc_args, tag, out_path, settings,
                           n, fps, w, h, src_path, pause_hook=None, stop_hook=None):
        """Pipelined export: decode -> [RTX Video stages] -> DLSS -> ffmpeg stdin.

        Shared by single export and the queue worker. pause_hook (if given) is
        called once per frame and must block while paused, returning False to
        abort; stop_hook aborts when it returns True. Both default to
        no-op so the single-export path is unchanged.

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
        layers = [dict(l) for l in (settings.get('layers') or []) if l]
        multi = use_dlss and len(layers) > 1
        # The chained path is used whenever any RTX stage is on, and also when
        # DLSS is switched off entirely (a pure pass-through re-encode).
        chained = rtx_video.Pipeline.needs_rtx(settings) or not use_dlss
        if multi:
            # worth saying out loud: this is N passes per frame, i.e. N times the
            # neural cost, and the double-buffered overlap no longer applies
            self._prog_q.put(("log", "多层 DLSSNR：每帧依次执行 %d 层"
                                     "（耗时约为单层的 %d 倍）" % (len(layers), len(layers))))

        pipe = None
        out_w, out_h = w, h
        in_fmt = "rgba"
        if chained:
            try:
                pipe = rtx_video.Pipeline()
                # ChainEngine, not engine2: it applies THIS job's parameters
                # (including the whole layer chain) instead of relying on
                # whatever the engine happened to hold -- which is how a queued
                # task with different settings used to export with the wrong ones.
                out_w, out_h, in_fmt = pipe.configure(
                    w, h, settings,
                    dlss_engine=dlss_engine.ChainEngine(settings)
                    if use_dlss else None)
            except Exception as ex:
                self._prog_q.put(("log", "[RTX Video] " + str(ex)))
                self._prog_q.put(("done", False, out_path))
                return
            stages = [x for x in [
                rtx_video.QUALITY_NAMES.get(int(settings.get('vsr_quality', 0)), "")
                if int(settings.get('vsr_quality', 0)) else "",
                "DLSS×%d层" % len(layers) if (use_dlss and len(layers) > 1)
                else ("DLSS" if use_dlss else ""),
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
                "-i", src_path,
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
            cap = cv2.VideoCapture(src_path)
            cancelled = False

            def checkpoint():
                """Pause / stop hook, checked once per emitted frame."""
                nonlocal cancelled
                if stop_hook is not None and stop_hook():
                    cancelled = True
                    return False
                if pause_hook is not None and not pause_hook():
                    cancelled = True
                    return False
                return True

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
                    if not checkpoint():
                        err_msg = "用户取消"
                        break
                    report_progress()
                    i += 1
                cap.release()
                if err_msg is None:
                    report_progress(force=True)
            elif multi:
                # Multi-layer plain DLSS. Each frame needs N dependent passes, so
                # the submit/fetch double buffer has nothing to overlap: the chain
                # runs synchronously, one full round trip per layer. The final
                # layer writes RGBA, so ffmpeg still gets -pix_fmt rgba and the
                # rest of the pipeline is unchanged.
                while True:
                    ok, fr = cap.read()
                    if not ok:
                        break
                    if eng is None:
                        eng = dlss_engine.engine2
                        eng.set_settings(settings)
                        eng.ensure(w, h)
                    try:
                        o = eng.process_chain(fr, layers, reset=(i == 0),
                                              out_rgba=True)
                    except Exception as ex:
                        err_msg = "帧 %d: %s" % (i, ex)
                        break
                    if o is None:
                        err_msg = f"帧 {i}: 多层级处理失败，终止导出"
                        break
                    emit(o, fr, i)
                    emitted += 1
                    if state["err"]:
                        err_msg = "ffmpeg 管道中断: " + state["err"]
                        break
                    if proc.poll() is not None:
                        tail = b"".join(stderr_chunks).decode("utf-8", "replace")[-600:]
                        err_msg = (f"ffmpeg 提前退出 (code={proc.returncode})，"
                                   f"已处理 {emitted} 帧\n" + tail)
                        break
                    if not checkpoint():
                        err_msg = "用户取消"
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
                        if not checkpoint():
                            err_msg = "用户取消"
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
                        if not checkpoint():
                            err_msg = "用户取消"
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
    # startup: CLI sources win, otherwise the HandBrake-style picker.
    #   python gui.py clip.mp4            open directly, skip the picker
    #   python gui.py --source-dir D:\v   open every video in the folder
    #   python gui.py --no-picker         empty main window (import later)
    raw = list(sys.argv[1:])
    cli_sources = []
    i = 0
    while i < len(raw):
        a = raw[i]
        if a == "--source" and i + 1 < len(raw):
            cli_sources.append(raw[i + 1]); i += 2
        elif a == "--source-dir" and i + 1 < len(raw):
            cli_sources.extend(list_videos_in_folder(raw[i + 1])); i += 2
        elif a.startswith("--"):
            i += 1
        else:
            p = os.path.abspath(a)
            if os.path.isdir(p):
                cli_sources.extend(list_videos_in_folder(p))
            elif os.path.isfile(p):
                cli_sources.append(p)
            i += 1
    # TkinterDnD root when available so the main window itself accepts drops;
    # plain Tk otherwise (the picker buttons still work).
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    root.title("DLSS5 工具 — 神经渲染预览与导出")
    root.configure(bg=C_BG)
    app = App(root)
    sources = [p for p in cli_sources if os.path.isfile(p)]
    if sources:
        app._open_sources(sources)
    elif "--no-picker" not in raw:
        # One window from the start: the picker is an overlay on the built UI,
        # so there is nothing to withdraw, wait on, or map a second time.
        app.open_import_overlay()
    root.mainloop()


if __name__ == "__main__":
    main()
