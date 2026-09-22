# dlssnr-toolkit

**用 NVIDIA 的神经渲染网络（DLSS "NR"）给视频做逐帧降噪 / 去块 / 细节重建，并导出成片。**

Windows 上的离线视频处理工具：导入 → 实时预览（原图 / DLSS / 分屏 / 并排）→ 调参 → 选编码器 → 导出带音轨的成片，也可攒成队列批量转换。

- 🖥️ **图形界面** —— 实时预览、逐帧对比、参数即时生效
- 🚀 **GPU 直通** —— 去掉 CPU 中转，720p 5.2 ms / 1080p 9.5 ms 每帧
- 📼 **无损音轨** —— 导出完整保留原音频
- 🎞️ **编码器可选** —— H.264 / H.265 / AV1 硬编 + x264 / x265 软编
- 🌈 **RTX Video 增强** —— AI 放大、降噪 / 去模糊 / 高码率、SDR→HDR10（各自独立开关）

> 📦 **想边播边看、实时调参？** 那需要配套的 DirectShow 滤镜，
> 在另一个仓库：**[dlssnr-filter](https://github.com/CyanKe/dlssnr-filter)**。
> 本仓库负责**离线导出**；滤镜负责**播放器里实时处理**。两仓库各自独立、互不依赖
> （滤镜已内置自己的 GPU 引擎，不再共用本仓库的引擎）。

> ⚠️ **本仓库不含任何 NVIDIA 二进制。**
>
> 核心运行库 `nvngx_dlssnr.dll` 目前**没有官方公开发布渠道** —— 本工具所依赖的是
> **社区构建版**，需要你**自行获取并放入 `app\`**。这不是 NVIDIA 官方分发的文件，
> 本仓库不提供、也不得随本仓库再分发。详见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

---

## 效果与性能

在我们的测试机上测得（RTX 5070 Ti Laptop，驱动 616.56）：

| 分辨率 | 每帧耗时 | 折算帧率 |
| --- | --- | --- |
| 1280×720 | 5.2 ms | 193 fps |
| 1920×1080 | 9.5 ms | 105 fps |
| 1920×1440 | 17.1 ms | 58 fps |

以上是**本工具（Python 直接调用引擎）**的实测值。
滤镜那边是另一套独立引擎（NV12 端到端走 GPU），性能数据见 dlssnr-filter 仓库。

> ⚠️ 历史注记：早期滤镜曾复用本仓库的 `dlssnr_host2.dll`（CPU 做
> `NV12 → BGR24 → 引擎 → BGR24 → NV12` 往返转换，同 1920×1440 下约 14.3 ms）。
> 该路径已随滤镜内置 GPU 引擎被移除，上面的旧数字**仅备查、以滤镜仓库现状为准**。

**导出实测**：1080p / 1440² @60fps，HEVC NVENC + 音轨，约 91–94 fps。

去噪强度实测（平坦噪声场，标准差 20，稳态）：

| 强度 | 0% | 25% | 50% | 75% | 100% |
| --- | --- | --- | --- | --- | --- |
| 去噪比例 | 0.0% | 2.2% | 4.2% | 6.0% | 7.7% |

---

## 快速开始

### 1. 装依赖

```
tools\setup.bat
```

需要 Python 3、`numpy`、`opencv-python`；视频读写依赖 **FFmpeg**（需在 `PATH` 中）。

### 2. 准备 NVIDIA 运行库

把下面这些放进 **`app\`** 目录（与 Python 模块同级）：

```
app\nvngx_dlssnr.dll      ← DLSS "NR" 网络，核心依赖
app\nvngxruntime.dll
```

可选（只有用 RTX Video 的放大 / TrueHDR 时才需要）：

```
app\NVVideoEffects.dll
app\NVCVImage.dll
app\nvVFXVideoSuperRes.dll
app\nvngx_vsr.dll
app\nvngx_truehdr.dll
```

它们来自你本机的 NVIDIA 驱动 / NVIDIA 官方分发渠道。**本仓库不附带，也不得随本仓库再分发。**

### 3. 跑起来

```
tools\run.bat
```

或手动：

```
cd app
python gui.py
```

自检（不开窗口，验证 GPU 通路是否可用）：

```
cd app
python gui.py --selftest
```

---

## 两个仓库，互不依赖

本工具**不带** DirectShow 滤镜。想在播放器里边播边看，需要另一个仓库：

| 仓库 | 做什么 | 什么时候用 |
| --- | --- | --- |
| **dlssnr-toolkit**（本仓库） | 离线导出：预览、调参、编码、带音轨输出 | 要把视频**存成文件** |
| **[dlssnr-filter](https://github.com/CyanKe/dlssnr-filter)** | 播放器实时滤镜 + 控制面板 | 要**边播边看**、实时调参 |

两者源码、引擎各自独立、互不依赖：

```
   ┌──────────────────┐                   ┌────────────────────┐
   │ 本仓库            │                   │ dlssnr-filter       │
   │ app\gui.py       │                   │ dlssnr_dshow.dll    │
   │ + dlssnr_host2.dll│                   │ (自带 GPU 引擎)      │
   │ (离线批处理)      │                   │ (播放器实时)         │
   └──────────────────┘                   └────────────────────┘
          两仓库之间无源码 / 二进制约定，各自独立演进。
```

> ⚠️ 历史注记：早期两仓库曾共用 `dlssnr_host2.dll` 的二进制接口
> （`dlssnr2_init / process / ...`，曾记于滤镜仓库的 `docs/ENGINE_INTERFACE.md`）。
> 滤镜内置引擎后该约定已废止、文档已删除；早期共用引擎的那段历史**仅备查**。

**注意：导出的视频不经过 DirectShow。** 导出走 Python 直接调用引擎，
滤镜只在播放器内部工作。两者可以同时用，互不干扰。

---

## 目录结构

```
dlssnr-toolkit/
├── app/                    运行时（Python + 它加载的所有 DLL，必须同目录）
│   ├── gui.py              tkinter 界面 + 导出流水线
│   ├── dlss_engine.py      DLSSNR 的 ctypes 封装
│   ├── rtx_video.py        RTX Video（放大 / 降噪 / 去模糊 / HDR）封装
│   └── *.dll               ★ 本机运行库，不进仓库
├── src/                    源码
│   ├── dlssnr_host2.cpp    DLSSNR 宿主（D3D12，GPU 直通，本仓库自用）
│   ├── vfx_host.cpp        RTX Video 宿主
│   └── truehdr_host.cpp    SDR→HDR10 宿主
├── tools/
│   ├── build.bat           编译全部（缺 deps\ 时自动跳过）
│   ├── run.bat             启动界面
│   └── setup.bat           安装 Python 依赖
├── docs/
│   ├── README.txt          详细功能说明与踩坑记录（中文，内容最全）
│   ├── IMPROVEMENT_REPORT.md  优化过程报告（含 25 张图）
│   └── THIRD_PARTY.md      ★ 第三方组件与再分发说明
├── deps/                   ★ NVIDIA SDK 本机缓存，不进仓库
└── build/                  编译中间产物，不进仓库
```

DirectShow 滤镜不在本仓库 —— 见 [dlssnr-filter](https://github.com/CyanKe/dlssnr-filter)。

---

## 功能一览

**界面**

单窗口、深色主题，顶部四个分页；处理参数是一块**浮在画面上**的可拖动卡片，预览时不用在画面和滑块之间二选一。

| 功能 | 说明 |
| --- | --- |
| 分页 | 预览 / 导出·队列 / 日志 —— 导出与日志不占对比画面；输出设置与队列同页 |
| 导入 | 启动时按 HandBrake 的做法**盖在窗口上**：打开文件 / 打开文件夹 / 拖放 |
| 参数卡片 | 浮在预览画面上，可拖动、可折叠，标题栏 `⠿` 拖动、`▾` 收起 |
| 显示模式 | 原图 / DLSS / 分屏 / 并排 |
| 对比线 | 分屏的分界线只能通过**线中间的圆形手柄**拖动；画面其他位置拖动是平移。并排固定左右各一半 |
| 缩放平移 | 滚轮 5%–400% 逐档（18 档，**不会跳到"适应窗口"**）；`复位` 回到适应窗口。放大到超出窗口才能拖动，且**只在溢出的方向**可拖：宽度不溢出就锁左右，高度不溢出就锁上下，两向都溢出才可随意拖 |
| 多文件 | 一次导入多个（文件夹 / 多选 / 拖放）后，用「文件」下拉或 `Ctrl+←` / `Ctrl+→` 在对比界面直接切换 |
| 播放 | 按视频帧率逐帧实时处理 |
| 预览处理上限 | 不限制 / 720p / 1080p（默认）/ 1440p —— 只影响预览，导出始终用原始分辨率 |
| DLSS 参数 | 风格（默认·自然·电影·风格3）、强度 0–1、局部色调 0–2、局部结构 0–2、皮肤结构 0–2、自动遮罩 |
| 多层 DLSSNR | 参数卡的「层」可加到 8 层，**每层参数独立**，每帧按 1→N 依次执行。每层有**自己的 NGX feature 与时域历史**（共用一个 feature 会放大 3.6 倍的大面积闪烁），整条链在一条命令列表里跑完 |
| RTX Video | AI 放大（×1.5 / ×2 / ×3 / ×4 / 各档位）、降噪 / 去模糊 / 高码率（各 4 档） |
| HDR | SDR → HDR10（PQ / BT.2020，中灰与峰值亮度可调，饱和度 / 对比度 ±100%） |
| 编码器 | H.264 / H.265 / AV1 硬编 + x264 / x265 软编 |
| 转换队列 | 每个视频带自己的参数快照排队；双击任务可把该任务的参数载回界面继续调。多文件导入后可用「列表全部加入队列」一次按当前参数排队 |

> 队列在后台顺序转换（神经引擎是进程单例，只能串行），期间可以继续预览。
> 预览与导出共用同一个 GPU 会话，所以由一把「占用锁」决定归属：**任务运行
> 期间预览自动只显示原图**，改参数、改「预览上限」都不会打扰正在写的文件；
> 任务之间（或开始之前）暂停，实时 DLSS 立刻回来。
> 之前没有这把锁时，导出中动「预览上限」会把正在跑的会话拆掉——实测串行丢帧
> 直到 `提交失败，终止导出`，甚至进程内 access violation；细节见
> [docs/README.txt](docs/README.txt) 的「已修复的坑」第 15、16 条。
>
> **皮肤结构需要配合「自动遮罩」才有作用**——这是网络本身的实测行为
> （遮罩关闭时皮肤结构 0→1 逐字节相同），不是界面限制。
> 强度上限 1.0 也是实测结果：>1.0 是死区，所以滑块只给到 100%。

**文件名**

导出文件带自解释后缀，例如：

```
video_DLSS.mp4                  仅 DLSSNR
video_reencode.mp4              仅重编码
video_VSR-Med_x2_640x360.mp4    RTX 放大
video_DLSS_DB-High_HDR10.mp4    DLSSNR + 去模糊 + HDR10
```

同名文件会弹窗二次确认（覆盖 / 自动加后缀 / 取消）。

---

## 编译

需要 Visual Studio 2022（C++ 工作负载）。

```
tools\build.bat         编译全部（缺 deps\ 时自动跳过）
tools\build.bat host    只编译 dlssnr_host2.dll
```

`build.bat` 会自动定位 `vcvars64.bat`，并按 `deps\` 里是否存在对应 SDK 决定跳过哪些目标。
中间产物写入 `build\`，不污染源码目录。

| 目标 | 需要的 SDK | 放哪里 |
| --- | --- | --- |
| `dlssnr_host2.dll` | NVIDIA NGX 头文件 + 静态库 | `deps\sdk_include\`、`deps\sdk_lib\` |
| `vfx_host.dll`、`truehdr_host.dll` | 官方 RTX Video SDK 1.1 | `deps\rtx_video_sdk\` |

> **本仓库的 `dlssnr_host2.dll` 仅供本仓库自用。** 滤镜已内置自己的 GPU 引擎，
> 不再需要这个 DLL；滤镜编译需要它自己的 NVIDIA NGX SDK，详见滤镜仓库的说明。

---

## 授权

本项目自有代码采用 **MIT**（见 [`LICENSE`](LICENSE)）。

本项目基于 **[purkatyy/DLSS5-](https://github.com/purkatyy/DLSS5-)**（作者 **ylso0**，MIT 许可）
衍生。`app\gui.py` 与 `app\dlss_engine.py` 源自该项目并有大幅改写，上游版权声明已依 MIT
要求保留在 `LICENSE` 及各文件头部。

本项目新增的部分（RTX Video 集成、GPU 直通宿主、导出流水线扩展等）
均为原创，不包含任何 NVIDIA 代码。

DirectShow 滤镜与控制面板是**独立的原创作品**，已拆分到
[dlssnr-filter](https://github.com/CyanKe/dlssnr-filter) 仓库，
其代码不衍生自上游，因此那里**只有 Cyanke 的署名**。

**MIT 只覆盖本仓库里的源码。** 它不覆盖、本仓库也不分发任何 NVIDIA 软件 ——
包括 NGX/DLSS 运行库、`nvngx_dlssnr.dll`、RTX Video SDK 的 DLL，
以及 SDK 头文件与静态库。这些仍属 NVIDIA 所有，受其自身条款约束。

使用前请阅读 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

---

## 致谢

- **[purkatyy/DLSS5-](https://github.com/purkatyy/DLSS5-)**（ylso0）—— 本项目的起点。
  最初那份 test4 工具确立了「零引导 + Feature 18」这条路可行。
- **[Magpie](https://github.com/Blinue/Magpie)** —— RTX Video 与 DLSSNR 的集成思路参考，
  以及 `docs/THIRD_PARTY.md` 的文档结构来源。本项目**没有复制其源码**。
- **NVIDIA** —— DLSS 与 RTX Video 技术的所有者。本项目与 NVIDIA 无隶属关系，亦未获其背书。

---

## 免责声明

仅供学习与个人研究使用。DLSS、RTX Video 是 NVIDIA Corporation 的商标与技术。

使用者需自行确认其使用方式符合所在地法律及 NVIDIA 的许可条款。
仓库作者不对因使用本软件产生的任何后果承担责任。

