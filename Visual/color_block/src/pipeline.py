"""色块追踪主流水线 —— 把采集、检测、映射、串口四步串起来。

设计要点
--------
**依赖注入**：流水线不自己 new 摄像头和串口，而是接收两个对象。
这样带来两个好处：
  1. 可以在没有硬件的机器上注入"假摄像头 + 假串口"做端到端测试；
  2. 将来若换成 MIPI 摄像头或别的通信方式，只需替换其中一个对象。

数据流::

    摄像头帧 → HSV 分割 → 最大色块质心 → 坐标映射 → 10字节帧 → 串口
                    │                        │
                    └── 未检出 → 发 TYPE=02 交还按键控制

对应固件行为：STM32 收到合法帧回 ``OK\\r\\n``，非法帧回 ``ERROR\\r\\n``；
长时间收不到帧会自动回退到按键控制。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from .camera import Frame, UsbCamera
from .config import AppConfig
from .detector import ColorDetector, Detection
from .mapper import CoordinateMapper, MappingResult, Roi
from .protocol import (
    ControlType,
    Response,
    build_frame,
    build_idle_frame,
    hexdump,
)
from .serialport import SerialError, SerialPort, probe_stm32

log = logging.getLogger("color_block.pipeline")


# ──────────────────────────────────────────────────────────────────────
# 依赖接口
# ──────────────────────────────────────────────────────────────────────


class Capture(Protocol):
    """摄像头抽象。任何提供 read() 的对象都能注入。"""

    def read(self, retries: int = 3) -> Frame | None: ...


class Link(Protocol):
    """串口抽象。"""

    def write(self, data: bytes) -> int: ...

    def send_frame_and_wait(self, frame: bytes, timeout: float = 0.5) -> Response: ...


# ──────────────────────────────────────────────────────────────────────
# 统计
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Stats:
    """运行统计，用于收尾时输出报告。"""

    frames: int = 0
    detections: int = 0
    sent: int = 0
    ack_ok: int = 0
    ack_error: int = 0
    ack_timeout: int = 0
    idle_sent: int = 0
    read_failures: int = 0
    dry_run_frames: int = 0
    """dry-run 模式下"本应发出"的帧数。与 sent 区分开，避免误导。"""

    serial_errors: int = 0
    """串口异常次数（USB 转串口被拔掉、驱动掉线等）。

    这类异常以前会直接抛出 run() 把程序打死 —— 现场拔一下线就崩，
    完全不可接受。现在改成计数 + 警告，程序继续跑（拔出后自然收不到
    ACK，插回去就能恢复）。
    """

    confidence_samples: list = field(default_factory=list)
    """检出的置信度样本，用于排查"检出率为什么低"。

    这是关键诊断信息：如果平均置信度只有 0.5 出头，说明现场条件
    （光照/对焦/尺寸）与训练数据有差距；如果接近 0.9 却检测率低，
    那更可能是"卡片根本不在画面里"。
    """

    brightness_samples: list = field(default_factory=list)
    """画面亮度采样，用于排查曝光问题。"""

    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.monotonic()
        return max(1e-6, end - self.started_at)

    @property
    def fps(self) -> float:
        return self.frames / self.elapsed

    @property
    def detection_rate(self) -> float:
        return self.detections / max(1, self.frames)

    @property
    def total_sent(self) -> int:
        """实际写进串口的帧总数（数据帧 + 交还帧）。"""
        return self.sent + self.idle_sent

    @property
    def ack_rate(self) -> float:
        """应答成功率。

        ⚠️ 分母必须是 ``sent + idle_sent``，不能只用 ``sent``。
        交还帧（TYPE=02）同样会收到 OK，漏算会算出 >100% 的荒谬值。
        """
        return self.ack_ok / max(1, self.total_sent)

    @property
    def average_confidence(self) -> float:
        """平均置信度（仅统计有 score 的检测）。"""
        if not self.confidence_samples:
            return 0.0
        return sum(self.confidence_samples) / len(self.confidence_samples)

    def report(self) -> str:
        lines = [
            "─" * 58,
            "运行统计",
            "─" * 58,
            f"  运行时长      : {self.elapsed:.1f} s",
            f"  处理帧数      : {self.frames}  ({self.fps:.1f} fps)",
            f"  采集失败      : {self.read_failures}",
            f"  检出帧数      : {self.detections}  "
            f"(检出率 {self.detection_rate * 100:.1f}%)",
        ]

        # 诊断信息：检出率低时最需要看的就是这两个数
        if self.confidence_samples:
            conf = self.confidence_samples
            lines.append(
                f"  检出置信度    : 平均 {self.average_confidence:.3f}  "
                f"最低 {min(conf):.3f}  最高 {max(conf):.3f}"
            )
        if self.brightness_samples:
            bright = self.brightness_samples
            lines.append(
                f"  画面亮度      : 平均 {sum(bright) / len(bright):.1f}  "
                f"最低 {min(bright):.1f}  最高 {max(bright):.1f}"
            )

        lines.extend([
            f"  发送数据帧    : {self.sent}",
            f"  发送交还帧    : {self.idle_sent}  (TYPE=02)",
            f"  单片机回 OK   : {self.ack_ok}",
            f"  单片机回 ERROR: {self.ack_error}",
            f"  回传超时      : {self.ack_timeout}",
        ])
        if self.serial_errors:
            lines.append(
                f"  串口异常      : {self.serial_errors} 次"
                f"  ← 检查 USB 转串口是否松动/掉线"
            )
        if self.total_sent:
            lines.append(
                f"  应答成功率    : {self.ack_rate * 100:.1f}%"
                f"  ({self.ack_ok}/{self.total_sent})"
            )
        if self.dry_run_frames:
            lines.append(f"  dry-run 帧数  : {self.dry_run_frames} (未写串口)")
        lines.append("─" * 58)

        # 根据统计给出针对性建议，而不是让人自己看数字
        if self.frames >= 100 and self.detection_rate < 0.5:
            lines.append("")
            lines.append("⚠️  检出率偏低，按这个顺序排查：")
            if self.confidence_samples and self.average_confidence >= 0.8:
                lines.append("   · 平均置信度很高但检出率低 →")
                lines.append("     说明模型认得准，只是卡片不常在画面里。")
                lines.append("     检查卡片是否在摄像头视野内、ROI 是否框对。")
            elif self.confidence_samples:
                lines.append("   · 置信度也不高 → 现场条件与训练数据有差距：")
                lines.append("     查画面亮度是否偏低（对比训练均值 137）、")
                lines.append("     卡片是否太小/太远/没对上焦。")
            else:
                lines.append("   · 一直没检出过 → 卡片可能不在视野内，")
                lines.append("     或 ROI 把它排除在外了。")
            if self.brightness_samples:
                avg_b = sum(self.brightness_samples) / len(self.brightness_samples)
                if avg_b < 100:
                    lines.append(
                        f"   · 画面平均亮度只有 {avg_b:.0f}（训练数据是 137）——"
                    )
                    lines.append(
                        "     摄像头可能协商成了高帧率，曝光被压缩导致画面偏暗。"
                    )
                    lines.append(
                        "     试试在 config.yaml 里把 camera.fps 调到 30。"
                    )
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# 流水线
# ──────────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    """单帧处理结果，便于测试断言与调试输出。"""

    frame_index: int
    detected: bool
    detection: Detection | None = None
    mapping: MappingResult | None = None
    frame_bytes: bytes | None = None
    response: Response | None = None
    sent: bool = False
    sent_idle: bool = False


class ColorTrackingPipeline:
    """色块追踪流水线。"""

    #: 实时预览窗口的标题
    #:
    #: ⚠️ 必须是纯 ASCII！OpenCV 的 HighGUI 在 Linux（Qt/GTK 后端）
    #: 对非 ASCII 窗口标题支持很差 —— 用中文标题会导致窗口弹出来
    #: 但内容完全不渲染（看起来就是"空窗口"）。
    #: 这个坑很隐蔽，因为画面数据本身完全正常。
    PREVIEW_WINDOW = "SmartClock Vision"

    def __init__(
        self,
        config: AppConfig,
        capture: Capture,
        link: Link | None = None,
        dry_run: bool = False,
        config_path: str | None = None,
        enable_roi_editor: bool = True,
    ) -> None:
        self.config = config
        self.capture = capture
        self.link = link
        self.dry_run = bool(dry_run)
        self._config_path = config_path
        self._enable_roi_editor = bool(enable_roi_editor)
        self.roi_editor = None
        self._mouse_hooked = False

        self.detector = build_detector(config)
        self.mapper = CoordinateMapper(
            roi=config.mapping.build_roi(),
            invert_x=config.mapping.invert_x,
            invert_y=config.mapping.invert_y,
            swap_xy=config.mapping.swap_xy,
            fixed_y=config.mapping.fixed_y,
            smoothing=config.mapping.smoothing,
            deadband=config.mapping.deadband,
        )

        self.stats = Stats()
        self._miss_streak = 0
        self._lost = False
        self._last_send = 0.0
        self._last_idle = 0.0
        # 丢失后第一次重新检出时刻。用于给平滑器留出收敛时间，
        # 避免挡板从旧位置猛跳到新位置。None 表示当前不在重新捕获过程中。
        self._reacquire_started: float | None = None

        self._captures_dir = Path(config.debug.save_dir) / "captures"
        self._preview_available = False

    # ── 生命周期 ────────────────────────────────────────────────────

    def open(self) -> None:
        """打开摄像头与检测器。

        顺序有讲究：
          1. 先开摄像头 —— 拿到真实分辨率才能校正 ROI
          2. 再开检测器 —— YOLO 会载模型、初始化 NPU，耗时一两秒，
             且需要知道实际分辨率来决定检测 ROI
        """
        if hasattr(self.capture, "open"):
            self.capture.open()  # type: ignore[attr-defined]

        self._bind_frame_size()
        self._ensure_preview_controls()

        # 延迟到此时才占用 NPU：构造配置阶段不该把硬件占住，
        # 这样配置校验失败时不会留下占用的 NPU 上下文。
        if hasattr(self.detector, "open"):
            self.detector.open()  # type: ignore[attr-defined]

    def _bind_frame_size(self) -> None:
        """用实际分辨率校正 ROI，防止配置与摄像头不符。"""
        if not hasattr(self.capture, "actual_width"):
            return
        width = self.capture.actual_width  # type: ignore[attr-defined]
        height = self.capture.actual_height  # type: ignore[attr-defined]
        if width > 0 and height > 0:
            self.mapper.bind_frame(width, height)

    def _frame_size(self) -> tuple[int, int]:
        """实际画面尺寸（拿不到就用配置里的）。"""
        size = (self.config.camera.width, self.config.camera.height)
        if hasattr(self.capture, "actual_width"):
            width = self.capture.actual_width  # type: ignore[attr-defined]
            height = self.capture.actual_height  # type: ignore[attr-defined]
            if width > 0 and height > 0:
                size = (width, height)
        return size

    def _ensure_preview_controls(self) -> None:
        """按需创建 ROI 编辑器（**幂等**）。

        这个方法的调用点很关键，踩过一次坑：编辑器原来只在 ``open()`` 里创建，
        而 ``scripts/main.py`` 是自己开摄像头、**从不调用 ``pipeline.open()``**，
        结果窗口照常弹出来、检测也正常，唯独整个控制面板（两个 ROI、按钮、
        指引）全都不见了 —— 用户看到的就是"根本没有你说的东西"。

        所以这里把创建逻辑抽出来，``open()`` 和 ``run()`` 都调一次：
        只要进入主循环，控制面板就一定在。
        """
        if self.roi_editor is not None:
            return
        if not (self.config.debug.preview and self._enable_roi_editor):
            return

        from .roi_editor import RoiEditor

        self.roi_editor = RoiEditor(
            config=self.config,
            mapper=self.mapper,
            detector=self.detector,
            frame_size=self._frame_size(),
            # None 就交给 RoiEditor 解析默认值（可用 SMARTCLOCK_CONFIG 重定向）
            config_path=self._config_path,
        )

    def close(self) -> None:
        if hasattr(self.capture, "close"):
            self.capture.close()  # type: ignore[attr-defined]
        if self.link is not None and hasattr(self.link, "close"):
            self.link.close()  # type: ignore[attr-defined]
        # YOLO 检测器持有 NPU 上下文，必须显式释放，
        # 否则进程退出前 NPU 内存不会归还。
        if hasattr(self.detector, "close"):
            try:
                self.detector.close()  # type: ignore[attr-defined]
            except Exception as exc:  # 释放失败不该掩盖前面的错误
                log.warning("释放检测器失败：%s", exc)
        if self._preview_available:
            cv2.destroyAllWindows()
            self._preview_available = False

    # ── 单帧 ────────────────────────────────────────────────────────

    def process_frame(self, frame: Frame) -> tuple[Detection | None, MappingResult | None]:
        """只做检测 + 映射，不碰串口。方便单独测试与预览。"""
        detection = self.detector.detect(frame.image)
        if detection is None:
            return None, None
        mapping = self.mapper.map_point(*detection.center)
        return detection, mapping

    def step(self, frame: Frame) -> StepResult:
        """处理一帧并按需发送。"""
        self.stats.frames += 1
        result = StepResult(frame_index=frame.index, detected=False)

        # 采样画面亮度（每 30 帧一次就够）——
        # 摄像头若协商成高帧率，曝光时间会大幅缩短、画面偏暗，
        # 这是"静态照片能检出、实时却检不到"的常见原因。
        if self.stats.frames % 30 == 1:
            try:
                brightness = float(frame.image.mean())
                samples = self.stats.brightness_samples
                samples.append(brightness)
                if len(samples) > 200:
                    del samples[: len(samples) - 200]
            except Exception:
                pass

        detection, mapping = self.process_frame(frame)
        now = time.monotonic()

        if detection is not None and mapping is not None:
            result.detected = True
            result.detection = detection
            result.mapping = mapping

            self.stats.detections += 1
            self._miss_streak = 0

            # 记录置信度，用于排查"检出率为什么低"
            # 只保留最近 500 个样本，避免长时间运行把内存吃满
            if detection.score is not None:
                samples = self.stats.confidence_samples
                samples.append(float(detection.score))
                if len(samples) > 500:
                    del samples[: len(samples) - 500]

            if self._lost:
                # 刚重新捕获：先让平滑器收敛几帧，避免挡板从旧位置猛跳。
                # 关键：这里必须用"重新捕获的起始时刻"做基准。若用最近检出
                # 时刻，差值恒为 0，会永远卡在此分支永远发不出帧。
                if self._reacquire_started is None:
                    self._reacquire_started = now
                    log.info(
                        "重新捕获到色块，等待 %.2fs 让平滑器收敛",
                        self.config.loop.reacquire_delay,
                    )
                if now - self._reacquire_started < self.config.loop.reacquire_delay:
                    return result
                self._lost = False
                self._reacquire_started = None
                log.info("追踪已恢复")

            # 发送限速
            min_interval = min(
                1.0 / max(1e-6, self.config.loop.target_fps),
                self.config.loop.send_interval,
            )
            if now - self._last_send < min_interval:
                return result

            frame_bytes = build_frame(
                self.config.control_type, mapping.game_x, mapping.game_y
            )
            result.frame_bytes = frame_bytes
            result.sent = True
            self._send(frame_bytes, result)

        else:
            self._miss_streak += 1
            if self._miss_streak >= self.config.loop.lost_frames and not self._lost:
                self._lost = True
                self._reacquire_started = None
                self.mapper.reset()
                log.info(
                    "连续 %d 帧未检出，交还按键控制（TYPE=02）",
                    self._miss_streak,
                )
                idle = build_idle_frame()
                result.frame_bytes = idle
                result.sent_idle = True
                self._send(idle, result)
                self._last_idle = now
            elif self._lost and now - self._last_idle > 1.0:
                # 周期性重申"交还控制权"，防止单片机超时后行为不确定
                idle = build_idle_frame()
                result.frame_bytes = idle
                result.sent_idle = True
                self._send(idle, result)
                self._last_idle = now

        return result

    def _send(self, frame_bytes: bytes, result: StepResult) -> None:
        """实际写串口并解析应答。"""
        self._last_send = time.monotonic()

        # dry-run 只统计"本应发出"的帧数，绝不累加到 sent ——
        # 否则统计报告会让人误以为真的往串口写了数据。
        if self.dry_run or self.link is None:
            self.stats.dry_run_frames += 1
            log.debug("[dry-run] %s", hexdump(frame_bytes))
            return

        if result.sent_idle:
            self.stats.idle_sent += 1
        else:
            self.stats.sent += 1

        # 串口异常绝不能让它冒泡到主循环 —— 现场 USB 转串口松一下、
        # 驱动掉一次线，整个追踪程序就崩掉，这不可接受。
        # 这里降级成"计数 + 警告"，下一步继续跑，插回去就能恢复。
        try:
            response = self.link.send_frame_and_wait(
                frame_bytes, timeout=self.config.serial.timeout
            )
        except SerialError as exc:
            self.stats.serial_errors += 1
            self.stats.ack_timeout += 1
            # 只在第一次和每 50 次报一次，避免刷屏
            if self.stats.serial_errors == 1 or \
                    self.stats.serial_errors % 50 == 0:
                log.warning("串口异常（第 %d 次）：%s；继续运行，"
                            "检查 USB 转串口是否松动",
                            self.stats.serial_errors, exc)
            return
        except OSError as exc:
            self.stats.serial_errors += 1
            self.stats.ack_timeout += 1
            if self.stats.serial_errors == 1:
                log.warning("串口 I/O 错误：%s；继续运行", exc)
            return

        result.response = response

        if response is Response.OK:
            self.stats.ack_ok += 1
        elif response is Response.ERROR:
            self.stats.ack_error += 1
        else:
            self.stats.ack_timeout += 1

        if self.config.serial.require_ack and response is not Response.OK:
            log.warning(
                "帧未获确认：%s → %s",
                hexdump(frame_bytes),
                response.name,
            )

    # ── 预览渲染 ────────────────────────────────────────────────────

    def render(self, frame: Frame, result: StepResult) -> np.ndarray:
        """把检测结果、ROI、坐标叠加到画面上，供预览与排错。"""
        canvas = self.detector.draw_roi(frame.image)
        if result.detection is not None:
            canvas = self.detector.draw(canvas, [result.detection])

        height, width = canvas.shape[:2]
        overlay: list[str] = []

        if result.mapping is not None:
            m = result.mapping
            overlay.append(
                f"cam=({m.raw_x:.0f},{m.raw_y:.0f}) "
                f"norm=({m.normalized_x:.2f},{m.normalized_y:.2f})"
            )
            overlay.append(f"GAME=({m.game_x}, {m.game_y})")
            # 置信度：排查"检出率为什么低"的第一手信息。
            # 0.9+ 说明模型很确定；0.5 出头说明现场条件与训练有差距。
            if result.detection is not None and \
                    result.detection.score is not None:
                overlay.append(f"conf={result.detection.score:.3f}")
        else:
            overlay.append("NO BLOCK")

        if result.frame_bytes is not None:
            overlay.append(f"TX: {hexdump(result.frame_bytes)}")

        if result.response is not None:
            overlay.append(f"ACK: {result.response.name}")

        overlay.append(
            f"fps={self.stats.fps:.1f} "
            f"hit={self.stats.detection_rate * 100:.0f}% "
            f"ok={self.stats.ack_ok}"
        )

        # 画面亮度：摄像头协商成高帧率导致曝光不足时，这个值会明显偏低
        # （训练数据均值是 137）
        if self.stats.brightness_samples:
            avg_bright = sum(self.stats.brightness_samples) / \
                len(self.stats.brightness_samples)
            overlay.append(f"bright={avg_bright:.0f} (train~137)")

        for index, text in enumerate(overlay):
            cv2.putText(
                canvas, text, (8, 24 + index * 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA,
            )
            cv2.putText(
                canvas, text, (8, 24 + index * 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 255, 80), 1, cv2.LINE_AA,
            )
        return canvas

    # ── 主循环 ──────────────────────────────────────────────────────

    def run(self, duration: float | None = None) -> Stats:
        """跑主循环。``duration=None`` 表示跑到 Ctrl+C 或摄像头断开。"""
        if self.config.debug.save_frames:
            self._captures_dir.mkdir(parents=True, exist_ok=True)

        self.stats = Stats(started_at=time.monotonic())
        interval = 1.0 / max(1e-6, self.config.loop.target_fps)
        save_every = max(1, self.config.debug.save_interval)

        log.info("开始追踪（目标 %.1f fps%s）",
                 self.config.loop.target_fps,
                 "，dry-run 不写串口" if self.dry_run else "")

        # 主循环是"预览"这件事的唯一入口：在这里也保证一遍控制面板，
        # 这样即使调用方没走 open()（scripts/main.py 就是自己开摄像头的），
        # 两个 ROI / 按钮 / 操作指引也一定会出现。
        self._bind_frame_size()
        self._ensure_preview_controls()

        # 显式建窗口 —— 比让 imshow 隐式创建更可靠。
        # 用 WINDOW_AUTOSIZE 让窗口跟随画面尺寸，避免被拉伸。
        if self.config.debug.preview:
            try:
                cv2.namedWindow(self.PREVIEW_WINDOW, cv2.WINDOW_AUTOSIZE)
                log.info("预览窗口已创建：%s", self.PREVIEW_WINDOW)
            except cv2.error as exc:
                log.warning("创建预览窗口失败（%s），已关闭预览", exc)
                self.config.debug.preview = False

        try:
            while True:
                loop_start = time.monotonic()
                if duration is not None and (
                    loop_start - self.stats.started_at >= duration
                ):
                    break

                frame = self.capture.read()
                if frame is None:
                    self.stats.read_failures += 1
                    log.warning("采集失败，重试中…")
                    if self.stats.read_failures > 60:
                        log.error("连续采集失败次数过多，退出")
                        break
                    time.sleep(0.05)
                    continue

                result = self.step(frame)

                if self.config.debug.save_frames and (
                    self.stats.frames % save_every == 0
                ):
                    path = self._captures_dir / f"frame_{self.stats.frames:06d}.jpg"
                    cv2.imwrite(str(path), self.render(frame, result))

                if self.config.debug.preview:
                    canvas = self.render(frame, result)

                    # 控制面板：叠加两个 ROI、键盘提示、按钮条、状态栏
                    if self.roi_editor is not None:
                        # 告诉编辑器这一帧的检测框，用来判断 B 是不是太小
                        # （框贴到 B 边界 = 色块被裁过 = 中心会偏）
                        if result.detection is not None:
                            self.roi_editor.note_detection(
                                result.detection.bbox_xyxy
                            )
                        else:
                            self.roi_editor.note_detection(None)
                        canvas = self.roi_editor.draw_rois(canvas)
                        canvas = self.roi_editor.overlay_hint(canvas)
                        canvas = self.roi_editor.compose(canvas)

                    # 用户可能直接用窗口管理器关掉窗口（点右上角 ×）。
                    # 在 Linux 上这会抛 cv2.error，不接住就是整个程序崩掉。
                    try:
                        cv2.imshow(self.PREVIEW_WINDOW, canvas)
                        self._preview_available = True
                        key = cv2.waitKey(1) & 0xFF
                    except cv2.error as exc:
                        log.info("预览窗口已关闭（%s），继续无画面运行", exc)
                        self.config.debug.preview = False
                        self._preview_available = False
                        continue

                    if self.roi_editor is not None:
                        # 首次显示时挂上鼠标回调
                        if not self._mouse_hooked:
                            cv2.setMouseCallback(
                                self.PREVIEW_WINDOW, self.roi_editor.on_mouse
                            )
                            self._mouse_hooked = True

                        # 键盘和按钮都要能退出：鼠标点 Quit 时没有返回值
                        # 能传到这里，编辑器会用 exit_requested 记一笔。
                        if self.roi_editor.on_key(key) == "quit":
                            log.info("用户按键退出")
                            break
                        if self.roi_editor.exit_requested:
                            log.info("用户点击 Quit 退出")
                            break
                    else:
                        if key in (27, ord("q")):
                            log.info("用户按键退出")
                            break

                elapsed = time.monotonic() - loop_start
                if elapsed < interval:
                    time.sleep(interval - elapsed)

        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，停止")
        finally:
            self.stats.finished_at = time.monotonic()

        return self.stats


# ──────────────────────────────────────────────────────────────────────
# 工厂：构建检测器、摄像头、串口
# ──────────────────────────────────────────────────────────────────────


def build_detector(config: AppConfig):
    """按 ``detector.engine`` 构建对应的检测器。

    两种检测器接口完全一致（``detect`` / ``detect_all`` / ``draw`` /
    ``draw_roi``），流水线对它们一视同仁，因此切换引擎不需要改流水线代码。

    · ``yolo`` —— 用仓库自带的 RKNN 模型在 NPU 上检测（主用方案）
    · ``hsv``  —— 传统阈值分割（无需模型，可作降级方案）
    """
    engine = config.detector.engine_normalized

    if engine == "yolo":
        from .card_detector import CardDetector, default_model_path

        model_path = (
            Path(config.detector.model_path)
            if config.detector.model_path
            else default_model_path()
        )
        # ── 双 ROI ──────────────────────────────────────────────
        #  A = mapping ROI：决定像素坐标 ↔ 游戏坐标的对应关系（逻辑边界）
        #  B = detection ROI：决定往模型里送多大范围，应比 A 大一圈
        #
        #  两者必须分开：若 B == A，色块部分移出 A 时模型只能看到剩余
        #  部分，算出的中心是"可见部分的中心"而非真实中心 —— 结果就是
        #  挡板到不了最边上，紧贴边缘落下的星星接不到。
        mapping_roi = config.mapping.build_roi()
        is_full_frame = (
            mapping_roi.x == 0 and mapping_roi.y == 0
            and mapping_roi.w == config.camera.width
            and mapping_roi.h == config.camera.height
        )

        detect_roi = None
        if not is_full_frame or config.detector.roi_w > 0:
            detect_roi = config.detector.build_roi(mapping_roi)

        log.info("检测引擎：YOLO / RKNN  （模型 %s）", model_path.name)
        if detect_roi is not None:
            log.info(
                "双 ROI：映射 A=(%d,%d,%d,%d)  检测 B=(%d,%d,%d,%d)",
                mapping_roi.x, mapping_roi.y,
                mapping_roi.w, mapping_roi.h,
                detect_roi[0], detect_roi[1], detect_roi[2], detect_roi[3],
            )
        return CardDetector(
            model_path=model_path,
            conf_threshold=config.detector.conf_threshold,
            nms_threshold=config.detector.nms_threshold,
            input_size=config.detector.input_size,
            core_mask=config.detector.core_mask,
            roi=detect_roi,
            max_blocks=config.detector.max_blocks,
            warmup=config.detector.warmup,
            box_format=config.detector.box_format,
        )

    log.info("检测引擎：HSV 阈值分割（颜色 %s）", config.detector.preset)
    return ColorDetector(
        ranges=config.detector.build_ranges(),
        min_area=config.detector.min_area,
        max_area=config.detector.max_area,
        blur_size=config.detector.blur_size,
        morph_kernel=config.detector.morph_kernel,
        morph_iterations=config.detector.morph_iterations,
        max_blocks=config.detector.max_blocks,
    )


def build_capture(config: AppConfig) -> UsbCamera:
    return UsbCamera(
        index=config.camera.index,
        width=config.camera.width,
        height=config.camera.height,
        fps=config.camera.fps,
        fourcc=config.camera.fourcc,
    )


def build_link(config: AppConfig) -> SerialPort | None:
    """按配置建立串口连接。

    ``port=auto`` 时会依次发送探测帧，谁能回 ``OK`` 就用谁 ——
    这是判断"哪个 /dev/ttySx 接到了 STM32"最可靠的方式。
    """
    if config.serial.port and config.serial.port != "auto":
        port = SerialPort(
            config.serial.port,
            baudrate=config.serial.baudrate,
            timeout=config.serial.timeout,
        )
        port.open()
        log.info("已连接串口 %s @ %d", config.serial.port, config.serial.baudrate)
        return port

    log.info("串口设为 auto，开始自动探测…")
    results = probe_stm32(baudrate=config.serial.baudrate,
                          timeout=config.serial.timeout)

    for item in results:
        status = {
            Response.OK: "OK",
            Response.ERROR: "ERROR",
            Response.TIMEOUT: "无响应",
        }[item.response]
        if not item.opened:
            log.info("  %s  打开失败", item.device)
        else:
            log.info("  %s  → %s  %s", item.device, status, item.detail)

    candidates = [r for r in results if r.is_candidate]
    if not candidates:
        log.warning(
            "没有探测到回 OK 的串口。请检查：STM32 是否在跑、"
            "波特率是否一致、TX/RX 是否交叉、共地是否接好。"
        )
        return None

    chosen = candidates[0].device
    log.info("选定串口：%s", chosen)
    port = SerialPort(
        chosen, baudrate=config.serial.baudrate, timeout=config.serial.timeout
    )
    port.open()
    return port
