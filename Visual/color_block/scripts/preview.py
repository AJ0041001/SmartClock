#!/usr/bin/env python3
"""可视化预览 —— 实时查看检测效果、坐标映射，并支持鼠标标定。

这是现场调试的主力工具。它把"摄像头看到什么""算法检出了什么""最终会发给
STM32 什么坐标"三件事同时显示在一个窗口里，出问题一眼就能看出是哪一环。

窗口分三块：视频区 + 按钮条 + 状态栏。**两个 ROI 都用鼠标调**：

    A 映射区（黄色实线）—— 像素坐标 ↔ 游戏坐标 的对应关系
    B 检测区（绿色虚线）—— 送进模型的范围，必须比 A 大一圈

按钮条上点 `Pick A` / `Pick B` 框矩形，点 `B +10` / `B -10` 调大小，
点 `Save` 写回配置，点 `Quit` 退出。键盘有等价快捷键（见下）。

窗口内的操作
------------
    a / b       框选 A（映射区）/ B（检测区），点两下确定矩形
    0           B = A + 边距（一键重置）
    + / -       B 每边放大 / 缩小 10 像素
    f           B 切到整幅画面 / 切回 A+边距
    w           把 A、B 写回配置文件
    s           保存当前画面
    p           切到"取色模式"：鼠标点哪里就从哪里采样颜色（仅 HSV 引擎）
    d           开关掩膜显示（看 HSV 分割到底切出了什么）
    q / ESC     退出（框选到一半时 ESC 只取消框选）

用法::

    python3 scripts/preview.py --camera
    python3 scripts/preview.py --image assets/captures/sample.jpg
    python3 scripts/preview.py --camera --write-roi   # 退出时自动写回配置
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.camera import UsbCamera  # noqa: E402
from src.cli import (  # noqa: E402
    add_camera_arguments,
    add_common_arguments,
    add_detector_arguments,
    add_mapping_arguments,
    banner,
    load_config,
    setup_logging,
)
from src.config import AppConfig  # noqa: E402
from src.detector import ColorDetector, sample_region_color  # noqa: E402
from src.mapper import CoordinateMapper  # noqa: E402
from src.protocol import build_frame, hexdump  # noqa: E402
from src.roi_editor import RoiEditor  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="preview.py",
        description="色块检测可视化预览与鼠标标定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_arguments(parser)
    add_camera_arguments(parser)
    add_detector_arguments(parser)
    add_mapping_arguments(parser)

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--camera", action="store_true", help="使用摄像头实时预览")
    source.add_argument("--image", help="对静态图片预览")

    parser.add_argument("--write-roi", action="store_true",
                        help="退出时把 A、B 两个 ROI 都写回配置文件")
    parser.add_argument("--print-frames", action="store_true",
                        help="每帧打印将要发送的串口帧（内容较多）")
    return parser.parse_args()


class PreviewState:
    """预览窗口的可变状态与鼠标交互逻辑。

    ROI 的编辑**不在这里实现** —— 统一交给 `src/roi_editor.RoiEditor`，
    这样 `preview.py` 和 `main.py` 的行为完全一致（包括双 ROI 和贴边告警），
    不会出现"两个脚本调 ROI 的方式不一样"的坑。

    这里只保留预览脚本独有的东西：取色模式、掩膜显示、存图计数。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.mode = "view"          # view | pick
        self.last_message = ""
        self.show_mask = False
        self.saved_frames = 0
        self.editor: RoiEditor | None = None
        self._last_frame: np.ndarray | None = None

    def set_message(self, text: str) -> None:
        self.last_message = text
        print(f"  → {text}")

    def on_mouse(self, event: int, x: int, y: int, flags: int,
                 param) -> None:
        """鼠标回调：取色模式自己处理，其余全部转给 ROI 编辑器。"""
        if event != cv2.EVENT_LBUTTONDOWN:
            if self.editor is not None:
                self.editor.on_mouse(event, x, y, flags, param)
            return

        if self.mode == "pick":
            self._handle_pick(x, y)
            return

        if self.editor is not None:
            self.editor.on_mouse(event, x, y, flags, param)

    def _handle_pick(self, x: int, y: int) -> None:
        """在点击位置采样颜色，更新检测区间。"""
        if self._last_frame is None:
            return
        try:
            sampled = sample_region_color(self._last_frame, x, y, radius=12)
        except ValueError as exc:
            self.set_message(f"取色失败：{exc}")
            return

        self.config.detector.preset = "custom"
        self.config.detector.custom_ranges = [
            r.to_dict() for r in sampled.ranges
        ]
        self.set_message(
            f"已取色 ({x},{y})：H={sampled.center_hsv[0]:.0f} "
            f"S={sampled.center_hsv[1]:.0f} V={sampled.center_hsv[2]:.0f}  "
            f"共 {len(sampled.ranges)} 段区间"
        )
        self.mode = "view"


def render(
    image: np.ndarray,
    detector: ColorDetector,
    mapper: CoordinateMapper,
    state: PreviewState,
    fps: float,
) -> np.ndarray:
    """把各种叠加信息画到画面上。"""
    canvas = image.copy()

    # 检测结果
    detection = detector.detect(image)
    lines: list[str] = []

    if detection is not None:
        x, y, w, h = detection.bbox
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cx, cy = detection.center_int
        cv2.drawMarker(canvas, (cx, cy), (0, 0, 255),
                       cv2.MARKER_CROSS, 20, 2)
        cv2.circle(canvas, (cx, cy), 3, (255, 255, 255), -1)

        result = mapper.map_point(*detection.center)
        lines.append(f"cam   = ({result.raw_x:.0f}, {result.raw_y:.0f})")
        lines.append(f"norm  = ({result.normalized_x:.3f}, "
                     f"{result.normalized_y:.3f})")
        lines.append(f"GAME  = ({result.game_x}, {result.game_y})")

        frame = build_frame(state.config.control_type,
                            result.game_x, result.game_y)
        lines.append(f"TX    = {hexdump(frame)}")
    else:
        lines.append("NO BLOCK DETECTED")

    lines.append(f"area_min = {state.config.detector.min_area:.0f}")
    lines.append(f"fps = {fps:.1f}   mode = {state.mode}")

    for index, text in enumerate(lines):
        position = (8, 24 + index * 22)
        cv2.putText(canvas, text, position, cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 0, 0), 3, cv2.LINE_AA)
        color = (80, 255, 80) if not text.startswith("NO BLOCK") else (80, 80, 255)
        cv2.putText(canvas, text, position, cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 1, cv2.LINE_AA)

    # 两个 ROI（A 映射实线 / B 检测虚线）。放在文字之后画，
    # 这样框线不会被信息文字压住。
    if state.editor is not None:
        canvas = state.editor.draw_rois(canvas)

    # 消息：放在信息文字下面，避免和底部提示打架
    if state.last_message:
        y = 24 + len(lines) * 22
        cv2.putText(canvas, state.last_message, (8, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, state.last_message, (8, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

    # 掩膜小窗
    if state.show_mask:
        mask = detector.make_mask(image)
        mask_bgr = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        scale = 0.28
        small = cv2.resize(mask_bgr, None, fx=scale, fy=scale)
        h, w = small.shape[:2]
        canvas[8:8 + h, canvas.shape[1] - w - 8:canvas.shape[1] - 8] = small

    # 键盘提示 + 按钮条 + 状态栏（与主程序共用同一套控件）
    if state.editor is not None:
        canvas = state.editor.overlay_hint(canvas)
        canvas = state.editor.compose(canvas)

    return canvas


def main() -> int:
    args = parse_args()
    config = load_config(args)
    setup_logging(config.debug.log_level)

    print(banner("色块检测可视化预览", "现场调试与鼠标标定"))
    print()
    print(config.describe())
    print()
    print("窗口快捷键：")
    print("  a/b 框 A(映射区)/B(检测区)   0=B+边距   +/- 调 B 大小   f 整幅画面")
    print("  w 写回配置    s 存图    p 取色    d 掩膜    q/ESC 退出")
    print("  也可以直接用鼠标点画面下方的按钮条。")
    print()

    state = PreviewState(config)

    # ── 静态图片模式 ────────────────────────────────────────────────
    if args.image:
        image = cv2.imread(args.image)
        if image is None:
            print(f"❌ 无法读取图片：{args.image}", file=sys.stderr)
            return 1
        state._last_frame = image
        # 用 build_detector 而不是直接 new ColorDetector ——
        # 否则会忽略配置里的 detector.engine，配了 yolo 却还在跑 HSV。
        from src.pipeline import build_detector

        detector = build_detector(config)
        detector.open()
        mapper = CoordinateMapper(
            roi=config.mapping.build_roi(),
            invert_x=config.mapping.invert_x,
            invert_y=config.mapping.invert_y,
            swap_xy=config.mapping.swap_xy,
            fixed_y=config.mapping.fixed_y,
            smoothing=None,   # 静态图不做平滑，直接看真实映射
            deadband=0,
        )
        mapper.bind_frame(image.shape[1], image.shape[0])
        state.editor = RoiEditor(
            config=config,
            mapper=mapper,
            detector=detector,
            frame_size=(image.shape[1], image.shape[0]),
            config_path=args.config,
        )
        canvas = render(image, detector, mapper, state, fps=0.0)

        out = Path(config.debug.save_dir) / "preview_static.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), canvas)
        print(f"已保存预览图：{out}")

    # ── 摄像头实时模式 ──────────────────────────────────────────────
    camera = UsbCamera(
        index=config.camera.index,
        width=config.camera.width,
        height=config.camera.height,
        fps=config.camera.fps,
        fourcc=config.camera.fourcc,
    )
    try:
        camera.open()
    except Exception as exc:
        print(f"❌ 摄像头打开失败：{exc}", file=sys.stderr)
        if not args.image:
            return 1
        return 0

    print(f"摄像头：{camera.summary()}")
    window = "SmartClock color block preview"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window, state.on_mouse)

    save_dir = Path(config.debug.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    # 检测器分两种处理方式：
    #   · YOLO  —— 持有 NPU 上下文，加载模型要一两秒，**必须复用**
    #              （每帧重建会反复加载 7MB 模型，直接卡死）
    #   · HSV   —— 纯 CPU、无状态，每帧重建以便取色标定立刻生效
    persistent_detector = None
    if config.detector.engine_normalized == "yolo":
        from src.pipeline import build_detector

        persistent_detector = build_detector(config)
        persistent_detector.open()
        print(f"检测器：YOLO / RKNN（NPU）")

    fps = 0.0
    last_time = time.monotonic()
    smoothed_fps = 0.0

    # 映射器**只建一次**：编辑器会直接改它的 roi，如果每帧重建，
    # 用户刚框好的 A 下一帧就被配置里的旧值覆盖回去了。
    mapper = CoordinateMapper(
        roi=config.mapping.build_roi(),
        invert_x=config.mapping.invert_x,
        invert_y=config.mapping.invert_y,
        swap_xy=config.mapping.swap_xy,
        fixed_y=config.mapping.fixed_y,
        smoothing=config.mapping.smoothing,
        deadband=config.mapping.deadband,
    )
    mapper.bind_frame(camera.actual_width or config.camera.width,
                      camera.actual_height or config.camera.height)

    # 编辑器需要检测器来同步 B，也需要真实的画面尺寸来限幅。
    # 注意：HSV 引擎的检测是全画面做的、不吃 ROI 裁切，
    # 所以 B 只对 YOLO 引擎有实际作用（界面上仍可调，不影响使用）。
    state.editor = RoiEditor(
        config=config,
        mapper=mapper,
        detector=persistent_detector,
        frame_size=(camera.actual_width or config.camera.width,
                    camera.actual_height or config.camera.height),
        config_path=args.config,
    )

    try:
        while True:
            frame = camera.read()
            if frame is None:
                print("⚠️  抓帧失败")
                time.sleep(0.1)
                continue

            image = frame.image
            state._last_frame = image

            now = time.monotonic()
            delta = now - last_time
            last_time = now
            if delta > 0:
                instant = 1.0 / delta
                smoothed_fps = (
                    instant if smoothed_fps == 0
                    else 0.9 * smoothed_fps + 0.1 * instant
                )
            fps = smoothed_fps

            # 检测器：YOLO 复用同一个实例（NPU 上下文），HSV 每帧重建
            # （重建是为了让 `p` 取色改完的区间立刻生效）
            if persistent_detector is not None:
                detector = persistent_detector
            else:
                detector = ColorDetector(
                    ranges=config.detector.build_ranges(),
                    min_area=config.detector.min_area,
                    blur_size=config.detector.blur_size,
                    morph_kernel=config.detector.morph_kernel,
                    morph_iterations=config.detector.morph_iterations,
                )

            canvas = render(image, detector, mapper, state, fps)

            if args.print_frames:
                detection = detector.detect(image)
                if detection is not None:
                    result = mapper.map_point(*detection.center)
                    print(hexdump(build_frame(
                        config.control_type, result.game_x, result.game_y
                    )))

            cv2.imshow(window, canvas)
            key = cv2.waitKey(1) & 0xFF

            # 预留给本脚本自己的快捷键：s 存图、p 取色、d 掩膜、w 写配置。
            # 其余按键交给 ROI 编辑器（a/b/0/+/-/f/q/ESC），两边不会打架。
            handled = False
            if key == ord("s"):
                path = save_dir / f"preview_{state.saved_frames:04d}.jpg"
                cv2.imwrite(str(path), canvas)
                state.set_message(f"已保存 {path}")
                state.saved_frames += 1
                handled = True
            elif key == ord("p"):
                state.mode = "pick"
                state.set_message("取色模式：请点击色块")
                handled = True
            elif key == ord("d"):
                state.show_mask = not state.show_mask
                state.set_message(
                    f"掩膜显示：{'开' if state.show_mask else '关'}")
                handled = True
            elif key == ord("w"):
                state.editor.apply_to_config()
                config.save(args.config)
                state.set_message(f"已写回配置：{args.config}")
                handled = True

            if not handled and state.editor is not None:
                if state.editor.on_key(key) == "quit":
                    break
                # 框选过程中把提示同步到本脚本的终端输出，方便事后回看
                if state.editor.state.message:
                    state.last_message = state.editor.state.message

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C")
    finally:
        camera.close()
        # YOLO 检测器持有 NPU 上下文，退出时释放
        if persistent_detector is not None:
            try:
                persistent_detector.close()
            except Exception:
                pass
        cv2.destroyAllWindows()

    # ── 收尾：可选写回两个 ROI ──────────────────────────────────────
    if args.write_roi and state.editor is not None:
        state.editor.apply_to_config()
        config.save(args.config)
        roi_a = config.mapping
        roi_b = config.detector
        print(f"\n✅ A、B 已写回：{args.config}")
        print(f"   A 映射区 = ({roi_a.roi_x},{roi_a.roi_y},"
              f"{roi_a.roi_w},{roi_a.roi_h})")
        print(f"   B 检测区 = ({roi_b.roi_x},{roi_b.roi_y},"
              f"{roi_b.roi_w},{roi_b.roi_h})")
        if roi_b.roi_w > 0 and roi_b.roi_w <= roi_a.roi_w:
            print("\n⚠️  B 并不比 A 大 —— 卡片贴到 A 边界时会被裁掉一角，"
                  "中心会算偏。")
            print("    建议在窗口里按 0（B = A + 边距）后重新写回。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
