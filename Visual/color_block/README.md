# SmartClock 视觉端（鲁班猫4 → STM32）

用 USB 摄像头识别**红卡**，计算卡片中心，按既定协议通过串口把坐标回传给
STM32，驱动屏幕上的红色挡板跟随卡片移动。检测用 **YOLO26 + RK3588 NPU**。

本目录是 **SmartClock 项目的视觉端**，与仓库里的 STM32 固件（`Code/`）配套，
协议定义见 `Docs/视觉坐标与串口协议.md`。

---

## 一、这套东西做什么

```
   ┌──────────────┐      ┌──────────────────────┐      ┌──────────────┐
   │ USB 摄像头   │─────▶│  鲁班猫4（本目录）     │─────▶│   STM32      │
   │  (UVC)       │ 图像 │                      │ 串口 │              │
   └──────────────┘      │  YOLO26 → RKNN       │10字节│  驱动挡板     │
                         │  NPU 检测红卡         │ 帧   │  480x800 LCD │
                         │  中心定位 + 坐标映射   │      │              │
                         └──────────────────────┘      └──────────────┘
```

| 环节 | 做法 | 为什么这么选 |
|---|---|---|
| 卡片检测 | YOLO26（自训练）→ RKNN，跑在 NPU 上 | 黑底红卡场景下比 HSV 稳得多，不怕反光与阴影 |
| 降级方案 | HSV 阈值 + 形态学（`detector.engine: hsv`） | 模型出问题时仍能跑，也方便判断是"模型问题"还是"系统问题" |
| 中心定位 | 检测框中心（YOLO）／图像矩（HSV） | 外接框中心比质心更抗部分遮挡造成的跳动 |
| 坐标映射 | **双 ROI**：A 映射 / B 检测 | 解决"色块出界时中心算成可见部分中心"→ 挡板贴不到最边上 |
| 抖动抑制 | EMA 平滑 + 死区 | 消除像素级抖动，避免挡板哆嗦 |
| 串口 | 纯 Python `termios` | 板子上没有 pyserial，这样免依赖 |
| 协议 | 10 字节帧 + CRC-8 | 与固件 `bsp_game_crc8` 逐比特一致 |
| 实时画面 | OpenCV + Pillow 中文渲染 | 画面里直接点按钮调参，不用改配置文件 |

### 双 ROI 是什么，为什么必须有

画面上有两个矩形：

```
   ┌──────────── B：检测区域（绿色虚线）──────────┐
   │   ┌──────── A：映射区域（黄色实线）──────┐   │
   │   │                                    │   │
   │   │            🟥 红卡                  │   │
   │   └────────────────────────────────────┘   │
   └────────────────────────────────────────────┘
```

- **A（映射区域）** 决定"像素坐标 ↔ 游戏坐标"的对应关系。它是**逻辑边界**：
  中心移出 A 就代表挡板贴边，会被限幅到最边上。
- **B（检测区域）** 只决定"往模型里送多大范围"。B 必须**比 A 大一圈**。

**如果两者是同一个框（早期实现），会出这个问题**：红卡中心移到 A 的边界时，
卡片有一半落在框外，模型只能看到剩下的那一半，算出来的中心是**可见部分的
中心**而不是真实中心 —— 表现就是挡板贴到边上却**到不了最边**，紧贴边界
落下的星星接不到。

让 B 比 A 大一圈（`detector.roi_margin`，默认 60 像素），卡片在 A 边缘时仍
完整落在 B 内，中心就算得准；而映射时超出 A 的中心照旧被限幅到边缘，
正是"挡板贴边"想要的语义。

界面截图（`docs/images/`）：

| 情形 | 画面 |
|---|---|
| B 太小 → 卡片被裁、报警 | `docs/images/ui_warn.png` |
| B = A + 60 → 卡片完整、无告警 | `docs/images/ui_ok.png` |
| 卡片顶在 A 左边界，B = A：**中心偏 22.5px** | `docs/images/edge_center_BA.png` |
| 同一位置，B = A+60：**中心误差 0.0px** | `docs/images/edge_center_BA_margin.png` |

> ⚠️ `roi_margin` 必须 **大于红卡宽度的一半**（卡在画面里约 90px 宽 → 至少 45）。
> 否则卡片仍会被裁掉一角，问题等于没解决。界面上有自动检查：**检测框一旦
> 贴到 B 的边界，B 会变红并在状态栏报警**，按 `B +` 放大即可。

另外 **B 必须包住 A**，这一条由程序强制保证：不管你怎么拖 B、按 `B -10`，
只要 B 会盖不住 A，程序就自动把它撑到刚好包住（状态栏会说明）。
因为 B 盖不住 A 时，你遇到的 bug 会原样复现，而且从画面上看不出来。

### 这个问题可以量出来

```bash
python3 scripts/demo_edge_center.py
```

用合成图像把"卡片中心顶到 A 边界"这件事算清楚，输出实测误差：

```
  位置             卡片真实中心X      B=A 误差       B=A+边距 误差
  左边               156.0     22.5 px          0.0 px
  右出界              504.0     34.0 px          4.0 px
  B=A       ：最大误差   34.0 px  平均   8.4 px
  B=A+边距  ：最大误差    4.0 px  平均   0.7 px

  B=A       最坏情况下，挡板离墙还差约 37 游戏像素
  B=A+边距    最坏情况下，挡板离墙还差约 4 游戏像素
```

**"离墙差 37 个游戏像素"就是你接不到边缘星星的原因。**
对照图存在 `assets/output/edge_center_*.png`（白叉=真实中心，黄叉=算出的中心）。

---

## 二、快速开始

### 1. 离线验证（不需要任何硬件）

```bash
cd Visual/color_block
python3 scripts/demo_offline.py
```

打印 9 帧的完整处理过程（像素坐标 → 归一化 → 游戏坐标 → 串口字节），
并生成一张行程示意图。看到 `✅ 离线演示完成` 就说明软件本身没问题。

### 2. 一键启动（板子上日常就用这个）

```bash
bash tools/start.sh            # 检查环境 + 启动主程序（带实时画面）
bash tools/start.sh --dry-run  # 空跑，不写串口
bash tools/start.sh --check    # 只检查环境，不启动
```

也可以在 `~/.local/bin/` 装几个短命令，之后从任何目录直接调用：

```bash
bash tools/make_shortcuts.sh
card-start      # = tools/start.sh
card-run        # 主程序
card-check      # 环境检查
card-verify     # 模型单图验证
card-validate   # 数据集定量评估
card-preview    # 仅预览
card-diagnose   # 模型/运行时诊断
serial-ping     # 串口测试
virtual-stm32   # 虚拟 STM32
```

### 3. 实时画面里的控制面板

只要用到摄像头就**自动弹窗**（`debug.preview: true`）。

**操作方式是"按一下键 → 在画面里点两下"**（先左上角、再右下角），这是主路径：

| 键盘 | 作用 |
|---|---|
| `r` （或 `a`） | **框选 A（映射区）**：按一下，然后在画面里点两下确定左上角、右下角 |
| `t` （或 `b`） | **框选 B（检测区）**：同上 |
| `0` | B 重置为"A 向外扩 60 像素" |
| `+` / `=` | B 每边各放大 10 像素（以中心为基准） |
| `-` | B 每边各缩小 10 像素 |
| `f` | B 切到整幅画面 / 切回 A+边距 |
| `s` | 保存到 `config.yaml` |
| `ESC` | 只取消正在进行的框选（不退出程序） |
| `q` | 退出 |

框选过程中**整幅画面都归框选用**，画面上的按钮不会抢走你的点击；
鼠标移动时会画"橡皮筋"预览矩形并实时显示尺寸；点下第二个角立刻生效。

画面底部同时有一排**可点击的按钮**（`框A` `框B` `B=A+60` `B+10` `B-10`
`整幅` `保存` `退出`）。按钮画在画面**内部**，所以窗口尺寸就等于摄像头
画面尺寸，屏幕再小也不会出现"按钮跑到屏幕外点不到"。

状态栏一直显示 A、B 的当前值；有告警时整行变红。

> 画面中央的**教学窗默认关闭**（不挡画面）。忘了怎么操作时，把
> `config.yaml` 里的 `debug.show_guide` 改成 `true`，启动后会在画面中间
> 显示一份图文步骤（约 12 秒，或框完 A 之后自动消失），样子见
> `docs/images/ui_startup_guide.png`。

界面长这样（`docs/images/ui_overlay_layout.png`）：

```
┌──────────────────────────────────────────────┐
│  B 检测区（绿色虚线）                          │
│    ┌── A 映射区（黄色实线）──┐                 │
│    │        🟥 卡片           │                 │
│    └─────────────────────────┘                 │
│  r=框选A(点两下) t=框选B 0=B=A+边距 …           │
│  [框A][框B]│[B=A+60][B+10][B-10]│[整幅][保存][退出]│ ← 压在画面内部
│  A 映射区=(156,20,326,428)  B 检测区=(96,0,446,480)│
└──────────────────────────────────────────────┘
```

### 4. 完整流程（第一次上手）

```bash
# ① 找摄像头
python3 scripts/camera_check.py --probe-all

# ①' 看双 ROI 到底修好了什么（无需硬件，几秒钟）
python3 scripts/demo_edge_center.py

# ② 确认模型没问题
python3 scripts/verify_model.py --image assets/captures/s1.jpg   # 单图
python3 scripts/validate_dataset.py                              # 量化评估

# ③ 框定两个 ROI：窗口里按 r → 点两下 → 按 0 → 按 s
python3 scripts/main.py --dry-run

# ④ 验证串口（谁能回 OK 就是它）
python3 scripts/serial_test.py --probe

# ⑤ 正式运行
python3 scripts/main.py
```

---

## 三、目录结构

```
color_block/
├── README.md                     ← 你正在看的文件
├── config.yaml                   ← 全部可调参数（带中文注释）
│
├── src/                          ← 核心库
│   ├── protocol.py               ★ 10字节帧 + CRC-8（与固件严格对齐）
│   ├── serialport.py             ★ 纯 Python 串口（无 pyserial 依赖）
│   ├── camera.py                 ★ USB UVC 采集封装
│   ├── yolo.py                   ★ letterbox + 输出解码（cxcywh）
│   ├── rknn_ctypes.py            ★ librknnrt.so 的 ctypes 绑定
│   ├── card_detector.py          ★ YOLO/RKNN 检测器（含 ROI 裁切）
│   ├── detector.py               ★ HSV 检测器 + 统一 Detection 结构
│   ├── mapper.py                 ★ 坐标映射 + EMA 平滑 + 死区
│   ├── pipeline.py               ★ 主流水线（依赖注入，可离线测试）
│   ├── roi_editor.py             ★ 双 ROI 编辑 + 控制面板
│   ├── ui_buttons.py             ★ 画面内按钮条 / 状态栏（自绘，无需 tkinter）
│   ├── text_cjk.py               ★ 用 Pillow 在画面上画中文（cv2.putText 不支持）
│   ├── dataset.py                  数据集解析与评估指标
│   ├── config.py                   配置加载、校验与回写
│   └── cli.py                      命令行公共设施
│
├── scripts/                      ← 命令行工具
│   ├── main.py                     主程序：追踪并发帧
│   ├── demo_offline.py             离线演示（无需硬件）
│   ├── demo_edge_center.py         演示"边缘中心算偏"及双 ROI 的修正效果
│   ├── camera_check.py             摄像头诊断
│   ├── verify_model.py             模型单图验证（带真值对比）
│   ├── validate_dataset.py         数据集定量评估（精确率/召回率/IoU）
│   ├── diagnose_model.py           模型/运行时故障诊断
│   ├── preview.py                  仅预览，不连串口
│   ├── color_pick.py               HSV 降级方案的取色标定
│   └── serial_test.py              串口分层自测
│
├── tests/                        ← 单元测试（459 个用例，全部离线可跑）
│   ├── test_protocol.py            协议与 CRC（含固件交叉验证）
│   ├── test_pipeline.py            检测→映射→组帧 端到端
│   ├── test_preview_ui.py          预览界面与流水线的接线
│   ├── test_robustness.py          串口掉线/窗口关闭/ROI 越界等现场故障
│   ├── test_preview_script.py      preview.py 与共享 ROI 编辑器的接线
│   ├── test_roi_editor.py          双 ROI 状态机、按钮、贴边告警
│   ├── test_ui_buttons.py          按钮布局与命中判定
│   ├── test_text_cjk.py            中文渲染与字体缺失降级
│   ├── test_card_detector.py       YOLO 检测器（假运行时）
│   ├── test_dataset.py             评估指标
│   ├── test_serial_e2e.py          串口端到端
│   ├── test_code_hygiene.py        代码卫生（重名方法、ASCII 窗口名等）
│   └── fakes.py                    假摄像头 / 假串口 / 合成图像
│
├── tools/                        ← 安装、验证与打包
│   ├── start.sh                    一键启动（检查 + 运行）
│   ├── install.sh                  依赖检查与安装
│   ├── make_shortcuts.sh           生成 ~/.local/bin 短命令
│   ├── run_all_tests.sh            一键跑全部验证
│   ├── package.sh                  分类打包成发布压缩包
│   ├── make_docx.py                Markdown → Word（零依赖，含插图/表格/代码块）
│   └── crc_crosscheck/             CRC 与固件交叉验证
│       ├── crc_ref.c               固件原版 CRC 函数（原样摘录）
│       └── crosscheck.py           10 万组随机向量比对
│
├── examples/                     ← 最小可运行示例
│   ├── camera_view.py              只看摄像头画面
│   ├── serial_ping.py              串口通讯冒烟测试
│   └── virtual_stm32.py            虚拟 STM32（无板子也能联调）
│
├── docs/                         ← 文档
│   ├── 部署与接线.md
│   ├── 标定指南.md
│   ├── 串口协议.md
│   ├── 验收与排查.md
│   ├── 项目功能与原理详解.md        ★ 答辩素材（全部功能 + 原理 + PPT 大纲）
│   ├── 关键原理讲解-教学版.md        ★ 教学版：六个关键原理从零讲起 + 自测题
│   ├── 关键原理讲解-教学版.docx      ← 上面的 Word 版本（含插图，可直接打印）
│   └── images/                     双 ROI 界面截图与对照图
│
├── redcard.yolo26/               ← 红卡数据集（Roboflow 导出）
│   ├── data.yaml
│   └── train/ valid/ test/         229 / 63 / 32 张，640x480
│
└── assets/
    ├── captures/                   抓帧与标定图
    └── output/                     处理结果与演示图
```

★ = 核心模块，改动前请先读对应文档并跑测试。

---

## 四、模型与数据集

| 项目 | 值 |
|---|---|
| 结构 | YOLO26，单类 `card` |
| 训练集 | 229 张训练 / 63 张验证 / 32 张测试，640×480 |
| 导出 | ONNX → RKNN（FP16，`do_quantization=False`） |
| 输入 | `[1, 640, 640, 3]` NHWC float16 |
| 输出 | `[1, 5, 8400]`，框格式 **cxcywh**（不是 xyxy！） |
| 归一化 | 已烧进 RKNN（`mean=0, std=255`），Python 侧只做 letterbox |
| 模型文件 | `Visual/lbm/model/best.rknn`（8.2 MB） |

**实测指标**（测试集 32 张，真实运行结果）：

| 指标 | 数值 |
|---|---|
| 精确率 | **100%** |
| 召回率 | **96.97%**（唯一的"漏检"是 `img_088` 的重复标注） |
| 平均 IoU | **0.950** |
| 中心误差 | 平均 **1.0 px** / 最大 2.5 px |

> 板子上没有 `rknn_toolkit_lite2`（官方轮子只到 cp312，板子是 Python 3.14），
> 所以本项目用 ctypes 直接调 `librknnrt.so`，见 `src/rknn_ctypes.py`。

---

## 五、关于正确性的一点说明

协议对接最怕"看着一样但差一位"。本项目用硬核方式验证：

**① CRC：把 STM32 固件里的 CRC 函数原样摘录出来编译，与 Python 实现跑
10 万组随机向量做逐比特比对。**

```bash
python3 tools/crc_crosscheck/crosscheck.py 100000
# ✅ 全部 100000 组向量完全一致（逐位版与查表版均通过）
```

固件源码位置：`Code/BSP/Src/bsp_board.c:208`，摘录副本：`tools/crc_crosscheck/crc_ref.c`。

协议文档给出的示例帧（中心坐标 212,578）实测结果：

```
AA 55 06 01 D4 00 42 02 00 7D
                            └─ CRC = 0x7D
```

**② 整链路：合成图像进，真实协议帧出。** `tests/test_pipeline.py` 用可控的
合成红色方块驱动整条流水线，断言"喂进一个位于 (x, y) 的方块，STM32 会收到
哪些字节"，期望值在测试里独立重算一遍，不复用被测代码的公式。

**③ 真机实测**（已在本板 + 真实 STM32 上跑通）：串口 `--probe` 收到 `OK`、
`--sweep` 挡板跟随；连续 144 秒 dry-run 共 2407 帧 @16.7 fps，检出率 66.5%
（未检出时卡片不在视野内），置信度均值 0.865，画面亮度均值 137.3
（训练集 137.4，说明现场光照与训练条件一致）。

---

## 六、依赖

| 依赖 | 用途 | 板子上是否已装 |
|---|---|---|
| Python 3.8+ | 运行环境 | ✅ 3.14.4 |
| `opencv-python` (cv2) | 图像处理与画面显示 | ✅ 4.10.0（Qt5 后端） |
| `numpy` | 数组运算 | ✅ 2.3.5 |
| `PyYAML` | 配置文件 | ✅ 6.0.3 |
| `Pillow` | 在画面上画中文 | ✅ 12.1.1 |
| `librknnrt.so` | NPU 运行时 | ✅ 系统自带 |
| 中文字体 | 界面中文 | ✅ Noto Sans CJK / 文泉驿 |
| `v4l-utils` | 摄像头诊断（可选） | ✅ 1.32.0 |
| **pyserial** | —— | ❌ **刻意不使用** |
| **tkinter** | —— | ❌ **刻意不使用**（按钮自己画） |

检查环境：

```bash
bash tools/install.sh --check
```

---

## 七、常见问题速查

| 现象 | 先看这里 |
|---|---|
| 找不到摄像头 | `python3 scripts/camera_check.py --probe-all` |
| 检测不到红卡 | `python3 scripts/verify_model.py --image <图>` 先确认模型；再确认卡片在 B 内 |
| **挡板贴不到最边上** | **B 太小**：看 B 有没有变红报警，按 `+` 放大（见第一节「双 ROI」） |
| 画面里没有文字/文字变空白 | 缺中文字体：`python3 -c "from src.text_cjk import describe; print(describe())"` |
| 画面窗口是空的 | 窗口标题必须纯 ASCII；`bash tools/run_all_tests.sh` 会检查这一条 |
| 窗口弹出了但**没有两个大框/按钮/指引** | 见 `docs/验收与排查.md` D8（曾经漏调 `pipeline.open()` 导致，已修 + 加回归测试） |
| 误检背景杂物 | 调高 `detector.conf_threshold`；或缩小 B |
| 挡板方向相反 | `mapping.invert_x` / `invert_y` 打开 |
| 挡板抖动 | 减小 `mapping.smoothing`；增大 `mapping.deadband` |
| 挡板反应迟钝 | 增大 `mapping.smoothing`；减小 `deadband` |
| 串口打不开 | 用户加入 `dialout` 组：`sudo usermod -aG dialout $USER`（重新登录生效） |
| 单片机回 ERROR | 波特率不一致，或 CRC 不匹配（跑 `bash tools/run_all_tests.sh`） |
| 单片机没反应 | TX/RX 接反、没共地、或 STM32 没调用 `BSP_GameSerial_Start()` |
| 板子上没有可用 UART 引脚 | 设备树里 10 个片上 UART 一个都没开，**用 USB-TTL 转串口**（`/dev/ttyUSB0`） |

详细排查见 `docs/验收与排查.md`。

---

## 八、打包与交付

```bash
bash tools/package.sh
```

会按类别整理并生成 `dist/SmartClock-视觉端-<日期>.tar.gz`，同时输出
`MANIFEST.txt` 列出包内每个文件的用途。

---

## 九、安全与合规提醒

本方案用于**本机视觉与离线串口通信**，不涉及任何网络功能。

另外提醒一句与本项目无关但值得注意的事：鲁班猫出厂默认密码
（`cat`/`temppwd`、`root`/`root`）是公开信息，而板子的 SSH 默认监听在
所有网卡上。**建议尽快修改密码**，尤其是 `root` 的：

```bash
passwd            # 修改 cat 用户密码
sudo passwd root  # 修改 root 密码
```
