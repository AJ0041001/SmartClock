"""坐标映射 —— 摄像头像素坐标 → SmartClock 游戏坐标。

坐标系统梳理
------------
游戏侧（依据 Docs/视觉坐标与串口协议.md）：

    屏幕逻辑尺寸            480 x 800
    游戏白色外框            430 x 590，左上角 y=78，边框 3px
    游戏内部有效区域        424 x 584
    内部坐标原点            白框内侧左上角 (0, 0)，+X 向右，+Y 向下
    挡板尺寸                70 x 13
    挡板中心有效范围        X: 35..389    Y: 6..578
    ─────────────────────────────────────────────────────
    视觉端发送的就是"挡板中心坐标"。

摄像头侧：

    图像分辨率由 USB 摄像头决定（默认 640x480），原点在左上角，
    +X 向右，+Y 向下 —— 与游戏侧方向一致，所以默认无需翻转。

映射策略
--------
在摄像头画面里框出一个矩形 ROI，把它**线性拉伸**到挡板中心的有效范围。
    · 色块位于 ROI 左边缘  → 挡板中心 X = 35（挡板贴左墙）
    · 色块位于 ROI 右边缘  → 挡板中心 X = 389（挡板贴右墙）
这样 ROI 就是"物理可动范围"，语义直观，标定时也好操作。

摄像头摆放方向不确定的问题，通过 invert_x / invert_y / swap_xy 三个开关解决。
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import (
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
    INNER_H,
    INNER_W,
    clamp_to_center_range,
)


# ──────────────────────────────────────────────────────────────────────
# ROI
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Roi:
    """摄像头画面中的一个矩形感兴趣区域。"""

    x: int
    y: int
    w: int
    h: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, data: dict) -> "Roi":
        return cls(
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            w=int(data.get("w", 640)),
            h=int(data.get("h", 480)),
        )

    @classmethod
    def full_frame(cls, width: int, height: int) -> "Roi":
        return cls(0, 0, width, height)

    def clamp_to(self, width: int, height: int) -> "Roi":
        """把 ROI 收进画面范围内，防止配置写错导致越界。"""
        x = max(0, min(self.x, max(0, width - 1)))
        y = max(0, min(self.y, max(0, height - 1)))
        w = max(1, min(self.w, width - x))
        h = max(1, min(self.h, height - y))
        return Roi(x, y, w, h)


# ──────────────────────────────────────────────────────────────────────
# 平滑器与死区
# ──────────────────────────────────────────────────────────────────────


class EmaSmoother:
    """指数移动平均（EMA）平滑器。

    摄像头检测必然有像素级抖动，直接转发会让挡板"哆嗦"。
    EMA 用极低的计算代价换到明显更稳的坐标：

        out = alpha * new + (1 - alpha) * prev

    alpha 越大越跟手但越抖；越小越平滑但越滞后。
    追踪场景一般 0.3~0.5 比较合适。
    """

    def __init__(self, alpha: float = 0.35) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha 必须落在 (0, 1]，收到 {alpha}")
        self.alpha = float(alpha)
        self._value: tuple[float, float] | None = None

    def reset(self) -> None:
        self._value = None

    @property
    def value(self) -> tuple[float, float] | None:
        return self._value

    def update(self, x: float, y: float) -> tuple[float, float]:
        if self._value is None:
            self._value = (float(x), float(y))
        else:
            px, py = self._value
            self._value = (
                self.alpha * float(x) + (1.0 - self.alpha) * px,
                self.alpha * float(y) + (1.0 - self.alpha) * py,
            )
        return self._value


class Deadband:
    """死区滤波器：目标移动小于阈值时视为静止。

    抖动通常只有 ±1~2 像素，而死区能把这些微小变化彻底抹平，
    让挡板完全静止 —— 比单纯依赖 EMA 更有效。
    """

    def __init__(self, threshold: float = 2.0) -> None:
        self.threshold = float(threshold)
        self._value: tuple[float, float] | None = None

    def reset(self) -> None:
        self._value = None

    def update(self, x: float, y: float) -> tuple[float, float]:
        if self._value is None:
            self._value = (float(x), float(y))
            return self._value

        px, py = self._value
        nx = px if abs(x - px) < self.threshold else float(x)
        ny = py if abs(y - py) < self.threshold else float(y)
        self._value = (nx, ny)
        return self._value


# ──────────────────────────────────────────────────────────────────────
# 映射器
# ──────────────────────────────────────────────────────────────────────


@dataclass
class MappingResult:
    """一次映射的完整结果，便于日志与调试。"""

    raw_x: float
    raw_y: float
    normalized_x: float
    normalized_y: float
    game_x: int
    game_y: int
    smoothed_x: float
    smoothed_y: float


class CoordinateMapper:
    """摄像头坐标 → 游戏挡板中心坐标。

    参数
    ----
    roi       : 摄像头画面里的有效运动范围
    invert_x  : 水平镜像（摄像头画面左右反了时打开）
    invert_y  : 垂直镜像（摄像头上下反了时打开）
    swap_xy   : 交换 XY 轴（摄像头旋转了 90° 时打开）
    fixed_y   : 若设置，则忽略摄像头 Y 值，始终使用该游戏 Y 坐标。
                挡板如果是纯水平移动的横条（70x13），通常需要固定 Y。
    smoothing : EMA 系数，None 表示不平滑
    deadband  : 死区阈值（游戏像素），0 表示关闭
    """

    def __init__(
        self,
        roi: Roi | None = None,
        invert_x: bool = False,
        invert_y: bool = False,
        swap_xy: bool = False,
        fixed_y: int | None = None,
        smoothing: float | None = 0.35,
        deadband: float = 2.0,
    ) -> None:
        self.roi = roi or Roi(0, 0, 640, 480)
        self.invert_x = bool(invert_x)
        self.invert_y = bool(invert_y)
        self.swap_xy = bool(swap_xy)
        self.fixed_y = None if fixed_y is None else int(fixed_y)

        self.smoother = EmaSmoother(smoothing) if smoothing else None
        self.deadband = Deadband(deadband) if deadband > 0 else None

    # ── 标定辅助 ────────────────────────────────────────────────────

    def bind_frame(self, width: int, height: int) -> None:
        """在拿到实际分辨率后修正 ROI，避免配置与实际不符。"""
        self.roi = self.roi.clamp_to(width, height)

    def reset(self) -> None:
        """清空平滑状态。检测丢失后重新捕获时应调用。"""
        if self.smoother:
            self.smoother.reset()
        if self.deadband:
            self.deadband.reset()

    # ── 核心映射 ────────────────────────────────────────────────────

    def _normalize(self, value: float, start: int, span: int) -> float:
        """把像素值归一化到 [0, 1]，越界值截断。"""
        if span <= 0:
            return 0.0
        ratio = (value - start) / float(span)
        return max(0.0, min(1.0, ratio))

    def map_point(self, cam_x: float, cam_y: float) -> MappingResult:
        """把摄像头坐标映射到游戏坐标（含平滑与死区）。"""
        src_x, src_y = float(cam_x), float(cam_y)

        # 1) 轴交换（摄像头旋转 90° 的情形）
        if self.swap_xy:
            src_x, src_y = src_y, src_x

        # 2) 归一化到 [0, 1]
        nx = self._normalize(src_x, self.roi.x, self.roi.w)
        ny = self._normalize(src_y, self.roi.y, self.roi.h)

        # 3) 镜像
        if self.invert_x:
            nx = 1.0 - nx
        if self.invert_y:
            ny = 1.0 - ny

        # 4) 映射到游戏坐标系
        #
        # 说明：ROI 的边缘对应"挡板中心的极限位置"，而不是游戏内区的边缘。
        # 因为协议里视觉端发送的就是中心坐标，其有效范围天然比内区小一圈
        # （X 少 35，Y 少 6），这个内缩正是挡板自身宽度/高度的一半。
        game_x = CENTER_X_MIN + nx * (CENTER_X_MAX - CENTER_X_MIN)

        if self.fixed_y is not None:
            game_y = float(self.fixed_y)
        else:
            game_y = CENTER_Y_MIN + ny * (CENTER_Y_MAX - CENTER_Y_MIN)

        # 5) 平滑
        if self.smoother is not None:
            sx, sy = self.smoother.update(game_x, game_y)
        else:
            sx, sy = game_x, game_y

        # 6) 死区
        if self.deadband is not None:
            dx, dy = self.deadband.update(sx, sy)
        else:
            dx, dy = sx, sy

        # 7) 取整并限幅，确保落在单片机可接受范围内
        ix, iy = clamp_to_center_range(int(round(dx)), int(round(dy)))

        return MappingResult(
            raw_x=float(cam_x),
            raw_y=float(cam_y),
            normalized_x=nx,
            normalized_y=ny,
            game_x=ix,
            game_y=iy,
            smoothed_x=sx,
            smoothed_y=sy,
        )

    def describe(self) -> str:
        """生成映射配置的可读描述，便于写进日志。"""
        flags = []
        if self.invert_x:
            flags.append("水平镜像")
        if self.invert_y:
            flags.append("垂直镜像")
        if self.swap_xy:
            flags.append("XY交换")
        flag_text = "、".join(flags) if flags else "无"

        lines = [
            f"ROI          : x={self.roi.x} y={self.roi.y} "
            f"w={self.roi.w} h={self.roi.h}",
            f"变换         : {flag_text}",
            f"固定 Y       : {self.fixed_y if self.fixed_y is not None else '跟随'}",
            f"平滑 alpha   : {self.smoother.alpha if self.smoother else '关闭'}",
            f"死区阈值     : {self.deadband.threshold if self.deadband else '关闭'}",
            f"输出范围     : X {CENTER_X_MIN}..{CENTER_X_MAX}  "
            f"Y {CENTER_Y_MIN}..{CENTER_Y_MAX}",
        ]
        return "\n".join(lines)


def percent_to_game_x(percent: float) -> int:
    """便捷函数：把 0~100 的百分比直接换算成游戏 X 坐标。"""
    ratio = max(0.0, min(100.0, float(percent))) / 100.0
    return int(round(CENTER_X_MIN + ratio * (CENTER_X_MAX - CENTER_X_MIN)))


def percent_to_game_y(percent: float) -> int:
    """便捷函数：把 0~100 的百分比直接换算成游戏 Y 坐标。"""
    ratio = max(0.0, min(100.0, float(percent))) / 100.0
    return int(round(CENTER_Y_MIN + ratio * (CENTER_Y_MAX - CENTER_Y_MIN)))


# 供文档引用：内区尺寸（当前未被映射直接使用，但保留以免调用方重复定义）
GAME_INNER_SIZE = (INNER_W, INNER_H)
