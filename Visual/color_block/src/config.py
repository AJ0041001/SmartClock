"""配置管理 —— 用 YAML 描述摄像头、检测、映射、串口四组参数。

设计原则
--------
1. **全部可配置**：换颜色、换摄像头、换串口都不需要改代码。
2. **缺省即合理**：配置文件缺失或字段残缺时，用一组经过验证的默认值补齐，
   而不是直接崩溃 —— 现场调试时这一点非常重要。
3. **可回写**：标定脚本会把结果写回配置文件，下次直接生效。
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .detector import HsvRange, preset_ranges
from .mapper import Roi
from .protocol import (
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
    ControlType,
)


# ──────────────────────────────────────────────────────────────────────
# 分节配置
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CameraConfig:
    """USB 摄像头参数。"""

    index: int = 0
    """``/dev/videoN`` 的 N。插了多个摄像头时按需改。"""

    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"
    """MJPG 在 USB2.0 上更省带宽；某些摄像头不支持时改成 YUYV。"""


@dataclass
class DetectorConfig:
    """检测参数 —— 支持两种引擎。"""

    engine: str = "yolo"
    """检测引擎：

    · ``"yolo"`` —— 用仓库自带的 YOLO26 / RKNN 模型在 NPU 上检测红卡。
      **这是本项目的主用方案**，已用真实数据训练，抗光照与干扰能力强。
    · ``"hsv"``  —— 传统 HSV 阈值分割。无需模型，适合纯色块快速验证，
      或作为 YOLO 失效时的降级方案。
    """

    # ── YOLO 引擎参数 ──────────────────────────────────────────────
    model_path: str = ""
    """``.rknn`` 模型路径。留空则自动定位 Visual/lbm/model/best.rknn。"""

    conf_threshold: float = 0.5
    """置信度阈值。黑底红卡场景可以偏高（0.5~0.6）来压低误检。"""

    nms_threshold: float = 0.45
    """NMS 去重阈值。卡片重叠时才需要调。"""

    input_size: int = 640
    """模型输入边长，须与训练/转换时一致。"""

    core_mask: int = 7
    """NPU 核心掩码：1=核0，2=核1，4=核2，7=三核全用。"""

    box_format: str = "auto"
    """框输出格式：auto / xywh / xyxy。

    ⚠️ ultralytics 导出的原始输出是 **xywh（中心+宽高）**，
    不是 xyxy。用错会导致框严重错位且不报错。默认 auto 自动判别。
    """

    warmup: bool = True
    """启动时是否先跑一次空推理。首帧通常明显慢，预热可避免算进正式统计。"""

    # ── HSV 引擎参数 ───────────────────────────────────────────────
    preset: str = "red"
    """预设色名（red/green/blue/yellow/...）。设为 custom 时用 custom_ranges。"""

    custom_ranges: list[dict[str, list[int]]] = field(default_factory=list)
    """自定义 HSV 区间，格式 [{lower: [h,s,v], upper: [h,s,v]}, ...]。"""

    min_area: float = 400.0
    """最小面积（像素²）。HSV 用于滤噪；YOLO 场景下也可用来排除过小的框。"""

    max_area: float | None = None
    """最大面积限制。可用来排除大面积同色背景。"""

    blur_size: int = 5
    morph_kernel: int = 5
    morph_iterations: int = 2
    max_blocks: int = 1

    # ── 检测区域 B（独立于映射区域 A）──────────────────────────────
    #  这一组参数解决"色块出界时中心算不准"的问题，详见 build_roi() 的说明。
    #
    #  roi_w / roi_h 为 0 时表示"没单独设"，此时自动取 A 向外扩张 roi_margin。
    roi_x: int = 0
    roi_y: int = 0
    roi_w: int = 0
    roi_h: int = 0

    roi_margin: int = 60
    """B 比 A 每边大多少像素。

    取值规则（重要）
    ----------------
    这个值必须 **大于色块宽度的一半**，否则色块中心贴到 A 的边界时，
    色块仍会有一角落在 B 之外被裁掉 —— 中心照样算偏，问题等于没解决。

        红卡在 640x480 画面里约 90x170 像素 → 半宽 45
        默认取 60，留 15px 余量。

    太小 → 色块边缘时仍被裁掉一部分，中心偏移（挡板贴不到最边上）
    太大 → 送进模型的范围变大，色块相对变小，检出率可能下降

    因此界面做了两件事帮忙判断：
      · 检测框一旦贴到 B 的边界，B 会变红并在状态栏报警 → 按 B+ 放大
      · 觉得检出率低了就按 B- 缩小，两者现场对着画面调即可。
    """

    def build_ranges(self) -> list[HsvRange]:
        """解析出实际使用的 HSV 区间。"""
        if self.preset.lower() == "custom":
            if not self.custom_ranges:
                raise ValueError(
                    "preset 设为 custom 但 custom_ranges 为空。"
                    "请先运行 scripts/color_pick.py 生成区间。"
                )
            return [HsvRange.from_dict(d) for d in self.custom_ranges]
        return preset_ranges(self.preset)

    def build_roi(
        self,
        mapping_roi: "Roi | None" = None,
    ) -> tuple[int, int, int, int] | None:
        """构建**检测区域 B**，返回 (x, y, w, h) 或 None（=整幅画面）。

        为什么检测区域要独立于映射区域
        ------------------------------
        两者职责不同，混用会出问题：

          · **映射区域 A**（``mapping.roi_*``）决定"像素坐标 ↔ 游戏坐标"
            的对应关系。它是**逻辑边界**：色块中心移出 A 就代表挡板贴边。
          · **检测区域 B**（本函数）只决定"往模型里送多大范围"。

        如果两者相同，会出现这个问题：色块**部分移出 A** 时，模型只能
        看到框内剩下的那部分，算出来的中心是**可见部分的中心**而不是
        真实中心 —— 于是挡板到不了最边上，边缘落下的星星接不到。

        让 B 比 A 大一圈（``roi_margin``），色块在 A 的边缘时仍能被完整
        看到，中心就准了。而映射时超出 A 的中心会被**限幅到边缘**，
        正好是我们要的行为。
        """
        # 未显式配置 B（宽高都是 0）时，自动用 A 向外扩张 roi_margin
        if self.roi_w <= 0 or self.roi_h <= 0:
            if mapping_roi is None:
                return None
            margin = max(0, int(self.roi_margin))
            if margin == 0:
                return mapping_roi.as_tuple()
            return (
                mapping_roi.x - margin,
                mapping_roi.y - margin,
                mapping_roi.w + 2 * margin,
                mapping_roi.h + 2 * margin,
            )
        return (self.roi_x, self.roi_y, self.roi_w, self.roi_h)

    def auto_roi_from(
        self, mapping_roi: "Roi"
    ) -> tuple[int, int, int, int]:
        """按 A + 边距算出 B，供界面上「B = A + 边距」按钮使用。"""
        margin = max(0, int(self.roi_margin))
        return (
            mapping_roi.x - margin,
            mapping_roi.y - margin,
            mapping_roi.w + 2 * margin,
            mapping_roi.h + 2 * margin,
        )

    def set_roi(self, roi: tuple[int, int, int, int]) -> None:
        """把 B 写入配置字段。"""
        self.roi_x = int(roi[0])
        self.roi_y = int(roi[1])
        self.roi_w = int(roi[2])
        self.roi_h = int(roi[3])

    @property
    def engine_normalized(self) -> str:
        value = (self.engine or "").strip().lower()
        if value in ("yolo", "rknn", "npu", "model"):
            return "yolo"
        if value in ("hsv", "color", "colour"):
            return "hsv"
        raise ValueError(
            f"detector.engine 取值非法：{self.engine!r}，只能是 'yolo' 或 'hsv'"
        )


@dataclass
class MappingConfig:
    """摄像头坐标 → 游戏坐标的映射参数。"""

    roi_x: int = 0
    roi_y: int = 0
    roi_w: int = 640
    roi_h: int = 480
    """有效运动范围。默认整幅画面。"""

    invert_x: bool = False
    invert_y: bool = False
    swap_xy: bool = False
    """摄像头摆放方向修正开关。"""

    fixed_y: int | None = None
    """固定 Y 坐标。挡板若只做水平移动，建议设为 292（内区垂直居中）。"""

    smoothing: float | None = 0.35
    """EMA 平滑系数，0.1~1.0；null 表示关闭。"""

    deadband: float = 2.0
    """死区阈值（游戏像素）；0 表示关闭。"""

    def build_roi(self) -> Roi:
        return Roi(self.roi_x, self.roi_y, self.roi_w, self.roi_h)


@dataclass
class SerialConfig:
    """串口参数。"""

    port: str = "auto"
    """``auto`` 表示启动时自动探测；也可写死如 ``/dev/ttyS3``。"""

    baudrate: int = 115200
    """必须与 STM32 侧 USART2 一致。"""

    timeout: float = 0.5
    """等待 ``OK\\r\\n`` 回传的超时（秒）。"""

    require_ack: bool = False
    """True 时，收到 ERROR 或超时会打印警告（不影响继续发帧）。"""

    auto_probe_limit: int = 6
    """自动探测时最多尝试几个串口。"""


@dataclass
class LoopConfig:
    """主循环节奏控制。"""

    target_fps: float = 20.0
    """视觉端发送频率上限。STM32 侧有"超时回退按键"机制，别发太慢。"""

    send_interval: float = 0.05
    """两次发送之间的最小间隔（秒），与 target_fps 取较小限制。"""

    lost_frames: int = 5
    """连续多少帧没检测到色块后，发一帧 TYPE=02 交还按键控制。"""

    reacquire_delay: float = 0.3
    """丢失后重新捕获时，先等这么久让平滑器稳定，避免挡板猛跳。"""


@dataclass
class DebugConfig:
    """调试与可视化。"""

    preview: bool = True
    """是否弹出实时预览窗口。

    **默认开启** —— 只要用到摄像头就主动把画面显示出来，不用额外加参数。
    窗口分三块：视频区 + 按钮条 + 状态栏，按钮可以直接用鼠标点。

    窗口里有两个矩形，职责不同：
      · A（映射区，黄色实线）—— 像素坐标 ↔ 游戏坐标 的对应关系
      · B（检测区，绿色虚线）—— 送进模型的范围，必须比 A 大一圈

    快捷键（按钮条上都有对应的按钮）：
      ``a`` / ``b``  框选 A / B          ``0``  B = A + 边距
      ``+`` / ``-``  调 B 大小           ``f``  B 切整幅画面
      ``s``          保存到配置文件       ``q`` / ``ESC``  退出

    检测框贴到 B 的边界时 B 会变红并在状态栏报警 —— 说明 B 太小、
    中心会算偏，按 ``B +`` 放大即可。

    以下情况会自动关闭并给出提示，不会报错：
      · 没有图形界面（纯 SSH、无 DISPLAY）
      · 显式传了 --no-preview
    """

    show_guide: bool = False
    """启动时是否在画面上显示"怎么调 ROI"的教学指引。

    **默认关闭**：底部那行键盘提示一直在，看一次就记住了，没必要每次
    启动都拿一个框挡住画面中间。想再看一次就改成 true —— 显示约
    12 秒，或者在你框完 A 之后立刻消失。
    """

    ui_layout: str = "overlay"
    """界面布局：

    · ``"overlay"``（默认）—— 按钮条与状态行**压在画面内部**。
      窗口尺寸 = 摄像头画面尺寸，所以小屏幕上也不会出现
      "按钮跑到屏幕外、根本点不到"的问题。
    · ``"stacked"`` —— 按钮条与状态栏各占一行，窗口更高。
      屏幕够大时看着更清爽，但画面高 480 时窗口高会到 570。
    """

    save_frames: bool = False
    """是否把处理后的帧落盘，便于事后分析。"""

    save_dir: str = "assets/output"
    save_interval: int = 30
    """每多少帧存一张，避免磁盘被写满。"""

    log_level: str = "INFO"


@dataclass
class AppConfig:
    """顶层配置。"""

    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)

    control_type: int = int(ControlType.SERIAL_AND_KEYS)

    # ── 序列化 ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.to_dict(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig":
        """从字典构造，缺失字段用默认值补齐。

        刻意"宽松解析"：现场改配置时少写一个字段不该导致程序起不来。
        """
        data = data or {}
        config = cls()

        for section_field in fields(cls):
            name = section_field.name
            if name == "control_type":
                if "control_type" in data:
                    config.control_type = int(data["control_type"])
                continue

            section_value = data.get(name)
            if not isinstance(section_value, dict):
                continue

            section_obj = getattr(config, name)
            valid_names = {f.name for f in fields(section_obj)}
            for key, value in section_value.items():
                if key in valid_names:
                    setattr(section_obj, key, value)

        return config

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        """从 YAML 文件加载；文件不存在则返回默认配置。"""
        file_path = Path(path)
        if not file_path.exists():
            return cls()
        with file_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return cls.from_dict(data)

    def save(self, path: str | Path) -> None:
        """写回 YAML 文件。"""
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as handle:
            handle.write(self.to_yaml())

    def clone(self) -> "AppConfig":
        return copy.deepcopy(self)

    # ── 校验 ────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """检查配置自洽性，返回问题列表（空列表表示没问题）。"""
        problems: list[str] = []

        if self.camera.width <= 0 or self.camera.height <= 0:
            problems.append("camera.width / camera.height 必须为正数")

        if self.camera.fourcc not in ("MJPG", "YUYV", "YUY2"):
            problems.append(
                f"camera.fourcc={self.camera.fourcc!r} 不常见，"
                f"USB 摄像头一般用 MJPG 或 YUYV"
            )

        # ── 检测引擎校验 ──
        try:
            engine = self.detector.engine_normalized
        except ValueError as exc:
            problems.append(str(exc))
            engine = None

        if engine == "yolo":
            if self.detector.conf_threshold <= 0 or \
                    self.detector.conf_threshold >= 1:
                problems.append(
                    f"detector.conf_threshold="
                    f"{self.detector.conf_threshold} 应在 (0,1) 之间"
                )
            if self.detector.input_size <= 0 or \
                    self.detector.input_size % 32 != 0:
                problems.append(
                    f"detector.input_size={self.detector.input_size} "
                    f"应为 32 的正倍数（YOLO 的 stride 要求）"
                )
            if self.detector.box_format not in ("auto", "xywh", "xyxy"):
                problems.append(
                    f"detector.box_format={self.detector.box_format!r} 非法，"
                    f"只能是 auto / xywh / xyxy"
                )
            if self.detector.model_path:
                from pathlib import Path as _Path

                if not _Path(self.detector.model_path).exists():
                    problems.append(
                        f"detector.model_path 指向的文件不存在："
                        f"{self.detector.model_path}"
                    )
        elif engine == "hsv":
            try:
                ranges = self.detector.build_ranges()
                for hsv_range in ranges:
                    lower, upper = hsv_range.lower, hsv_range.upper
                    if lower[0] > upper[0]:
                        problems.append(
                            f"HSV 区间 Hue 下界大于上界：{lower} > {upper}。"
                            f"红色需拆成两段，不能写成 170..10"
                        )
            except (ValueError, KeyError) as exc:
                problems.append(f"检测颜色配置有误：{exc}")

        if self.detector.min_area <= 0:
            problems.append("detector.min_area 必须为正数")

        if self.mapping.roi_w <= 0 or self.mapping.roi_h <= 0:
            problems.append("mapping.roi_w / roi_h 必须为正数")

        roi = self.mapping.build_roi()
        if roi.x + roi.w > self.camera.width or roi.y + roi.h > self.camera.height:
            problems.append(
                f"ROI ({roi.x},{roi.y},{roi.w},{roi.h}) 超出摄像头分辨率 "
                f"{self.camera.width}x{self.camera.height}"
            )

        if self.mapping.fixed_y is not None:
            if not (CENTER_Y_MIN <= self.mapping.fixed_y <= CENTER_Y_MAX):
                problems.append(
                    f"mapping.fixed_y={self.mapping.fixed_y} 超出有效范围 "
                    f"{CENTER_Y_MIN}..{CENTER_Y_MAX}"
                )

        if self.mapping.smoothing is not None and not (
            0.0 < float(self.mapping.smoothing) <= 1.0
        ):
            problems.append("mapping.smoothing 必须落在 (0, 1]，或设为 null 关闭")

        if self.debug.ui_layout not in ("overlay", "stacked"):
            problems.append(
                f"debug.ui_layout={self.debug.ui_layout!r} 非法，"
                f"只能是 overlay 或 stacked"
            )

        if self.loop.target_fps <= 0:
            problems.append("loop.target_fps 必须为正数")

        if self.serial.baudrate not in (
            9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600
        ):
            problems.append(
                f"serial.baudrate={self.serial.baudrate} 不是常见值，"
                f"请确认 STM32 侧配置一致"
            )

        if self.control_type not in (
            int(ControlType.SERIAL_AND_KEYS), int(ControlType.KEYS_ONLY)
        ):
            problems.append(
                f"control_type={self.control_type} 非法，只能是 1 或 2"
            )

        return problems

    def describe(self) -> str:
        """生成配置摘要，启动时打印，便于确认参数是否符合预期。"""
        # 检测引擎摘要
        try:
            engine = self.detector.engine_normalized
        except ValueError:
            engine = "非法"

        if engine == "yolo":
            model = self.detector.model_path or "(自动定位 best.rknn)"
            detect_desc = [
                f"  引擎=YOLO/RKNN  conf={self.detector.conf_threshold}  "
                f"nms={self.detector.nms_threshold}",
                f"  模型={model}  输入={self.detector.input_size}  "
                f"NPU核掩码={self.detector.core_mask}",
                f"  框格式={self.detector.box_format}  "
                f"最大目标数={self.detector.max_blocks}",
            ]
        elif engine == "hsv":
            try:
                color_desc = f"{self.detector.preset}"
                if self.detector.preset.lower() == "custom":
                    ranges = self.detector.build_ranges()
                    color_desc = f"custom({len(ranges)}段)"
            except Exception:
                color_desc = f"{self.detector.preset}(解析失败)"
            detect_desc = [
                f"  引擎=HSV  颜色={color_desc}  "
                f"最小面积={self.detector.min_area}",
            ]
        else:
            detect_desc = [f"  引擎配置非法：{self.detector.engine!r}"]

        lines = [
            "── 摄像头 ──",
            f"  /dev/video{self.camera.index}  "
            f"{self.camera.width}x{self.camera.height}@{self.camera.fps}  "
            f"{self.camera.fourcc}",
            "── 检测 ──",
            *detect_desc,
            "── 映射 ──",
            f"  ROI=({self.mapping.roi_x},{self.mapping.roi_y},"
            f"{self.mapping.roi_w},{self.mapping.roi_h})  "
            f"镜像X={self.mapping.invert_x} 镜像Y={self.mapping.invert_y}  "
            f"XY交换={self.mapping.swap_xy}",
            f"  固定Y={self.mapping.fixed_y}  "
            f"平滑={self.mapping.smoothing}  死区={self.mapping.deadband}",
            f"  输出范围 X {CENTER_X_MIN}..{CENTER_X_MAX}  "
            f"Y {CENTER_Y_MIN}..{CENTER_Y_MAX}",
            "── 串口 ──",
            f"  {self.serial.port} @ {self.serial.baudrate}  "
            f"超时={self.serial.timeout}s",
            "── 循环 ──",
            f"  目标 {self.loop.target_fps}fps  "
            f"丢失阈值 {self.loop.lost_frames} 帧",
        ]
        return "\n".join(lines)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
