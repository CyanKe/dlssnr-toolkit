# Third-party components and redistribution / 第三方组件与再分发

This document is an engineering inventory, **not legal advice**. A component being
downloadable or usable on your own machine does **not** by itself grant permission to
redistribute it in this project's source tree or in binary releases.

本文是工程侧清单，**不构成法律意见**。某个组件可以下载或在本机使用，并不自动表示可以把
它再次放进本项目源码或二进制 Release。

---

## Project source / 项目源码

- The source in `src/`, `app/*.py`, and `tools/` is licensed under **MIT** (see `LICENSE`).
- The repository must **not** contain: NVIDIA runtime DLLs, NVIDIA SDK headers or static
  libraries, model files, logs, `deps/`, or build output (`*.obj`, `*.lib`, `*.exp`).
- `deps/` is a **local development cache**. It is not part of the distributable source.
  It is listed in `.gitignore` for that reason.

- `src/`、`app/*.py`、`tools/` 中的源码采用 **MIT** 许可（见 `LICENSE`）。
- 仓库**不得**包含：NVIDIA 运行库 DLL、NVIDIA SDK 头文件或静态库、模型文件、日志、
  `deps/`、以及编译产物（`*.obj`、`*.lib`、`*.exp`）。
- `deps/` 只是**本机开发依赖缓存**，不属于可分发源码，因此已在 `.gitignore` 中排除。

---

## Component matrix / 组件清单

| Component | Local use | In this source repo | Public binary redistribution |
| --- | --- | --- | --- |
| dlssnr-toolkit's own code (`src/`, `app/*.py`, `tools/`) | MIT | **Yes — that is the point** | Allowed under MIT terms |
| `nvngx_dlssnr.dll` — **community build**, no official public release | The engine; must be placed in `app\` by the user | **No — gitignored** | **Do not redistribute.** Neither NVIDIA's terms nor the community build's provenance permit it; treat as internal-use only |
| NVIDIA NGX runtime — `nvngx_vsr.dll`, `nvngx_truehdr.dll`, `nvngxruntime.dll` | Runs locally once present | **No — gitignored** | **Not permitted.** `LicenseRef-NvidiaProprietary` |
| NVIDIA NGX SDK — headers under `deps/sdk_include/`, `nvsdk_ngx_s.lib` | Needed to compile `dlssnr_host2.dll` | **No — gitignored** | **Not permitted** without an express NVIDIA agreement |
| NVIDIA RTX Video SDK 1.1 — `NVVideoEffects.dll`, `NVCVImage.dll`, `nvVFXVideoSuperRes.dll`, `deps/rtx_video_sdk/` | Runs locally once present | **No — gitignored** | **Not permitted** as part of this package |
| `numpy`, `opencv-python` | `pip install` | **No** (declared as dependencies) | Ship via the normal package index, not vendored |
| FFmpeg (`ffmpeg.exe` on PATH) | User-installed | **No** | Not redistributed here |

> **The DirectShow filter is not in this repository.** It lives in the separate
> **dlssnr-filter** repository, under its own MIT licence and with no upstream
> attribution obligations. The two repositories share only the binary interface of
> `dlssnr_host2.dll`; see that repository's `docs/ENGINE_INTERFACE.md`.
>
> **滤镜不在本仓库。** 它位于独立的 **dlssnr-filter** 仓库，采用自己的 MIT 许可，
> 且不承担上游署名义务。两个仓库之间唯一的约定是 `dlssnr_host2.dll` 的二进制接口。

> **Note on `nvngx_dlssnr.dll`:** this is the one component with no clean answer. It is
> NVIDIA's DLSS "NR" snippet, but the specific binary this project was developed against
> is a **community build** rather than an official NVIDIA release, so we cannot point at
> a licence that clearly grants redistribution — and we cannot point at an official
> download either. The safe position, and the one this repository takes, is: the user
> supplies it, the repository never contains it, and no binary package is published that
> bundles it.
>
> **关于 `nvngx_dlssnr.dll`：** 这是唯一一个没有干净答案的组件。它本身是 NVIDIA 的
> DLSS「NR」组件，但本项目开发所用的那个具体文件是**社区构建版**而非 NVIDIA 官方发行，
> 因此既找不到明确允许再分发的许可，也找不到官方下载渠道。本仓库采取的安全立场是：
> 由用户自行提供、仓库永不包含它、也不发布任何捆绑它的二进制包。

The controlling texts are the exact licence files shipped with each SDK/runtime.
The relevant NVIDIA notices are reproduced in the component headers themselves, e.g.
`nvsdk_ngx.h`:

> SPDX-License-Identifier: LicenseRef-NvidiaProprietary
> NVIDIA CORPORATION, its affiliates and licensors retain all intellectual property and
> proprietary rights in and to this material, related documentation and any modifications
> thereto. Any use, reproduction, disclosure or distribution of this material and related
> documentation without an express license agreement from NVIDIA CORPORATION or its
> affiliates is strictly prohibited.

Note that **use** is listed alongside distribution. That is why `deps/` and every NVIDIA
binary are excluded from the repository: the intent is that the *user* obtains them under
their own NVIDIA entitlement, exactly as they would for any other DLSS-enabled application.

注意其中把 **use** 与 distribution 并列。这正是 `deps/` 与所有 NVIDIA 二进制都被排除在
仓库之外的原因：设计意图是让**用户自行**依其 NVIDIA 授权取得这些文件，与其他任何启用
DLSS 的程序一致。

---

## What the user must supply / 需要用户自备的部分

To run the tool you need, on your own machine:

1. An NVIDIA RTX GPU with a recent driver (this is where the NGX runtimes come from).
2. `nvngx_dlssnr.dll` — the DLSS "NR" snippet. **Not distributed here, and there is
   no official public download for it.** The build this project is developed against
   is a **community build**, not an NVIDIA release, so its provenance and its
   redistribution status are both unclear. Obtain it yourself; do not expect this
   repository to ship it or to tell you where to get it.
3. Python 3 with `numpy` and `opencv-python` (`tools\setup.bat`).
4. FFmpeg on `PATH` for video I/O.
5. Optional: the official RTX Video SDK 1.1, only if you want the VSR / TrueHDR paths.
   Source: <https://catalog.ngc.nvidia.com/orgs/nvidia/multimedia/models/dlpp/versions/1.5>

Place the engine DLL **next to the Python modules in `app\`** — the loaders in
`dlss_engine.py` and `rtx_video.py` resolve them relative to their own file.

要让工具运行，你需要在自己的机器上准备：

1. NVIDIA RTX 显卡 + 较新的驱动（NGX 运行库即来自驱动）。
2. `nvngx_dlssnr.dll` —— DLSS「NR」组件。**本仓库不附带，而且它没有官方公开下载渠道。**
   本项目开发所用的是**社区构建版**，既非 NVIDIA 官方发行，其来源与再分发地位都不明确。
   请自行取得；不要指望本仓库提供它，也不要指望本仓库告诉你从哪里获取。
3. Python 3 以及 `numpy`、`opencv-python`（可运行 `tools\setup.bat`）。
4. FFmpeg 在 `PATH` 中，用于视频读写。
5. 可选：官方 RTX Video SDK 1.1（仅在需要 VSR / TrueHDR 时）。
   来源：<https://catalog.ngc.nvidia.com/orgs/nvidia/multimedia/models/dlpp/versions/1.5>

把 NVIDIA 的 DLL 放在 **`app\` 目录下、与 Python 模块同级** —— `dlss_engine.py` 与
`rtx_video.py` 是按自身文件位置解析它们的。

---

## Pre-release checklist / 发布前检查

- [ ] `git ls-files` lists **no** `.dll`, `.lib`, or `.pdf`.
- [ ] `deps/` is absent from the index (`git ls-files deps` prints nothing).
- [ ] `app/` contains the Python sources but **no** DLLs in the index.
- [ ] No logs (`*.log`, `host2_log.txt`, `vfx_log.txt`, `thdr_log.txt`) are tracked.
- [ ] `dlssnr-filter/` is absent from the index — it has its own repository.
- [ ] `build/` is absent from the index.
- [ ] `LICENSE` and this file are present and accurate for the shipped revision.

- [ ] `git ls-files` 中**没有**任何 `.dll`、`.lib`、`.pdf`。
- [ ] `deps/` 不在索引中（`git ls-files deps` 应无输出）。
- [ ] `app/` 里的 Python 源码被跟踪，但索引中**没有** DLL。
- [ ] 没有任何日志（`*.log`、`host2_log.txt`、`vfx_log.txt`、`thdr_log.txt`）被跟踪。
- [ ] `dlssnr-filter/` 不在索引中 —— 它有自己的仓库。
- [ ] `build/` 不在索引中。
- [ ] `LICENSE` 与本文存在，且与发布版本一致。
