"""测试替身 —— 让整条流水线能在没有摄像头、没有 STM32 的机器上跑通。

这是本项目能在开发机上完成验证的基础：把不确定的物理硬件换成
完全可控的合成数据，就能对"检测→映射→组帧"整条链路做断言。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from src.camera import Frame
from src.protocol import Response


# ──────────────────────────────────────────────────────────────────────
# 合成图像
# ──────────────────────────────────────────────────────────────────────


def make_solid_background(width: int = 640, height: int = 480,
                          color: tuple[int, int, int] = (230, 230, 230)
                          ) -> np.ndarray:
    """生成一块浅灰背景（模拟白桌面/白纸）。"""
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = color
    return image


def draw_block(
    image: np.ndarray,
    center: tuple[int, int],
    size: int = 60,
    color_bgr: tuple[int, int, int] = (0, 0, 255),
    noise: float = 0.0,
) -> np.ndarray:
    """在图上画一个实心方块（默认纯红），可叠加高斯噪声。

    返回新图，不修改入参。
    """
    canvas = image.copy()
    cx, cy = int(center[0]), int(center[1])
    half = size // 2
    cv2.rectangle(
        canvas,
        (cx - half, cy - half),
        (cx + half, cy + half),
        color_bgr,
        thickness=-1,
    )
    if noise > 0:
        sigma = noise * 255.0
        jitter = np.random.default_rng(12345).normal(0, sigma, canvas.shape)
        canvas = np.clip(canvas.astype(np.float32) + jitter, 0, 255).astype(np.uint8)
    return canvas


def draw_circle_block(
    image: np.ndarray,
    center: tuple[int, int],
    radius: int = 30,
    color_bgr: tuple[int, int, int] = (0, 0, 255),
) -> np.ndarray:
    """画一个实心圆。圆形的质心可解析求解，便于精确验证。"""
    canvas = image.copy()
    cv2.circle(canvas, (int(center[0]), int(center[1])), radius, color_bgr, -1)
    return canvas


# ──────────────────────────────────────────────────────────────────────
# 假摄像头
# ──────────────────────────────────────────────────────────────────────


class FakeCapture:
    """按预设轨迹产生合成帧的"摄像头"。

    可以精确控制色块出现在画面中的位置，从而断言映射结果。
    """

    def __init__(
        self,
        positions: list[tuple[int, int] | None],
        width: int = 640,
        height: int = 480,
        block_size: int = 60,
        block_color_bgr: tuple[int, int, int] = (0, 0, 255),
        background_bgr: tuple[int, int, int] = (230, 230, 230),
        repeat_last: bool = True,
    ) -> None:
        """
        positions : 每帧色块中心；None 表示该帧没有色块（模拟丢失）
        repeat_last : 序列播完后是否重复最后一帧（模拟持续运行）
        """
        self.positions = list(positions)
        self.width = width
        self.height = height
        self.block_size = block_size
        self.block_color_bgr = block_color_bgr
        self.background_bgr = background_bgr
        self.repeat_last = repeat_last

        self._index = 0
        self.frame_index = 0
        self.opened = False
        self.closed = False

    def open(self) -> "FakeCapture":
        self.opened = True
        return self

    def close(self) -> None:
        self.closed = True

    @property
    def actual_width(self) -> int:
        return self.width

    @property
    def actual_height(self) -> int:
        return self.height

    def set_positions(self, positions: list[tuple[int, int] | None]) -> None:
        """替换轨迹（用于分阶段测试）。"""
        self.positions = list(positions)
        self._index = 0

    def read(self, retries: int = 3) -> Frame | None:
        if not self.positions:
            return None

        if self._index >= len(self.positions):
            if not self.repeat_last:
                return None
            position = self.positions[-1]
        else:
            position = self.positions[self._index]
            self._index += 1

        base = make_solid_background(self.width, self.height, self.background_bgr)
        if position is not None:
            image = draw_block(
                base, position, self.block_size, self.block_color_bgr
            )
        else:
            image = base

        self.frame_index += 1
        return Frame(image=image, index=self.frame_index, timestamp=time.monotonic())


# ──────────────────────────────────────────────────────────────────────
# 假串口
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FakeLink:
    """记录所有发出的字节，并可按脚本返回应答的"串口"。"""

    frame_log: list[bytes] = field(default_factory=list)
    response: Response = Response.OK
    write_calls: int = 0
    closed: bool = False

    #: 设为 True 时，模拟一个"从不回话"的 STM32
    silent: bool = False

    def write(self, data: bytes) -> int:
        self.write_calls += 1
        self.frame_log.append(bytes(data))
        return len(data)

    def send_frame_and_wait(self, frame: bytes, timeout: float = 0.5) -> Response:
        self.write(frame)
        if self.silent:
            return Response.TIMEOUT
        return self.response

    def close(self) -> None:
        self.closed = True

    # ── 断言辅助 ────────────────────────────────────────────────────

    def clear(self) -> None:
        self.frame_log.clear()
        self.write_calls = 0

    @property
    def last_frame(self) -> bytes | None:
        return self.frame_log[-1] if self.frame_log else None
