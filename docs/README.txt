DLSS5 工具（GPU 直通版）
========================
导入视频 → 实时预览(原图/DLSS/分屏/并排) → 调风格/强度/本地色调/本地结构 →
选编码器 → 导出 DLSS 视频(带音轨)，或攒成转换队列批量跑。

【界面】(2026-09 改版)
  单窗口、深色主题，顶部三个分页：
    预览 / 导出 · 队列 / 日志
  - 导出与日志不占对比画面；输出设置和转换队列同页
    (两者本来就是一条流程: 调好 → 加入队列 → 批量跑，而且导出单独一页太空)
  - 处理参数是一块【浮在预览画面上的卡片】：可拖动(标题栏 ⠿)、可折叠(▾)、
    可用底部"参数面板"复选框整体收起
  - 启动时不另开窗口：导入界面像 HandBrake 那样【盖在主窗口上】，
    选完直接露出工具(打开文件 / 打开文件夹 / 拖放)

【目录结构】(2026-09 重组)
  app\    运行时：Python 模块 + 它们加载的所有 DLL(必须同目录)
           gui.py / dlss_engine.py / rtx_video.py
           dlssnr_host*.dll、vfx_host.dll、truehdr_host.dll
           nvngx_*.dll 等 NVIDIA 运行库(不在仓库里,需自备)
  src\    源码：dlssnr_host2.cpp / vfx_host.cpp / truehdr_host.cpp
  deps\   第三方 SDK 本机缓存(NVIDIA 专有，已 gitignore，不进仓库)
  tools\  run.bat / setup.bat / build.bat
  docs\   README.txt(本文) / IMPROVEMENT_REPORT.md / THIRD_PARTY.md

  ★ DirectShow 滤镜(dlssnr_dshow.cpp / 面板已内置为原生窗口)已拆分到
    【独立仓库 dlssnr-filter】，不在本仓库内。

【运行】
  双击 tools\run.bat，或命令:  cd app  &&  python gui.py
  启动参数(都可选):
    python gui.py 视频.mp4            直接打开该视频，跳过导入界面
    python gui.py --source-dir D:\v   打开该文件夹里的全部视频
    python gui.py --source 视频.mp4   同上，逐个指定
    python gui.py --no-picker         空窗口启动，稍后自己导入
  自检(不开窗口，验证 GPU 通路):  python gui.py --selftest  → 生成 _selftest.txt

【编译】
  tools\build.bat         编译全部(缺 deps\ 时自动跳过需要 SDK 的部分)
  (DirectShow 滤镜的编译脚本已随源码移到 dlssnr-filter 仓库的 tools\build.bat)

【功能】
  显示下拉:  原图 / DLSS / 分屏 / 并排      (旧名"对比"= 现在的"分屏")
    - DLSS: 实时生成当前帧的 DLSS5 神经渲染效果(零引导)，拖帧进度条或播放逐帧看。
    - 分屏: 同一帧左右分界，右侧取 DLSS 结果。【分界线只能拖线中间的圆形手柄】
            —— 点在画面其他任何地方都是"平移画面"，不会误拖动分界线。
    - 并排: 左右固定各占一半(线不可拖)。拖动改变的是【显示区域】，
            两半永远裁同一块源画面，所以左右看到的永远是同一处像素。
  多文件: 一次导入多个(文件夹/多选/拖放)后，第一个立即预览，全部进入可切换列表：
          - 底部"文件"下拉直接选，或 Ctrl+← / Ctrl+→ 前后切换
          - 「导出/队列」页的"列表全部加入队列"可把列表按当前参数一次性排队
          (改版前是"其余自动进队列"，现在改成可浏览，排队需显式点按钮)
  缩放: 滚轮按 5% / 8% / 10% / 15% / 20% / 25% / 33% / 50% / 67% / 80% / 100% /
        125% / 150% / 175% / 200% / 250% / 300% / 400% 逐档(共 18 档)。
        【滚轮不会切到"适应窗口"】—— 适应窗口是一次性动作("复位"按钮或下拉选它)，
        否则从 25% 往下滚会落回适应窗口，看起来像缩放坏了。
        从"适应窗口"起滚时，第一档取"最接近当前实际显示比例"的那一档，不会跳。
  平移: 放大到超出窗口后才能拖，而且【只在溢出的方向能拖】——
        画面宽度没超过窗口宽度就锁左右，高度没超过就锁上下，两向都超过才随意拖。
        拖到边界即止(不会把画面甩出窗口)。"复位"一键回中。
  显示路径(2026-09 优化): 每帧只把【画布可见的那一块】缩放后交给 Tk，
        所以缩放倍率不再影响绘制耗时(此前上传整张放大图，2x 是 ~80 ms、
        3x 是 ~183 ms，现在是 8-9 ms 且与倍率无关)。
        颜色转换也从"BGR→RGBA→RGB"两步改成一步(BGR→RGB)，省掉一次整帧转换。
  播放/暂停: 按视频帧率逐帧实时跑 DLSS。
  DLSS 设置(实时生效): 【启用 DLSS 处理】总开关 + 风格(默认/自然/电影/风格3)、
              强度(0-1)、本地色调(0-2)、本地结构(0-2)、皮肤结构(0-2)、自动遮罩。
              注: 【皮肤结构要勾上"自动遮罩"才有作用】(实测: 遮罩关闭时
                  皮肤结构 0→1 逐字节相同)；强度上限 1.0 也是实测结果，
                  1.0 以上是死区，所以滑块只给到 100%。
              关闭总开关时，下面的参数会置灰(不再起作用)，整条神经渲染通道被跳过：
                - 只做 RTX Video 增强(放大/HDR 等)时更快
                - 全关时就是"仅重新编码"，帧数据原样送编码器(零转换)
              改完立刻刷新。
              输出视图(处理/差异×10/左右对比)与输出混合在【导出】页，
              它们是预览调试用的，不跟引擎参数混在一起。
  【多层 DLSSNR】(参数卡里的"层"一行)
    用法: 点「＋」加一层(最多 8 层)，点层号切换，当前层的参数显示在下面
          (风格/强度/色调/结构/皮肤/遮罩都是【每层独立】)，点「－」删掉当前层。
          新加的一层默认复制当前层，方便在它的基础上微调。
          滑块/下拉改的是"当前选中的那一层"，切层时界面会跟着换成那一层的值。
    执行: 每帧对同一帧依次跑完所有层 —— 第 1 层的输出喂给第 2 层，
          依此类推，最后一层的输出才是画面。预览、单导出、队列都遵循这个顺序。
          队列任务会把整条层链一起快照下来(表格里显示成 "DLSS 3层: 风0/强.8…")，
          双击任务载回界面时层链一起恢复。
    每层独立: 【每层有自己的 NGX feature 和时域历史】(dlssnr2_chain，宿主里
          建 N 个 feature)。这一点是必须的，见下面「为什么会闪」。
          整条链记在【同一条命令列表】里，所以每帧只有一次上传、一次等栅栏、
          一次回读，而不是 N 次往返。
    代价: 推理本身仍是每层一遍，所以耗时约为单层的 N 倍
          (1080p 单层约 9.5 ms)。多层时导出不走双缓冲流水(层之间有依赖)，
          日志会写明"每帧依次执行 N 层"。
    【为什么会闪(已修)】曾经各层共用同一个 feature，结果大面积纯色区域会以
          帧率闪烁。实测(640x360，70% 面积为纯色 + 缓慢平移 + 颗粒，指标是
          "纯色区域整片平均亮度的逐帧波动")：
              1 层                        0.140
              2 层·共用 feature(旧)        0.504   ← 3.6 倍，肉眼可见
              2 层·各自 feature(现在)      0.233   ← 1.7 倍
          原因: 同一个 feature 每帧跑两次时，第二次拿到的历史【恰好等于它自己的
          输入】。本宿主喂的运动矢量恒为零，网络据此判断"整帧完全静止、历史全部
          可信"，于是环路增益翻倍；非线性网络环路增益翻倍就是帧率级振荡。
          各层独立 feature 后，每层都拿到正常的(输入, 上一帧输出)配对，放大倍数
          从 3.6 倍降到 1.7 倍 —— 剩下的 1.7 倍是级联两个时域滤波器本身的残余
          按平方和叠加(≈√2)，属于这种串联方式的固有性质。
          另外验证过一条弯路: 让第 2 层"纯空域"(每帧 reset)【不会】更好
          (实测 0.500，和共用历史一样差) —— 时域状态在第 2 层是阻尼器，
          去掉它等于让第 2 层的空域增益直接放大第 1 层的帧间起伏。
          想进一步降低只能少用层数或调低各层强度。
  预览处理上限: 不限制 / 720p / 1080p(默认) / 1440p        (也在【导出】页)
              超过上限时先缩到上限再做神经处理，等于或低于上限则完全不动。
              只影响预览，导出始终用原始分辨率(已实测: 4K 源无论上限怎么设，
              导出结果都是 3840x2160)。4K 素材预览 18.9 → 35.5 fps。详见【性能】。
  RTX Video 增强(独立开关，可叠加):
    AI 放大:      关闭 / 放大 低·中·高·超高          ← 唯一会改变分辨率的选项
    目标:         ×1.5 / ×2 / ×3 / ×4 / 720p / 1080p / 1440p / 2160p
    降噪/去模糊/高码率: 关闭 / 降噪(4档) / 去模糊(4档) / 高码率(4档)
    SDR → HDR10:  勾选后输出真正的 10bit HDR10 文件(PQ / BT.2020)
                  中灰 10-100、峰值亮度 400-2000 nits
                  对比% / 饱和% 都是 -100 ~ +100(0 = 不改变，与 NVIDIA 界面一致)
                  注: SDK 内部用 0-200、100 为中性，界面按 NVIDIA 习惯显示成相对百分比:
                      -100% → 0   0% → 100(不改变)   +100% → 200
                  实测有效性(TrueHDR 真实输出):
                      对比 -100%/+100% → 反差(std) 0.1008 / 0.1237
                      饱和 -100%/+100% → 彩度      0.1277 / 0.2470
                  中灰和峰值亮度是绝对值(nits)，不是百分比，故保持原样。
  编码器下拉:  H.265 / H.264 / AV1 走 NVENC(硬件，最快)；x264 / x265 为软编码备选。
  导出 DLSS 视频: 流水线处理整段视频，音轨原样复制，输出 mp4。
    输出文件名会写明实际做了什么，例如:
      video_DLSS.mp4                      ← 只跑神经渲染
      video_reencode.mp4                  ← 全部关闭（仅换编码器/封装）
      video_VSR-Med_x2_2560x1440.mp4      ← AI 放大到 1440p
      video_DN-High.mp4                   ← 只降噪
      video_DB-High_HDR10.mp4             ← 去模糊 + SDR→HDR10
      video_DLSS_VSR-Ultra_x4_HB-Med_HDR10_7680x4320.mp4   ← 全套
    尾缀含义: DLSS=神经渲染 / VSR=放大(带目标尺寸) / DN=降噪 / DB=去模糊 /
             HB=高码率 / HDR10=HDR 输出 / 最后是实际输出分辨率(仅当与源不同)
    目标文件已存在时不会直接覆盖，会弹窗二次确认:
      是 = 覆盖  否 = 自动改名(加 -1、-2 …)  取消 = 不导出
  进度显示(进度条下方两行):
      第一行: 状态 + 当前帧/总帧数
      第二行: 速度(XX.X fps / XX ms每帧)  已用  预计剩余  预计总耗时  总帧数
    速度取最近 60 帧的滑动窗口(反映当前状况)，预计剩余用整体平均(稳定不跳动)。
    导出结束后保留最终耗时，导入新视频时清空。

【RTX Video 增强】(实测数据，RTX 5070 Ti Laptop)
  四个能力都来自 NVIDIA RTX Video SDK，在同一个效果(VideoSuperRes)里按
  QualityLevel 选择，所以它们是"一个效果的不同模式"，但可以叠加使用：

    模式            QualityLevel    作用
    放大 低/中/高/超高   1 / 2 / 3 / 4    会改变分辨率(唯一能放大的)
    降噪 低/中/高/超高   8 / 9 /10 /11    同分辨率
    去模糊 低/中/高/超高 12/13/14 /15     同分辨率
    高码率 低/中/高/超高 16/17/18 /19     同分辨率

  单帧耗时(640x360，含 H2D/D2H 传输)：
    放大 中 ×2 → 1280x720      1.46 ms
    放大 超高 ×2 → 1280x720    2.15 ms
    降噪 中                    0.99 ms
    去模糊 高                  1.35 ms
    高码率 中                  0.97 ms
    SDR→HDR10 (R10)            0.91 ms
    放大 + 去模糊              6.20 ms
    DLSS + 去模糊 + HDR10      5.86 ms

  SDR → HDR10 输出:
    输出 10bit PQ / BT.2020，验证方式: 编码后回读，
    最深信号 = 0.751(归一化)，正好是 ST2084 中 1000 nits 的码值，
    说明「峰值亮度」参数精确生效；可用色阶 551 级(远超 8bit 的 256)。
    注意: 这是把 SDR 素材"上变换"成 HDR，不是解码已有的 HDR 视频。
    RTX Video HDR 只接受 8bit SDR 输入(连 P010 也被当作 SDR)，
    所以「处理既有 HDR10 片源」仍然做不到。

【处理链路组合】(DLSS 开关 × RTX Video，实测导出全部通过)
  导出时会打印实际链路，例如:
      DLSS                  → 只跑神经渲染(GPU 直连 + 双缓冲，最快)
      无处理（仅重新编码）    → 全部关闭，帧原样送编码器
      VSR_Medium            → 只放大
      TrueHDR               → 只做 SDR→HDR10
      DLSS + TrueHDR        → 神经渲染后转 HDR
      VSR_Medium + DLSS + Deblur_High → 三合一(放大 → 神经渲染 → 去模糊)
  关闭 DLSS 后处理链末端的输出是原始 BGR 字节，逐字节等于解码帧(已实测验证)，
  没有任何隐藏的色彩或精度转换。

【性能】(实测，RTX 5070 Ti Laptop)
  预览(画布约 850x265；数字为稳态，已排除一次性初始化开销):
      1080p 源   逐帧约 19 ms(对比/DLSS 视图)，播放 24.0 fps(与视频本身一致)
      4K 源      取决于「预览处理上限」:
                    上限           DLSS 处理尺寸     单帧      可达fps
                    不限制         3840x2160      53.0 ms     18.9
                    1440p         2560x1440      34.8 ms     28.8
                    1080p (默认)   1920x1080      28.2 ms     35.5   ← 1.88 倍
                    720p          1280x720       23.0 ms     43.5
                  1080p 及以下的源不受上限影响(实测 19.6 ms vs 19.0 ms，等价)
      修复前      15-22 fps(逐帧 seek，原因见「已修复的坑」第 8、9 条)
      显示路径    改造后(2026-09)：整帧绘制 9-12 ms 且【与缩放倍率无关】。
                  改造前：适应窗口 9-14 ms，但 2x 是 76-81 ms、3x 是 183 ms
                  —— 原因是把整张放大图(2x=3840x2160)交给 Tk 上传，
                  而其中大部分在画布外。现在只上传可见区域。
                  屏幕上的耗时构成(1080p 源)：可见区 resize ~1.1 ms +
                  一次 BGR→RGB 1.6 ms + PPM 打包 ~0.6 ms + Tk 图片上传 ~4.4 ms。
                  剩余最大项是 Tk 自己的图片上传，属 C 层，不是 Python 解释器开销。
      RTX 增强开启时每帧增加 7-10 ms；首次启用需一次性加载模型约 13 秒，
      之后切换模式是即时的(在同一会话上换 QualityLevel，实测 0.00 秒)。
  单帧处理(GPU 直通 + 双缓冲):
      720p      5.17 ms/帧  → 约 193 fps
      1080p     9.53 ms/帧  → 约 105 fps
      1440x1440 9.58 ms/帧  → 约 104 fps
  整段导出(1080p / 1440x1440 @60fps + HEVC NVENC + 音轨): 约 91-94 fps
  GPU 利用率: 98%(单缓冲时只有 87-88%)
  历代对比(1080p 单帧):
      旧宿主 + numpy 往返   约 21.8 ms (46 fps)
      新宿主(GPU 直通)     约 11.4 ms (88 fps)
      新宿主 + 双缓冲       约  9.5 ms (105 fps)

【达到极限了吗：瓶颈分析】
  1080p 每帧 9.5 ms 的构成:
      convIn  (BGR→RGBA SIMD)  0.8 ms
      eval    (设参数+录制)     0.9 ms
      gpu     (提交+同步+推理)  约 7.0 ms
      convOut (RGBA 直出)       0.9 ms
  分辨率扩展拟合: T(ms) ≈ 2.5 + 4.4 × 百万像素
      → 固定开销 2.5 ms(同步延迟 + 小 kernel 启动)
      → 边际 4.4 ms/百万像素(真实推理计算，约 5 ns/像素)
  实测 nvidia-smi: GPU 利用率 98%、功耗 130W / 上限 140W(默认 65W)、
  SM 时钟 2512MHz(上限 3090MHz)、温度 70°C 不热。
  结论: 剩余瓶颈是「GPU 功耗墙 + 模型本身的计算量」。功耗上报
  SW Power Cap: Active，即 GPU 因功耗被降频，不是散热问题。
  想再快可解锁功耗: NVIDIA 控制面板 → 管理 3D 设置 → 电源管理模式 →
  "最高性能优先"，并把 Windows / 厂商控制中心的电源计划切到性能模式。

【文件夹内容】
  gui.py              界面 + 导出流水线(tkinter)
  dlss_engine.py      ctypes 封装。Engine2 = 新的 GPU 直通宿主；Live = 旧宿主(保留兼容)
  rtx_video.py        RTX Video 封装: Pipeline(链路) + vfx/truehdr 会话管理
  dlssnr_host2.cpp    新宿主源码(BGR进/RGBA出，GPU 直通 + 双缓冲 + 多层链)
  dlssnr_host2.dll    新宿主编译产物 ← 实际使用这个
  dlssnr_host.dll     旧宿主(CPU 往返，仅供 dlss_engine.Live 兼容使用)
  nvngx_dlssnr.dll    NVIDIA DLSS5 神经渲染运行时(310.8.0.0)
  vfx_host.cpp        RTX Video 宿主源码(VSR/降噪/去模糊/高码率，会话制)
  vfx_host.dll        RTX Video 宿主编译产物
  truehdr_host.cpp    TrueHDR 宿主源码(SDR→HDR10，自带独立 NGX 加载器)
  truehdr_host.dll    TrueHDR 宿主编译产物
  NVVideoEffects.dll  NVIDIA Video Effects SDK 加载器(1.2.0.0)
  NVCVImage.dll       NvCVImage C 接口
  nvVFXVideoSuperRes.dll  效果插件
  nvngx_vsr.dll       VSR 运行时(1.8.2.0)
  nvngx_truehdr.dll   TrueHDR 运行时(1.1.0.0，含 sm_120)
  nvngxruntime.dll    NGX runtime
  sdk_include/        NVIDIA NGX SDK 头文件(官方 NVIDIA/DLSS 仓库)
  sdk_lib/            nvsdk_ngx_s.lib(编译 dlssnr_host2.cpp 用)
  rtx_video_sdk/      官方 RTX Video SDK 1.1.0(编译 truehdr_host 用 + 文档/样例)
  rtx_sdk_legal/      NVIDIA RTX Video SDK 许可与编程指南 PDF
  run.bat / setup.bat 启动与环境脚本
  README.txt          本说明

【为什么快：新旧数据路径对比】
  旧路径(每帧，720p 实测约 21.8 ms):
      cv2 读帧(BGR) → cvtColor(BGR→RGB) 0.6ms → dstack(alpha) 3.6ms
      → 旧宿主(内部 CPU↔GPU 往返 + 每帧重建 numpy 缓冲) 16.1ms
      → cvtColor(RGBA→BGR) 4.0ms → 写文件
  新路径(每帧，720p 实测约 5.2 ms):
      cv2 读帧(BGR) → 宿主内部 SIMD 转 RGBA 0.36ms
      → GPU 计算 + 单次提交 4.5ms → 直接输出 RGBA 0.6ms → 写管道
  关键改动:
    1. 宿主直接吃 BGR、直接吐 RGBA，Python 侧不再有 dstack / cvtColor 往返。
    2. 宿主的 D3D12 缓冲、命令分配器、命令列表全程复用，不再每帧重建。
    3. 每帧只提交一次命令(旧版分三段各等一次围栏)。
    4. 通道转换用 SSSE3 SIMD，比 numpy 的 dstack 快一个量级。
    5. 【双缓冲】宿主持有 2 套资源槽，用 submit / fetch 两阶段：
       提交第 N 帧后不等围栏，直接去取第 N-1 帧的结果。
       CPU 的搬运+录制工作(约 2.6ms)藏进 GPU 跑上一帧的时间里，
       GPU 利用率 87-88% → 98%，导出 62 → 91-94 fps。
       预览仍用同步接口 process_rgba(一进一出)，两种路径输出逐像素一致。
       注: 已实测【三缓冲无收益】。2 槽与 3 槽在 720p/1080p/1440x1440/2560x1440
       吞吐完全相同(差异<0.5%)，即使把 CPU 阶段人为加重到 GPU 的 1.7 倍也一样。
       原因: 吞吐 = max(CPU每帧, GPU每帧)，加深队列只能缓冲吞吐、不能创造吞吐。
       故保持 2 槽(可用 /DDLSSNR_SLOTS=3 重编译实验)。
    6. 导出走 ffmpeg 管道(-pix_fmt rgba)，帧数据零转换直接喂编码器；
       处理线程与写入线程并行，GPU 计算与硬件编码重叠。
    7. 进度上报走线程安全队列、主线程轮询 —— 绝不在工作线程里调 Tk
       (跨线程调 root.after 会让 Tcl 阻塞，实测每帧多花 1.3 秒)。

【改进全历程】
  详见同目录 IMPROVEMENT_REPORT.md —— 记录了从最初 Python 版到现在的
  6 个阶段(每阶段附工作流图)、每一步的实测数据、被证伪的两个分支
  (深度引导、三缓冲)、以及瓶颈分析方法。

【对方电脑要求】(要"直接用")
  1. Windows 10/11，NVIDIA 显卡(建议 RTX 30/40/50 系)，装最新驱动。
  2. Python 3(勾选 Add to PATH)，然后:  pip install numpy opencv-python pillow
     (tkinter 随 Python 自带)
  3. VC++ 2015-2022 运行库(vcruntime140.dll)——缺则装 "Microsoft Visual C++ Redistributable"。
  4. 需要 ffmpeg(用于导出/音轨)。装在 PATH，或放到以下任一位置:
       C:\ffmpeg\bin\ffmpeg.exe
       D:\Programe\ffmpeg-7.1.1-full_build\bin\ffmpeg.exe
       或 pip install imageio-ffmpeg
  5. 所有文件放同一文件夹(别移动 DLL)。

【已知限制】
  - Feature 18 神经渲染在本配置下忽略光流/深度引导(零引导)，故不需要深度/光流模型和 torch；
    【预设/引导模式】对画面无影响，因此只保留真正有效的参数。
    已实测验证：即使把真实深度图送入 DLSSNR.Depth，输出与全零深度完全一致(差 0.000)，
    说明该模型在当前 ProjectID 下不消费深度输入 —— 这是 NVIDIA 侧的限制，不是接线问题。
    但运动矢量(MVec)是【生效】的：喂入非零 MVec 会让输出改变(mean 2.46 / max 24)，
    且正确方向是「真实位移取负、单位是像素」(已用合成序列定标，
    分辨率翻倍后峰值位置不变，证明是像素单位而非 UV)。
    当前实现仍喂全零 MVec(等于告诉网络"画面静止")，对运动画面不理想，
    但实测收益仅 +0.39 dB，且基线本身在加细节而非降噪，故暂不启用。
  - RTX Video 的放大与同分辨率模式来自同一个效果，靠 QualityLevel 选择；
    可以叠加(如放大+去模糊)，此时会占用两个会话，每帧跑两次 GPU。
  - 「高码率」模式针对的是高压缩比素材的块状/蚊噪伪影，对干净片源可能过度平滑。
  - SDR→HDR10 是上变换，不能用来处理既有的 HDR10 片源(见上)。
  - 编码器必须是 NVENC 才能跑满；若显卡不支持所选编码器，请在编码器下拉里换一个。
    全部 5 个编码器均已实测通过(含 tag 正确性):
      hevc_nvenc→hvc1  h264_nvenc→avc1  av1_nvenc→av01  libx264→avc1  libx265→hvc1
  - HDR 导出建议用 hevc_nvenc(快)或 libx265(元数据完整)。libx264 的 HDR 支持不佳。
  - 换视频(含不同分辨率)已支持：同尺寸复用会话，异尺寸只重建 feature + 纹理，
    不重新初始化 NGX 核心(重复初始化会崩)。
  - TrueHDR 需要 RTX 显卡 + 较新驱动；不可用时界面会明确提示，勾选也不会崩。

【已修复的坑】(踩过的,别再踩)
  1. 【x265 导出挂起】mp4 容器标签必须与编码格式匹配。早期版本用编码器名推导标签
     (`startswith("hevc")`)，而 "libx265" 不含 "hevc" → 给 HEVC 流打了 "avc1" 标签
     → ffmpeg 中途报错退出 → 管道写端阻塞 → 导出永久卡住(实测 30 秒无输出)。
     现在标签在 ENCODER_CHOICES 里显式声明，并且导出循环会检测 ffmpeg 提前退出。
  2. 【stderr 管道死锁】ffmpeg 的 stderr 必须持续读取。libx265 即使在 -loglevel error
     下也会大量打日志，管道缓冲区(约 64KB)写满后编码器阻塞 → 同样的死锁。
     现在有专门的线程持续排空 stderr(并对 x265 加 log-level=error 从源头减少输出)。
  3. 【Tk 跨线程】绝不在工作线程里调用 root.after()/控件方法 —— Tcl 会阻塞，
     实测每帧多花 1.3 秒(30 帧导出从 0.5s 变 8s)。进度上报一律走队列 + 主线程轮询。
  4. 【NvVFX_DestroyEffect 永久挂起】RTX Video 的效果句柄一旦 Load 成功就不能销毁 ——
     调用它无限阻塞(实测 >120 秒不返回)。所以 vfx_host 故意泄漏该句柄，
     并且把会话按「尺寸」缓存复用；销毁只释放自己的缓冲。
     这也意味着【每个新尺寸第一次启用要等约 13 秒】加载模型，界面上有提示。
  5. 【NVSDK_NGX_D3D11_Shutdown1 挂起】TrueHDR 初始化成功后再关闭 NGX 核心会挂住。
     所以 NGX 每进程只初始化一次、永不关闭，只释放 feature/纹理；进程退出时由系统回收。
  6. 【nvenc 不写色彩元数据】HDR 导出时 -color_primaries / -color_trc 输出选项
     对 hevc_nvenc 无效(实测只写出 matrix，primaries/transfer 仍是 unknown)。
     改用 setparams 滤镜给帧打标记，编码器再写进 VUI，这样对所有编码器都有效。
     HDR10 的静态元数据(mastering display / MaxCLL)只有 libx265 能写，nvenc 不支持。
  7. 【TrueHDR 输出格式】FP16 是线性 scRGB(值可超 1.0)，直接当 PQ 标注会让
     往返误差高达 47/255；R10G10B10A2 才是已编码好的 HDR10 形态(往返误差 5.95/255)。
     所以 HDR 导出固定用 R10，不做任何色彩转换。
  7b.【HDR 红蓝互换】R10 交给 ffmpeg 时必须用 x2bgr10le，不是 x2rgb10le。
     DXGI 的 R10G10B10A2 把 R 放在【低位】、B 放在【高位】，与 ffmpeg 的
     X2R10G10B10 正好相反。用纯色探针实测:
        按 DXGI R10G10B10A2 打包纯红(1023,0,0)
          -> x2rgb10le 解出 RGB(0,0,255)   ← 红蓝互换(错误)
          -> x2bgr10le 解出 RGB(255,0,0)   ← 正确
     所以带 "bgr" 名字的那个才是匹配的。端到端验证: 红/绿/蓝/白四色块
     解码后各自落在正确通道，亮度保持不变。
  8. 【预览卡顿的元凶：逐帧 seek】预览曾经只有 ~15 fps，而导出一直是满速。
     原因是两条路径读帧的方式不同：
       导出: cap.read() 顺序读        实测 1.3 ms/帧
       预览: cap.set(POS_FRAMES)+read  实测 23.4 ms/帧  ← 慢 17.8 倍
     H.264 的任意跳帧必须先定位前一个关键帧再逐帧解码到目标帧，而关键帧间隔
     常见 1-10 秒，所以每次"跳一帧"实际可能解码几十帧。
     现在只在真正跳转时才 seek(播放是连续帧，直接接着读)，并加了一帧缓存
     ——因为对比视图会对同一帧请求两次(原图一次、DLSS 一次)，
     第二次会被当成跳转而白付一次 seek。
     结果: 单帧 34.5 → 11.0 ms，播放 22 → 30 fps。
  9. 【播放节奏没补偿处理耗时】root.after(interval) 的计时是在当前帧【处理完之后】
     才开始的，所以实际周期 = 处理耗时 + interval。固定 33 ms 的延迟在处理要花
     12 ms 时只能跑出 22 fps，而不是 30 fps。
     现在按实测周期自校正(把 Tk 渲染等循环内的开销也一起扣掉)，
     30 fps 素材实测稳定在 30.0 fps。
  10. 【测量陷阱：把一次性初始化平均进去】改「预览处理上限」会让 DLSS 按新尺寸
     重建会话(ReleaseFeatureAndTextures + CreateFeature)，这是一次性的。
     早期测 4K 预览时只跑 3 帧就取平均，把这次重建算进去，得到 576 ms / 90 ms
     两个虚高值，而真实稳态是 53.0 ms / 28.2 ms —— 差了 3-6 倍。
     现在所有预览性能数字都先热身再计时。改分辨率、改上限、换视频之后的第一帧
     都会慢一次，属于正常现象，不是性能退化。
  11. 【窗口高度写死导致统计行消失】窗口原先固定 860x720，但加了 RTX Video 面板
     和"预览处理上限"一行之后内容需要 846 px，于是最后打包的统计行(速度/已用/
     预计剩余)被压成 5 px 高、等于看不见了 —— 导出其实一直在正常上报数据。
     现在窗口高度在启动时按实际内容测量，并夹到屏幕可用高度。屏幕装不下时按
     重要性依次让出空间: 日志行数 → 视频区高度 → 整个日志面板，
     因为进度和统计是最不该丢的。minsize 也据此计算，用户拖动窗口不会再把它们挤没。
     实测: 屏幕高 1080/900/800/768/720 都能完整显示统计行。
  12. 【HDR 对比度/饱和度的单位】官方 SDK 头文件写的是 0-200(100 为中性)，
     nvVFXVideoSuperRes 也只接受这个范围；但 NVIDIA 给用户看的界面用的是
     相对百分比 -100% ~ +100%。两者是同一个范围，因为 0-200 正好以 100 对称。
     界面现在按 NVIDIA 习惯显示百分比，内部换算 API = 100 + 百分比，
     换算只发生在 GUI 一层(gui.py 的 hdr_adj_to_api)，rtx_video.py 和
     C++ 宿主仍全部使用 API 单位，宿主自己的 0-200 夹取作为第二道防线保留。
     中灰(10-100)和峰值亮度(400-2000 nits)是绝对值，不是百分比，故不变。
  13. 【GPU 占用率的误读】看宿主日志里的 PERF 行时要注意: CUDA 是异步提交的，
     `run=0.25` 是【提交】耗时不是计算耗时，真正等待发生在后面的 sync 里，
     所以那一行看起来像 "d2h+sync=7.96" 占了大头。"GPU 只忙 2%" 是这么误读出来的。
     用隔离测量(30 次、每相末同步一次)才得到真实数字:
         H2D 0.59 ms | GPU 计算 7.4 ms | D2H 0.69 ms  = 8.7 ms/帧
     => GPU 计算占 84%，与 nvidia-smi 的 76-78% 吻合。传输只占 14%，
        且实测 13.1 GB/s (H2D) / 11.2 GB/s (D2H)，是正常 PCIe 4.0 速度。
     结论: 该效果【已经是计算受限】，GPU 没有被闲置。
     也测过双缓冲是否有用: 同一尺寸建两个会话、各跑一个线程，合计只快 1.08 倍
     (各自从 113.9 掉到 ~61 fps)，说明被驱动/硬件引擎串行化了，加缓冲拿不到收益
     —— 这也是为什么 vfx_host 保持同步设计。
     想更快只能降低分辨率(用「预览处理上限」)或换更轻的档位，没有别的余地。
  14. 【零 MVec 到底安不安全(DirectShow 滤镜可行性)】我们离线处理没有运动矢量，
     一直喂零(dlssnr_host2.cpp 的 `DLSSNR.MVec` = g.texZeroMV)。
     DLSSNR 确实【带时域历史】: 两段输入只在前 10 帧不同，t>=15 起喂完全相同的帧，
     输出仍有 mean|diff| 0.40 / max 10，15/15 帧全部不同 => 不是纯空域滤镜。
     但这【不】等于会拖影。刚性平移的真实运动场就是常量场，于是用
     dlssnr2_mvec_const 注入 oracle 运动矢量做无估计误差的对照:
       位移 4 px/帧 与 12 px/帧 各测一次，滞后曲线 k=0..6 均为 best k=0
       (输出最优匹配就是当前帧，没有向过去偏移)
       物体后方拖尾(干净背景 55.0): 零 MVec 与 oracle 都在 +0.6 以内
     对照有效性已验证: 同输入下零 MVec vs oracle 输出差 mean 0.67 / max 18，
     即 MVec 确实被消费，上面比较不是空的。
     结论: 零 MVec 不造成运动拖影，滤镜路线在这点上安全。
     代价在硬切镜头: 陈旧历史第 1 帧 mean|diff| 2.02 (max 17)，第 2 帧降到 0.49，
     第 3 帧起 0.39~0.44 见稳态 0.40 —— 【一帧内就洗净】。
     所以滤镜只需在 seek/切镜头时置一次 Reset(即 process 的 reset=1)，
     gui.py 已用同样做法(帧号不连续就 reset)。

【DirectShow 滤镜(PotPlayer 等播放器)】
  产物: dlssnr_dshow.dll (自包含、无第三方依赖)
  ★ 滤镜源码已拆分到独立仓库 dlssnr-filter，不在本仓库内。
  注册: 在 dlssnr-filter 仓库里管理员运行 tools\register_filter.bat
        (或 regsvr32 app\dlssnr_dshow.dll)
  配置: dlssnr_dshow.ini (与 dll 同目录; 删掉即用默认值)
  日志: dlssnr_dshow.log (同目录, 超过 4 MB 自动轮转为 .log.old)

  为什么是「自包含裸滤镜」: Windows SDK 只发 strmbase.lib 而【不发 streams.h】
  (vcvars 所在 SDK 实测没有 streams.h，也没有 ATL)，所以 CTransformFilter
  及其基类都用不了。本滤镜直接自己实现 IPin / IMemInputPin / IBaseFilter，
  只复用系统自带的 CLSID_MemoryAllocator 分配样本。

  契约上的巧合: dlssnr2_process 收的是系统内存 BGR24，而 DirectShow 的
  MEDIASUBTYPE_RGB24 在内存里就是 B,G,R(DIB 老规矩; 名字和 x2rgb10le 那次
  一样在骗人)。两边逐字节一致，【不需要任何通道转换】。

  实测每帧开销(引擎已 READY，非直通):
      1280x720    5.79 ms (172.7 fps)   Python 路径 5.17 ms  -> +0.62 ms
      1920x1080  10.82 ms ( 92.4 fps)   Python 路径 9.53 ms  -> +1.29 ms
  多出的 0.6~1.3 ms 是 stride 收拢/铺开 + DS 样本流转的固定成本。
  => 1080p60 可行(10.8 < 16.7)，1440p60 和 4K 都不行。

  写这个滤镜时踩到并已修的坑:
   a) 【DLL 零导出】只写 STDAPI 不会导出。首次编译出的 dll 一个导出都没有，
      regsvr32 必然失败。补 dlssnr_dshow.def 显式导出 DllGetClassObject /
      DllCanUnloadNow / DllRegisterServer / DllUnregisterServer 才成立。
      验证: dumpbin /exports dlssnr_dshow.dll
   b) 【输出引脚没有媒体类型】变换滤镜的输出类型就是它的输入类型，但本滤镜
      的输出引脚起初不会自己获得类型，导致下游 Connect 一律返回
      0x80040209 VFW_E_NOT_CONNECTED。修法: 输入引脚连上后把协商好的类型
      镜像到输出引脚(CPinBase::AdoptType)，断开时清掉。
   c) 【NULL pmt 被当成错误】DS 会用 pmt=NULL 调 Connect 表示「你挑」。
      原先直接拒绝，导致下游永远连不上。
   d) 【初始化失败变成死循环】init 失败后 needInit 仍为真，工作线程每秒重试
      上千次: 一个核心跑满 + 日志被刷爆(实测刷到几万行)。另外 NGX 每进程
      只能 init 一个尺寸，重试根本不可能成功。修法: 同一尺寸最多试 3 次，
      之后放弃并写一条明确日志(提示「换分辨率要重启播放器」)。
   e) 【stride 并非总是紧密排列】RGB24 行按 4 字节对齐: 宽 642 时
      packed=1926 而 stride=1928。引擎只认紧密 BGR24，所以要走
      收拢->处理->铺开。校验方式: 送纯灰 128 的平场，引擎必须原样返回
      (实测最大偏差 1)，同时 padding 字节必须原样保留(实测 0 字节被改)。
      注意【不要】用渐变行去校验 stride: 引擎的局部色调映射会把渐变
      clip 得很厉害(实测 (0,255) -> (81,232))，那是效果不是错位。
   f) 【不要用同一进程测多个分辨率】NGX 同进程只能一个尺寸，换尺寸会
      Init_Ext fail 0xBAD00002。测性能必须【每个分辨率一个独立进程】，
      否则测到的是直通速度(会看到 0.1 ms 这种假数字)。

  【PotPlayer「加载了但没有帧输入」的根因与修复】
  现象: 强制开启滤镜后，面板显示"已加载,但当前没有帧"，framesSeen=0、
        heartbeat=0；PotPlayer 完全正常播放，没有任何报错。
  定位: 滤镜日志停在
          filter: created
          filter: created          <- 之后再无任何一行
        关键缺失是【Pause 从未被调用】—— 说明滤镜被创建了，但从未被插入
        到图里。对照 PotPlayer 自己的 OSD:
          视频解码器: 内置 FFmpeg 解码器
          输出: NV12(12 位) 1920×1440       <- 解码器输出 NV12
        而本滤镜当时只接受 RGB24/RGB32 -> Connect 被拒 -> 播放器放弃插入。
  两个真正的 bug:
   a) 【EnumMediaTypes 在未连接时直接失败】原实现在没有协商好类型时返回
      VFW_E_NOT_CONNECTED。但播放器/图构建器【正是在没连接的时候】来问
      "你接受什么格式"。这一问失败，播放器就不再尝试插入滤镜。
      修法: 未连接时用默认几何(1920x1080)照样枚举出可接受的类型。
      这条也和之前那个崩溃同源: 两个 bug 都在 EnumMediaTypes 相关路径上，
      而播放器一定会走这条路径。
   b) 【只支持 RGB24/RGB32】绝大多数播放器(尤其硬解)默认输出 NV12。
      修法: 接受 NV12(以及 YV12/IYUV/I420)，并在滤镜内部做
      NV12 -> BGR24 -> 引擎 -> BGR24 -> NV12 的往返转换。
      转换用 BT.709 有限范围系数(见下)，实测 14 万个颜色往返误差
      平均 1.23 / 最大 3(8bit)。色度按 2x2 平均回写，是上面"复制"的
      正确逆运算(只取一个像素会让色度偏向 DLSSNR 恰好移动的那一点)。
      注册表里的媒体类型也加了 NV12(放在第一位，因为那才是解码器默认输出的)。

  为什么用 BT.709: PotPlayer OSD 报告的色域是 bt709，且这是 HD 内容的常规。
  系数(有限范围, Y 偏置 16, 色度偏置 128, 定点 8 位):
      NV12 -> RGB:  R=(298*C+459*E+128)>>8  G=(298*C-55*D-136*E+128)>>8
                    B=(298*C+541*D+128)>>8     其中 C=Y-16, D=U-128, E=V-128
      RGB -> NV12:  Y=((47R+157G+16B+128)>>8)+16
                    U=((-26R-87G+112B+128)>>8)+128
                    V=((112R-102G-10B+128)>>8)+128
  实测四色块(经引擎处理后回读)与目标色的偏差:
      红 (230,40,40) -> (210,51,51) | 绿 (40,220,60) -> (24,221,76)
      蓝 (50,70,230) -> (58,76,222) | 近白 (210,210,210) -> (213,213,209)
  并且专门断言了【红蓝没有互换】(偏红块 R>B、偏蓝块 B>R)。
  偏差主要来自引擎自身的色调映射，不是矩阵错误。

  性能影响: NV12 路径每帧多两次转换(约 1920x1440 下 3 MB/帧的读写)。
  1080p 的 RGB24 直通路径不受影响(仍是零拷贝直接指向样本缓冲)。

  注册相关(踩到的第三个坑):
   【DllRegisterServer 里 COM 写失败会连累滤镜条目】注册分两部分: HKCR\CLSID
   下的 COM 服务器键(需要管理员)和 IFilterMapper2 写的滤镜条目。原来前者
   失败就直接 return，于是后者根本没写，滤镜库里还是【旧】的媒体类型。
   现象: 明明重新注册了，FilterData 仍是 176 字节且不含 NV12。
   修法: 两者独立注册，互不阻断；最后如实返回 COM 那部分的错误码，让
   regsvr32 能把真实原因暴露出来。
   验证方法(不需要反编译):
     FilterData 是 REGFILTER2 结构，前 12 字节 = dwVersion/dwMerit/cPins2，
     后面是 GUID 对；可以直接在字节流里搜 NV12 的 GUID
     (3231564E-0000-0010-8000-00AA00389B71) 来确认媒体类型是否更新。

  回归测试(临时探针，验证后已删除):
    - _regress2: 未连接时枚举(RGB24/RGB32/NV12 三种都要有) + 三种格式各自
      走完 connect/receive。PASS。
      注意【每种格式要用新的滤镜实例】: 引脚会记住连接，复用同一个滤镜会让
      第 2/3 种格式返回 VFW_E_ALREADY_CONNECTED —— 那是测试写法问题，
      不是滤镜缺陷(我一开始就踩了这个，误判成 RGB 路径坏了)。
    - _nv12_test: NV12 未连接枚举、连接、帧流、未就绪时【逐字节直通】
      (实测 |output-input| = 0)、就绪后确实被处理。PASS。
    - _color_test: NV12 四色块颜色保真 + 红蓝未互换。PASS。

  注意: 滤镜以 MERIT_DO_NOT_USE 注册，不会被 Intelligent Connect 自动插入
  (否则它会劫持机器上所有 DS 图)。必须由用户/播放器显式添加。

  【第二个真凶: 伪造的默认分辨率(我自己引入的 bug)】
  修完 NV12 之后，日志终于显示输入连接【成功】了:
      ReceiveConnection(Input) <- NV12 1920x1080 ...  -> ACCEPTED
  但注意分辨率: 视频实际是 1920x1440，日志里却是 1920x1080。
  这正是我为"未连接时也要能枚举出类型"而写死的假默认值
  (`if (w<=0||h<=0) { w=1920; h=1080; }`)。图构建器信以为真，用一个
  【不存在的几何】去连接，于是:
    - 输入连上了(类型本身合法)
    - 但输出引脚的 Connect 一次都没被调用 -> 图不完整
    - 因此 Pause 从未发生 -> 一帧都没有
  教训: 枚举媒体类型时【绝不能编造几何】。正确做法是——未连接时枚举
  仍然返回成功(S_OK)，但列表为空(m_count=0)，等上游定下真实尺寸后
  自然会再问一次。空列表是诚实的答案，假数据会把图带偏。
  修法: MakeVideoType() 在 w<=0 或 h<=0 时直接返回空类型；
  两个引脚的 EnumMediaTypes 都不再填默认值；
  分配器那处改成明确失败(VFW_E_INVALIDMEDIATYPE)而不是猜尺寸。

  【顺带补上的 VideoInfo2 支持】
  解码器/渲染器常用 FORMAT_VideoInfo2 协商(它带隔行与像素宽高比信息)，
  原来只认 FORMAT_VideoInfo，等于直接拒绝。现在两者都接受，并且枚举时
  【每种格式都提供两种形式】(共 6 项: NV12/RGB24/RGB32 x VINFO/VINFO2)。

  验证(临时探针，验证后已删除):
    - _graph_test: 精确复现 PotPlayer 的失败场景。断言未连接时
      【枚举成功但一个类型都不发布】(不再出现假的 1920x1080)，
      真实 1920x1440 下输入连上后输出必须报出同样的几何，
      最后整条链连通并把帧送进渲染器。PASS (0 failures)。
    - _fmt_test: 6 种组合(NV12/RGB24/RGB32 x VideoInfo/VideoInfo2)
      在 1920x1440 下全部连通并出帧。PASS (0 failures)。

  【诊断方法(靠它才找到根因)】
  滤镜会把以下事件写进 dlssnr_dshow.log，遇到"装了但没效果"直接看日志:
      JoinFilterGraph(...)               -> 是否真被加进了图
      Connect(<pin>) pmt=<类型>          -> 谁尝试连接、报的什么格式
      ReceiveConnection(<pin>) <- <类型> -> 协商过程与接受/拒绝
      QueryAccept(<pin>) <类型> -> O K/S_FALSE
      filter: Pause (..., in-conn=? out-conn=?)
  判断要点:
    - 只有 "filter: created"、没有 JoinFilterGraph -> 根本没进图
    - 有 ReceiveConnection 但没有 Pause -> 图没建完(本次就是这个)
    - 有 Pause 但 framesSeen=0 -> 图在跑但上游没送帧
  并且【分辨率一定要核对】: 日志里的几何必须等于视频真实分辨率，
  对不上就说明某处报错了尺寸。

  【第三个真凶: 输出引脚的分配器协商(最后一个阻塞点)】
  修完假分辨率后，日志显示输入连上、几何也对了，但输出引脚的 Connect
  一次都没被调用，滤镜随即被踢出图。为了不再靠猜，我搭了一个【真实
  DirectShow 图】复现:
      LAV Splitter Source -> LAV Video Decoder -> [本滤镜] -> EVR
  结果一步就把问题钉死了:
      decoder -> 本滤镜    0x00000000 OK
      本滤镜  -> EVR       0x80004005 FAIL      <- 就是这里
  再加一步对照实验(decoder 直接连 EVR)发现:
      decoder -> EVR 直连   0x00000000 OK       <- EVR 本身没问题
      EVR QueryAccept(本滤镜的类型) = S_OK      <- 类型也没问题!
  也就是说【EVR 接受我们的媒体类型，但连接仍然失败】。加上分配器日志后
  真正的调用链是:
      GetAllocator  -> 拿到 EVR 的分配器
      GetProperties -> cbBuffer = 0        <- EVR 还没 commit
      于是判定"太小"，调 SetProperties
      SetProperties -> 0x80004005 FAIL     <- EVR 拒绝
  根因: 对变换滤镜而言，【分配器应当由上游(本滤镜)自己创建】，
  然后通过 NotifyAllocator 告知下游。EVR 自带的分配器在 commit 之前
  cbBuffer 报 0，此时对它 SetProperties 会被 E_FAIL 拒绝 —— 连接就此失败。
  修法: 无条件创建并使用自己的 MemoryAllocator(3 个缓冲、
  cbBuffer = 真实帧大小)，再 NotifyAllocator 通知下游。
  实测(1920x1440 NV12 真实播放):
      收到 299  已处理 261  直通 38  引擎就绪 1  13.3 ms/帧
  (前 38 帧是模型加载期间的直通)

  【这次的验证工具(临时探针，验证后已删除)】
    - _realgraph: 用你的真实素材搭出 LAV->滤镜->EVR 的完整图，
      逐步打印每一步的 HRESULT。定位"哪一步断"最有效。
    - _evr_control: 对照实验。A) decoder 直接连 EVR  B) 经本滤镜。
      两者对比能区分"是 EVR 不接受"还是"是本滤镜的错"。
    - _nullgraph: 把末端换成 Null Renderer(不需要窗口)，这样图能全速跑，
      用来证明【帧真的流过滤镜并进入引擎】(EVR 无窗口时跑一帧就停)。
  经验: 手搓引脚桩子测不出真实渲染器的行为。要复现播放器问题，
  必须用【真解码器 + 真渲染器】搭图。

  【PotPlayer 强制开启后崩溃 0xC0000005 的根因与修复】
  现象: 管理员注册成功后，PotPlayer 添加滤镜并强制开启 -> 立即
        Unhandled exception 0xC0000005(AccessViolation)，faulting module
        正是 dlssnr_dshow.dll。
  定位方法(没有装调试器也能做):
    a) PotPlayer 自己写了崩溃报告:
         %APPDATA%\Daum\PotPlayer\Log\PotPlayer.exc.xml  (含异常码/寄存器/栈)
         %APPDATA%\Daum\PotPlayer\Log\PotPlayer.exc.dmp  (minidump)
       xml 里给出 ModuleName="dlssnr_dshow.dll" 和 EIP=0x7FF8D24A2FE7。
    b) 用纯 stdlib 解析 minidump 的模块表拿到该 dll 的加载基址 0x7FF8D24A0000，
       于是 RVA = 0x2FE7。
    c) 编译时加 /MAP 生成 dlssnr_dshow.map，把 RVA 0x2FE7 落回符号:
         最近的函数 = CEnumMediaTypes::Next  (+0x77)
  根因: Next() 里写的是 `CopyMediaType(*pp[n], ...)`。
        pp 是【输出】数组: 调用方只提供 c 个【指针槽位】，由被调方负责分配
        每个 AM_MEDIA_TYPE。这些槽位里是【未初始化的指针】，解引用 *pp[n]
        就是往垃圾地址写 -> 访问违例。崩溃现场 EAX=ESI=EDI=EBP=0、
        LogicalAddress=0，正是空指针解引用。
        正确写法: 每个槽位自己 CoTaskMemAlloc(sizeof(AM_MEDIA_TYPE)) 再填。
  为什么之前的测试没抓到: 我的测试都是「带显式媒体类型直接 Connect」，
        从来没调用 EnumMediaTypes。而播放器在协商阶段【一定会】枚举媒体类型
        —— 所以自测全绿、一进播放器就崩。教训: 协商类的接口必须单独测。
  附带修掉的两个问题:
    - Clone() 没检查 CopyMediaType 返回值，且未把多余槽位清零，
      析构里的 FreeMediaType 可能碰到未初始化内存；已清零并只拷贝有效项。
    - 引脚持有 m_pFilter 裸指针，而图可能比滤镜活得久 -> 析构时先 Orphan()
      把回指针置空，之后任何调用优雅失败而不是 use-after-free。

  【顺带发现并修掉的线程竞态(同一轮排查)】
   Process() 原先 EnterCriticalSection 后调 dlssnr2_process，属于流线程；
   而工作线程的 DoInit() 调 dlssnr2_init【没有持锁】。init 会释放并重建
   D3D12 feature 与纹理，与正在 process 的流线程撞上就是 use-after-free。
   修法: 整个 init 持锁，Process() 改用 TryEnterCriticalSection，拿不到锁
   (正在 init)就本帧直通 —— 这同时避免了「流线程被 13 秒模型加载卡住」的新
   死锁(实测: init 占用锁 1.4 s 期间，单帧 Receive 最坏只有 7 ms)。
   另外给 DLL 加载失败加了 latch，避免失败后反复重试。

  回归测试(均为临时探针，验证后已删除):
    - _enum_test: 专门跑 EnumMediaTypes 的 Next(1) 循环 / 多槽位 / Clone /
      耗尽后 Skip，复现并锁定上述崩溃。修复后 PASS (0 failures)。
    - _race_test: 在 init 进行中持续投帧，验证单帧 Receive 不被 init 卡住
      (实测最坏 7 ms，且总耗时与 Sleep 粒度算出的节拍地板一致)，并验证
      READY 后引擎确实生效(输出与输入的平均绝对差 3.01，直通则恒为 0)。
      修复后 PASS (0 failures)。

【实时控制面板】(面板已改为滤镜 DLL 内的原生窗口，随滤镜一起在 dlssnr-filter 仓库)
  它是怎么和滤镜通信的: 一块命名共享内存 Local\DLSSNR_DShow_Shared_v1，
  滤镜每帧读一次"请求区"、写一次"状态区"。面板因此能同时回答两个问题:

  (1) 「滤镜到底加载了没有?」
      状态区有 heartbeat(每帧自增)、framesSeen/Processed/Passthrough 计数、
      engineReady、引擎会话尺寸、当前视频尺寸/像素格式、以及一句话状态。
      面板据此显示:
        - 已加载 · 正在处理            (绿)
        - 已加载 · 引擎加载中…         (橙, 模型加载期间)
        - 已加载,但当前没有帧(暂停/未播放)
        - 滤镜未加载(共享内存存在但 magic 不对)
      关键点: heartbeat 停止跳动 = 没在解码; magic 不对 = 滤镜根本没被加载。
      这比"看画面对比"可靠得多。
      另外状态区还会给出【为什么在直通】的原因码(见下), 不再需要猜。

  (2) 「能不能边看边调?」
      能。四个参数(开关/风格/强度/局部色调/局部结构)【都实测可以在会话中途
      生效, 不需要重建 feature】(验证: intensity 1.0 vs 0.0 输出 mean|diff|
      2.58; localTone 1.36; localStruct 2.71; style 0 vs 1 为 4.52)。
      面板写请求区并自增 reqSeq, 滤镜应用后回写 reqApplied, 面板比对两者
      来确认"确实生效了", 而不是只把滑块动了一下。

  直通原因码(面板会翻译成中文):
      0 正常处理 / 1 引擎加载中 / 2 已关闭 / 3 输入不是 RGB24 /
      4 分辨率与会话不一致 / 5 引擎报错 / 6 初始化永久失败

  两个实现细节值得记下(都踩过):
   a) 【参数在帧首应用】ApplyRequests() 必须放在处理【之前】, 遥测发布放在
      之后。一开始两件事都写在帧尾, 结果是滑块改动要等下一帧才生效 —— 实测
      表现为"同一设置连测两次, 第一次均值 134.0019、第二次 133.9776"。
      拆开后两次结果逐字节相同(diff 0.0000)。
   b) 【CreateFileMapping 参数顺序】签名是
      (hFile, attrs, protect, maxSizeHIGH, maxSizeLOW, name)。
      把大小写进 HIGH 槽位等于申请 大小×2^32 字节, 直接失败并返回
      ERROR_NO_SYSTEM_RESOURCES(1455)。第一版就是这么写的, 滤镜侧一直
      连不上共享内存。面板侧的 Python 反而写对了 —— 导致"面板正常、滤镜不
      发布"这种很容易看错方向的现象。

  实测去噪强度(平坦噪声场 std=20, 稳态):
      强度   0% 去噪 0.0% | 25% 2.2% | 50% 4.2% | 75% 6.0% | 100% 7.7%
      单调可用作滑块。勾选开关为【真正直通】(输出逐字节等于输入,
      |disabled - INPUT| = 0); 而强度拉到 0% 时引擎仍在跑。

  关于测试方法的两条教训(都实际误导过我):
    - 【不要用均值比较两帧是否相同】本效果保持均值不变(输入均值 127.49,
      输出也是 127.49), 所以两个完全不同的输出可以有相同的均值。判断"是否
      直通"必须逐字节比较(|output - input| == 0)。
    - 【不要用纯噪声算相关性】拿纯噪声算 corr(输出,输入) 会得到 ~0.38 这种
      "看起来完全不像"的数字 —— 那恰恰是去噪成功的表现。验证几何/通道是否
      正确要用【有结构的输入】(水平渐变), 此时 corr = 1.0000。

【原理】
  用 NVIDIA DLSS5 神经渲染(Feature 18，同分辨率增强/去噪)对视频逐帧处理。
  宿主 DLL 预编译并内置 SDK 签名门禁绕法(caller 伪装 + Init_Ext 0x0876232C)。

【重新编译宿主】(改了 dlssnr_host2.cpp 后；以下命令在仓库根目录执行)
  cmd /c "call <VC>\Auxiliary\Build\vcvars64.bat && cl /nologo /EHsc /O2 /utf-8 ^
    /I deps\sdk_include src\dlssnr_host2.cpp ^
    /LD /Fe:app\dlssnr_host2.dll ^
    /link d3d12.lib dxgi.lib user32.lib advapi32.lib ^
    deps\sdk_lib\nvsdk_ngx_s.lib"

  注: 也可以直接跑 tools\build.bat(它自动处理这些路径)。

【重新编译 RTX Video 宿主】(改了 vfx_host.cpp / truehdr_host.cpp 后)
  vfx_host.dll 只用系统库，动态加载 NVVideoEffects/NVCVImage，不需要导入库:
  cmd /c "call <VC>\Auxiliary\Build\vcvars64.bat && cl /nologo /EHsc /O2 /utf-8 ^
    src\vfx_host.cpp /LD /Fe:app\vfx_host.dll"

  truehdr_host.dll 用官方 SDK 的头文件和导入库:
  cmd /c "call <VC>\Auxiliary\Build\vcvars64.bat && cl /nologo /EHsc /O2 /utf-8 ^
    /I deps\rtx_video_sdk\include src\truehdr_host.cpp ^
    /LD /Fe:app\truehdr_host.dll ^
    /link d3d11.lib dxgi.lib advapi32.lib user32.lib ^
    deps\rtx_video_sdk\lib\Windows\x64\nvsdk_ngx_s.lib"

【SDK 来源】
  RTX Video SDK 1.1.0 是 NVIDIA NGC 上的公开制品，可自行重新下载:
    https://api.ngc.nvidia.com/v2/models/nvidia/multimedia/dlpp/versions/1.5/files/RTX_Video_SDK_v1.1.0.zip
    SHA256: ABF4F34E2B5A618E355B0D5A0365D8ECC3DB4396E756E4C850A867E1AE2ED69E
  许可: LicenseRef-NvidiaProprietary（非开源，随包附带许可 PDF）。自用没问题，
  公开发布前需自行确认许可条款。

【调试】
  宿主日志:  host2_log.txt (DLSS 初始化/错误)
  逐阶段计时: 在项目目录建一个空文件 host2_perf.on，再跑，会生成 host2_perf.txt
              (convIn / eval / gpu / convOut 各阶段毫秒数)
  RTX Video 日志: vfx_log.txt(会话创建/模式切换)  thdr_log.txt(TrueHDR/NGX 状态)
  RTX 逐阶段计时: 建空文件 vfx_perf.on / thdr_perf.on
                 注意: PERF 行里的 h2d/run/d2h 是【提交】耗时(CUDA 异步)，
                 真实计算时间在紧跟其后的 sync 里(见「已修复的坑」第 13 条)。
                 要准数字用 vfx_bench_transfer 导出(每相末同步一次)。
