"""色块检测 —— HSV 阈值分割 + 形态学去噪 + 质心定位。

为什么用 HSV 而不是 RGB？
    RGB 三个通道都随光照强度变化，阈值很难定；HSV 把"色调"独立成 H 通道，
    同一个色块在亮处和暗处的 H 值几乎不变，只在 V（亮度）上有差异。
    这对摄像头自动曝光变化剧烈的场景至关重要。

为什么用图像矩算质心而不是外接框中心？
    外接框中心（bbox center）对形状敏感：色块被遮挡一角、或被形态学削掉
    一块时，bbox 会被拉偏。而零阶/一阶图像矩算出的质心是"面积加权中心"，
    部分遮挡时仍然稳定。对追踪类应用这是明显更优的选择。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────
# HSV 颜色范围
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HsvRange:
    """一段 HSV 阈值区间（OpenCV 口径：H 0..179, S 0..255, V 0..255）。"""

    lower: tuple[int, int, int]
    upper: tuple[int, int, int]

    def as_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return (
            np.array(self.lower, dtype=np.uint8),
            np.array(self.upper, dtype=np.uint8),
        )

    def to_dict(self) -> dict[str, list[int]]:
        return {"lower": list(self.lower), "upper": list(self.upper)}

    @classmethod
    def from_dict(cls, data: dict) -> "HsvRange":
        return cls(
            lower=tuple(int(v) for v in data["lower"]),   # type: ignore[arg-type]
            upper=tuple(int(v) for v in data["upper"]),   # type: ignore[arg-type]
        )


#: 预设色。红色跨越 H=0/180 边界，因此拆成两段 —— 这是 HSV 分割最经典的坑。
PRESET_COLORS: dict[str, list[HsvRange]] = {
    "red": [
        HsvRange((0, 70, 60), (10, 255, 255)),
        HsvRange((170, 70, 60), (180, 255, 255)),
    ],
    "orange": [HsvRange((10, 80, 80), (22, 255, 255))],
    "yellow": [HsvRange((22, 80, 80), (35, 255, 255))],
    "green": [HsvRange((40, 60, 50), (85, 255, 255))],
    "cyan": [HsvRange((85, 60, 50), (98, 255, 255))],
    "blue": [HsvRange((98, 60, 50), (130, 255, 255))],
    "purple": [HsvRange((130, 60, 50), (155, 255, 255))],
    "magenta": [HsvRange((155, 60, 50), (170, 255, 255))],
}

#: 预设色的中文名，供 CLI 提示与文档使用
PRESET_NAMES_ZH: dict[str, str] = {
    "red": "红色",
    "orange": "橙色",
    "yellow": "黄色",
    "green": "绿色",
    "cyan": "青色",
    "blue": "蓝色",
    "purple": "紫色",
    "magenta": "品红",
}


def preset_ranges(name: str) -> list[HsvRange]:
    """取预设颜色对应的 HSV 区间列表。"""
    key = name.strip().lower()
    if key not in PRESET_COLORS:
        known = ", ".join(sorted(PRESET_COLORS))
        raise KeyError(f"未知预设色 {name!r}，可选：{known}")
    return PRESET_COLORS[key]


# ──────────────────────────────────────────────────────────────────────
# 检测结果
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Detection:
    """单个色块/目标的检测结果。

    这是两种检测器（HSV 与 YOLO）共用的统一接口 —— 流水线只认这个结构，
    因此换检测算法不需要动流水线一行代码。
    """

    center: tuple[float, float]
    """**二维中心坐标** (x, y)，浮点，单位：图像像素。
    这就是最终要回传给 STM32 的那个值。"""

    bbox: tuple[int, int, int, int]
    """外接矩形，格式为 **(x, y, w, h)** —— 左上角坐标 + 宽高。

    ⚠️ 注意不是 (x1,y1,x2,y2)！这两种约定混用会造成极隐蔽的 bug：
    把 (x,y,w,h) 当成 (x1,y1,x2,y2) 用时，会得到 x2<x1 的非法框 ——
    IoU 恒为 0，画出来则是一个横跨两个角的大矩形。
    需要 xyxy 时请用 :attr:`bbox_xyxy`，不要手工换算。
    """

    area: float
    """面积（像素²）。HSV 是轮廓面积；YOLO 是外接框面积。"""

    contour: np.ndarray = field(repr=False)
    """轮廓点集，供可视化使用。YOLO 会用外接框合成一个矩形轮廓。"""

    score: float | None = None
    """置信度（0~1）。YOLO 会填真实置信度；HSV 无此概念，为 None。"""

    @property
    def bbox_xyxy(self) -> tuple[int, int, int, int]:
        """外接矩形，格式为 (x1, y1, x2, y2)。"""
        x, y, w, h = self.bbox
        return (x, y, x + w, y + h)

    @property
    def bbox_center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return (x + w / 2.0, y + h / 2.0)

    @property
    def center_int(self) -> tuple[int, int]:
        return (int(round(self.center[0])), int(round(self.center[1])))


# ──────────────────────────────────────────────────────────────────────
# 检测器
# ──────────────────────────────────────────────────────────────────────


class ColorDetector:
    """HSV 色块检测器。

    典型用法::

        detector = ColorDetector.from_preset("red", min_area=400)
        result = detector.detect(frame.image)
        if result is not None:
            print(result.center)
    """

    def __init__(
        self,
        ranges: Sequence[HsvRange],
        min_area: float = 300.0,
        max_area: float | None = None,
        blur_size: int = 5,
        morph_kernel: int = 5,
        morph_iterations: int = 2,
        roi: tuple[int, int, int, int] | None = None,
        max_blocks: int = 1,
    ) -> None:
        """
        参数
        ----
        ranges          : HSV 区间列表（红色这类需要两段）
        min_area        : 小于此面积的轮廓直接忽略，用于滤掉噪点
        max_area        : 大于此面积忽略；None 表示不限制（可用于排除大面积背景）
        blur_size       : 高斯模糊核，必须是奇数；<=1 表示不模糊
        morph_kernel    : 形态学核大小，必须是奇数
        morph_iterations: 开运算迭代次数，越大去噪越狠但小色块也会被吃掉
        roi             : 只在该区域 (x, y, w, h) 内检测；None 表示全图
        max_blocks      : 最多返回几个色块
        """
        if not ranges:
            raise ValueError("至少需要一段 HSV 区间")

        self.ranges = list(ranges)
        self.min_area = float(min_area)
        self.max_area = None if max_area is None else float(max_area)
        self.blur_size = self._odd(blur_size)
        self.morph_kernel = self._odd(morph_kernel)
        self.morph_iterations = max(0, int(morph_iterations))
        self.roi = roi
        self.max_blocks = max(1, int(max_blocks))
        # 与 CardDetector 保持一致的接口状态。HSV 检测器虽然不需要
        # 初始化资源，但 detect() 前不要求显式 open()，所以默认视为已就绪。
        self._opened = True

    @staticmethod
    def _odd(value: int) -> int:
        """把核大小规整成合法的奇数。"""
        value = int(value)
        if value <= 1:
            return 1
        return value if value % 2 == 1 else value + 1

    @classmethod
    def from_preset(cls, name: str, **kwargs) -> "ColorDetector":
        """用预设颜色构造检测器。"""
        return cls(preset_ranges(name), **kwargs)

    # ── 生命周期 ────────────────────────────────────────────────────
    #
    # HSV 检测器是纯函数式的，没有需要初始化的资源。但为了与
    # :class:`src.card_detector.CardDetector`（它要载模型、占 NPU）
    # **接口完全一致**，这里提供同样名字的空实现。
    #
    # 这样调用方（流水线、评估脚本）就能无差别地写：
    #     detector.open()
    #     ...
    #     detector.close()
    # 而不需要到处 hasattr 判断用的是哪种检测器。

    def open(self) -> "ColorDetector":
        """空实现 —— HSV 检测器无需初始化。"""
        self._opened = True
        return self

    def close(self) -> None:
        """空实现 —— HSV 检测器没有需要释放的资源。"""
        self._opened = False

    @property
    def is_open(self) -> bool:
        return self._opened

    def __enter__(self) -> "ColorDetector":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── 内部：分割 ──────────────────────────────────────────────────

    def make_mask(self, image: np.ndarray) -> np.ndarray:
        """生成二值掩膜（不做 ROI 裁剪）。"""
        if self.blur_size > 1:
            blurred = cv2.GaussianBlur(
                image, (self.blur_size, self.blur_size), 0
            )
        else:
            blurred = image

        hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)

        mask = None
        for hsv_range in self.ranges:
            lower, upper = hsv_range.as_arrays()
            part = cv2.inRange(hsv, lower, upper)
            mask = part if mask is None else cv2.bitwise_or(mask, part)

        assert mask is not None  # ranges 非空已保证

        if self.morph_kernel > 1 and self.morph_iterations > 0:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (self.morph_kernel, self.morph_kernel),
            )
            # 先开后闭：开运算去掉散点，闭运算填补色块内部的小孔
            mask = cv2.morphologyEx(
                mask, cv2.MORPH_OPEN, kernel,
                iterations=self.morph_iterations,
            )
            mask = cv2.morphologyEx(
                mask, cv2.MORPH_CLOSE, kernel,
                iterations=self.morph_iterations,
            )
        return mask

    # ── 对外：检测 ──────────────────────────────────────────────────

    def detect_all(self, image: np.ndarray) -> list[Detection]:
        """返回画面中所有满足条件的色块，按面积从大到小排序。"""
        if image is None or image.size == 0:
            return []

        offset_x, offset_y = 0, 0
        working = image

        if self.roi is not None:
            rx, ry, rw, rh = self.roi
            height, width = image.shape[:2]
            rx = max(0, min(int(rx), width - 1))
            ry = max(0, min(int(ry), height - 1))
            rw = max(1, min(int(rw), width - rx))
            rh = max(1, min(int(rh), height - ry))
            working = image[ry:ry + rh, rx:rx + rw]
            offset_x, offset_y = rx, ry

        mask = self.make_mask(working)
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        detections: list[Detection] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self.min_area:
                continue
            if self.max_area is not None and area > self.max_area:
                continue

            center = self._centroid(contour)
            if center is None:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            detections.append(
                Detection(
                    # 把 ROI 内的坐标换算回整图坐标
                    center=(center[0] + offset_x, center[1] + offset_y),
                    bbox=(x + offset_x, y + offset_y, w, h),
                    area=area,
                    contour=contour + np.array([offset_x, offset_y]),
                )
            )

        detections.sort(key=lambda d: d.area, reverse=True)
        return detections[: self.max_blocks]

    def detect(self, image: np.ndarray) -> Detection | None:
        """返回面积最大的色块；没找到返回 None。"""
        found = self.detect_all(image)
        return found[0] if found else None

    @staticmethod
    def _centroid(contour: np.ndarray) -> tuple[float, float] | None:
        """用图像矩计算质心；退化情况回退到外接框中心。"""
        moments = cv2.moments(contour)
        m00 = moments.get("m00", 0.0)
        if m00 > 1e-6:
            return (
                float(moments["m10"] / m00),
                float(moments["m01"] / m00),
            )
        # 面积为零的退化轮廓：用点集均值兜底
        points = contour.reshape(-1, 2)
        if len(points) == 0:
            return None
        return (float(points[:, 0].mean()), float(points[:, 1].mean()))

    # ── 可视化 ──────────────────────────────────────────────────────

    def draw(
        self,
        image: np.ndarray,
        detections: Iterable[Detection],
        color: tuple[int, int, int] = (0, 255, 0),
        label_prefix: str = "",
    ) -> np.ndarray:
        """在图像副本上画出检测结果（外接框 + 质心十字）。"""
        canvas = image.copy()
        for index, det in enumerate(detections):
            x, y, w, h = det.bbox
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)

            cx, cy = det.center_int
            cv2.drawMarker(
                canvas, (cx, cy), (0, 0, 255),
                markerType=cv2.MARKER_CROSS, markerSize=18, thickness=2,
            )
            cv2.circle(canvas, (cx, cy), 3, (255, 255, 255), -1)

            label = f"{label_prefix}{index}: ({cx}, {cy}) a={det.area:.0f}"
            cv2.putText(
                canvas, label, (x, max(20, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA,
            )
        return canvas

    def draw_roi(self, image: np.ndarray,
                 color: tuple[int, int, int] = (255, 200, 0)) -> np.ndarray:
        """画出检测 ROI 范围，便于标定。"""
        canvas = image.copy()
        if self.roi is not None:
            x, y, w, h = self.roi
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                canvas, "ROI", (x + 6, y + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA,
            )
        return canvas


# ──────────────────────────────────────────────────────────────────────
# 取色标定辅助
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SampledColor:
    """从图像某区域采样得到的 HSV 统计。"""

    center_hsv: tuple[float, float, float]
    lower: tuple[int, int, int]
    upper: tuple[int, int, int]
    sample_count: int
    ranges: list[HsvRange]

    def describe(self) -> str:
        h, s, v = self.center_hsv
        lines = [
            f"采样像素数：{self.sample_count}",
            f"中心 HSV ：H={h:.1f}  S={s:.1f}  V={v:.1f}",
            f"建议区间 ：lower={self.lower}  upper={self.upper}",
        ]
        if len(self.ranges) > 1:
            lines.append("（检测到该色跨越 H=0/180 边界，已自动拆成两段）")
        return "\n".join(lines)


def sample_region_color(
    image: np.ndarray,
    x: int,
    y: int,
    radius: int = 15,
    h_margin: int = 8,
    s_margin: int = 60,
    v_margin: int = 60,
    saturation_floor: int = 60,
) -> SampledColor:
    """采样图像中某个圆形区域，自动推导 HSV 阈值区间。

    这是"取色标定"的核心：用户只需指出色块在画面里的大致位置，
    程序统计该区域像素的 HSV 分布，用百分位数 + 余量生成阈值。

    参数
    ----
    h_margin / s_margin / v_margin : 在百分位区间外再放宽多少
    saturation_floor               : 低于此饱和度的像素视为灰/白，不参与统计
    """
    height, width = image.shape[:2]
    x0, x1 = max(0, x - radius), min(width, x + radius + 1)
    y0, y1 = max(0, y - radius), min(height, y + radius + 1)

    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"采样区域 ({x},{y},r={radius}) 完全在画面之外")

    patch = image[y0:y1, x0:x1]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3)

    # 过滤低饱和度像素（背景白纸/灰桌面），只保留真正的彩色像素
    colored = hsv[hsv[:, 1] >= saturation_floor]
    if len(colored) == 0:
        raise ValueError(
            f"采样区域内没有足够饱和的彩色像素（S >= {saturation_floor}）。"
            f"请确认采样点落在色块上，而不是背景。"
        )

    hues = colored[:, 0].astype(np.float64)
    sats = colored[:, 1].astype(np.float64)
    vals = colored[:, 2].astype(np.float64)

    # 用百分位而非 min/max：避免个别反光/阴影像素把区间撑得过大
    h_low, h_high = np.percentile(hues, [5, 95])
    s_low, s_high = np.percentile(sats, [5, 95])
    v_low, v_high = np.percentile(vals, [5, 95])

    h_center = float(np.median(hues))

    def clamp(value: float, low: int, high: int) -> int:
        return int(max(low, min(high, round(value))))

    # 红色特殊性处理：H 分布在 0 或 180 附近时，直接算 min/max 会得到
    # 一个横跨整个 H 轴的错误区间，必须拆成两段。
    near_zero = (hues <= 15).sum()
    near_max = (hues >= 165).sum()
    wraps = near_zero > 0 and near_max > 0

    if wraps:
        ranges = [
            HsvRange(
                (0,
                 clamp(s_low - s_margin, 0, 255),
                 clamp(v_low - v_margin, 0, 255)),
                (clamp(h_high if h_high <= 15 else 15, 0, 179),
                 255, 255),
            ),
            HsvRange(
                (clamp(h_low if h_low >= 165 else 165, 0, 179),
                 clamp(s_low - s_margin, 0, 255),
                 clamp(v_low - v_margin, 0, 255)),
                (179, 255, 255),
            ),
        ]
        lower = ranges[0].lower
        upper = ranges[0].upper
    else:
        single = HsvRange(
            (
                clamp(h_low - h_margin, 0, 179),
                clamp(s_low - s_margin, 0, 255),
                clamp(v_low - v_margin, 0, 255),
            ),
            (
                clamp(h_high + h_margin, 0, 179),
                clamp(s_high + s_margin, 0, 255),
                clamp(v_high + v_margin, 0, 255),
            ),
        )
        ranges = [single]
        lower = single.lower
        upper = single.upper

    return SampledColor(
        center_hsv=(
            h_center,
            float(np.median(sats)),
            float(np.median(vals)),
        ),
        lower=lower,
        upper=upper,
        sample_count=int(len(colored)),
        ranges=ranges,
    )


def dominant_colored_pixel(image: np.ndarray,
                           saturation_floor: int = 80) -> tuple[int, int] | None:
    """在没有指定采样点时，自动找出画面中"最像彩色物体"的像素坐标。

    策略：对饱和度做阈值，取面积最大的连通域，返回其质心。
    用于标定脚本的 ``--auto`` 模式，省去用户手点坐标。
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array((0, saturation_floor, 40), dtype=np.uint8),
        np.array((179, 255, 255), dtype=np.uint8),
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 50:
        return None

    center = ColorDetector._centroid(largest)
    if center is None:
        return None
    return (int(round(center[0])), int(round(center[1])))
