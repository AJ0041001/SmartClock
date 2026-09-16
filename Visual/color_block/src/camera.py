"""USB 摄像头采集封装（V4L2 / UVC）。

对应硬件：彩钻风 USB 摄像头 → 标准 UVC 设备 → ``/dev/videoN``。

设计要点
--------
1. **强制 V4L2 后端**：OpenCV 在 Linux 上可能默认走 GStreamer，
   而板子上 GStreamer 插件未必齐全。显式指定 ``cv2.CAP_V4L2`` 最稳。
2. **优先 MJPG**：USB 2.0 带宽有限，640x480 以上分辨率用 YUYV 会被
   卡在 5~10fps；MJPG 是压缩流，能轻松跑到 30fps。
3. **缓冲置 1**：``CAP_PROP_BUFFERSIZE=1`` 只保留最新帧，
   避免"画面滞后好几秒"的经典问题。
4. **预热丢帧**：刚打开的前若干帧常是自动曝光未收敛的花屏，丢弃。
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────
# 设备枚举
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VideoDeviceInfo:
    """一个 V4L2 视频设备的描述。"""

    index: int
    device: str
    name: str = ""
    is_capture: bool = True


def _fourcc_to_str(value: float) -> str:
    """V4L2 的四字符编码整数转可读字符串。"""
    code = int(value)
    if code <= 0:
        return "????"
    return "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))


def list_video_devices() -> list[VideoDeviceInfo]:
    """从 sysfs 枚举 V4L2 设备。

    刻意读 sysfs 而不是只 glob ``/dev/video*``：sysfs 里能拿到
    ``name``（例如 "USB Camera: USB Camera"），便于确认插的是哪个摄像头。
    同时能识别出 UVC 常见的"一个摄像头暴露两个 node"现象。
    """
    devices: list[VideoDeviceInfo] = []

    for node in sorted(glob.glob("/sys/class/video4linux/video*")):
        base = os.path.basename(node)
        try:
            index = int(base.replace("video", ""))
        except ValueError:
            continue

        name = ""
        try:
            with open(os.path.join(node, "name"), "r",
                      encoding="utf-8", errors="replace") as handle:
                name = handle.read().strip()
        except OSError:
            pass

        # sysfs 里没有 devices 目录的通常是 metadata 节点，不是真正的采集口
        is_capture = os.path.exists(os.path.join(node, "device"))

        devices.append(
            VideoDeviceInfo(
                index=index,
                device=f"/dev/{base}",
                name=name,
                is_capture=is_capture,
            )
        )

    if not devices:
        # sysfs 不可用时退回 glob /dev
        for path in sorted(glob.glob("/dev/video*")):
            base = os.path.basename(path)
            try:
                index = int(base.replace("video", ""))
            except ValueError:
                continue
            devices.append(VideoDeviceInfo(index=index, device=path))

    return devices


def describe_environment() -> str:
    """生成一段可读的摄像头环境报告。"""
    devices = list_video_devices()
    if not devices:
        return (
            "未发现任何视频设备。\n"
            "  · 请确认 USB 摄像头已插好（插上后内核会自动创建 /dev/videoN）\n"
            "  · 可以用 `lsusb` 看摄像头有没有出现在 USB 总线上\n"
            "  · 可以用 `v4l2-ctl --list-devices` 交叉验证"
        )
    lines = [f"发现 {len(devices)} 个视频节点："]
    for info in devices:
        tag = "采集" if info.is_capture else "元数据"
        name = info.name or "(无名称)"
        lines.append(f"  · {info.device}  [{tag}]  {name}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# 采集器
# ──────────────────────────────────────────────────────────────────────


class CameraError(RuntimeError):
    """摄像头打开或读取失败。"""


@dataclass
class Frame:
    """一帧图像及其元信息。"""

    image: np.ndarray
    index: int
    timestamp: float

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


class UsbCamera:
    """USB（UVC）摄像头采集器。

    典型用法::

        with UsbCamera(index=0, width=640, height=480) as cam:
            frame = cam.read()
            if frame is not None:
                ...  # frame.image 是 BGR ndarray
    """

    # 打开后丢弃的帧数：等自动曝光/白平衡收敛
    WARMUP_FRAMES = 8

    def __init__(
        self,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        fourcc: str = "MJPG",
        buffersize: int = 1,
    ) -> None:
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.buffersize = buffersize
        self._capture: cv2.VideoCapture | None = None
        self._frame_index = 0

    # ── 生命周期 ────────────────────────────────────────────────────

    def open(self) -> "UsbCamera":
        if self._capture is not None:
            return self

        # 打开前先给出可诊断的错误信息（比 OpenCV 的静默失败友好得多）
        if not os.path.exists(f"/dev/video{self.index}"):
            available = [d.device for d in list_video_devices()]
            hint = (
                f"当前可用节点：{', '.join(available)}"
                if available
                else "当前没有任何 /dev/video* 节点，USB 摄像头可能没插好"
            )
            raise CameraError(
                f"/dev/video{self.index} 不存在。{hint}"
            )

        capture = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            raise CameraError(
                f"打开 /dev/video{self.index} 失败。"
                f"可能被其他程序占用（如另一个预览窗口 / motion / guvcview），"
                f"或该节点不是采集口。"
            )

        # 顺序有讲究：先设 FOURCC，再设分辨率。反过来某些 UVC 驱动会忽略。
        capture.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*self.fourcc[:4].ljust(4)),
        )
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, self.buffersize)

        self._capture = capture

        # 预热：把还没收敛的帧丢掉
        for _ in range(self.WARMUP_FRAMES):
            capture.read()

        return self

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self) -> "UsbCamera":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── 属性 ────────────────────────────────────────────────────────

    @property
    def actual_width(self) -> int:
        if self._capture is None:
            return 0
        return int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))

    @property
    def actual_height(self) -> int:
        if self._capture is None:
            return 0
        return int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))

    @property
    def actual_fps(self) -> float:
        if self._capture is None:
            return 0.0
        return float(self._capture.get(cv2.CAP_PROP_FPS))

    @property
    def actual_fourcc(self) -> str:
        if self._capture is None:
            return "????"
        return _fourcc_to_str(self._capture.get(cv2.CAP_PROP_FOURCC))

    def summary(self) -> str:
        return (
            f"/dev/video{self.index}  "
            f"{self.actual_width}x{self.actual_height}  "
            f"{self.actual_fps:.1f}fps  "
            f"FOURCC={self.actual_fourcc}"
        )

    # ── 读取 ────────────────────────────────────────────────────────

    def read(self, retries: int = 3) -> Frame | None:
        """抓取一帧。连续失败 ``retries`` 次则返回 None。"""
        if self._capture is None:
            raise CameraError("摄像头尚未打开，请先调用 open()")

        for _ in range(max(1, retries)):
            ok, image = self._capture.read()
            if ok and image is not None:
                self._frame_index += 1
                return Frame(
                    image=image,
                    index=self._frame_index,
                    timestamp=time.monotonic(),
                )
            # USB 偶发丢帧，稍等再试
            time.sleep(0.02)
        return None
