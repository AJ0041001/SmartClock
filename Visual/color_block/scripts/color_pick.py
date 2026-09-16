#!/usr/bin/env python3
"""取色标定 —— 从画面里采样色块，自动推导 HSV 阈值并写回配置。

这是整个流程里最需要"现场调"的一步：房间灯光、摄像头白平衡、色块本身的
材质都会影响 HSV 值，预设色只能作为起点，实战必须按现场重新标定。

用法::

    # 1) 从摄像头抓一帧存下来，方便反复标定
    python3 scripts/color_pick.py --capture assets/captures/sample.jpg

    # 2) 在图上点出色块位置（用图片查看器看坐标），然后采样
    python3 scripts/color_pick.py --image assets/captures/sample.jpg --x 320 --y 240

    # 3) 不知道色块在哪，让程序自己找画面里最显眼的彩色物体
    python3 scripts/color_pick.py --image assets/captures/sample.jpg --auto

    # 4) 确认结果满意后写回配置
    python3 scripts/color_pick.py --image assets/captures/sample.jpg --auto --write

标定完务必用预览确认::

    python3 scripts/preview.py --camera
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from src.camera import UsbCamera  # noqa: E402
from src.cli import (  # noqa: E402
    add_camera_arguments,
    add_common_arguments,
    add_detector_arguments,
    banner,
    load_config,
    setup_logging,
)
from src.config import AppConfig  # noqa: E402
from src.detector import (  # noqa: E402
    ColorDetector,
    HsvRange,
    dominant_colored_pixel,
    sample_region_color,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="color_pick.py",
        description="色块 HSV 取色标定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_arguments(parser)
    add_camera_arguments(parser)
    add_detector_arguments(parser)

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="从图片文件采样")
    source.add_argument("--capture", metavar="OUT",
                        help="从摄像头抓一帧保存到 OUT，并对其采样")
    source.add_argument("--camera", action="store_true",
                        help="直接对摄像头当前画面采样（不存盘）")

    parser.add_argument("--x", type=int, default=None, help="采样点 X")
    parser.add_argument("--y", type=int, default=None, help="采样点 Y")
    parser.add_argument("--radius", type=int, default=15,
                        help="采样半径（像素），默认 15")
    parser.add_argument("--auto", action="store_true",
                        help="自动寻找画面中最显眼的彩色区域作为采样点")
    parser.add_argument("--write", action="store_true",
                        help="把结果写回 config.yaml 并设 preset=custom")
    parser.add_argument("--save-annotated", default=None,
                        help="把标注了采样位置的图保存下来，便于核对")
    return parser.parse_args()


def grab_image(args: argparse.Namespace, config: AppConfig):
    """按参数取得一张 BGR 图像。"""
    if args.image:
        path = Path(args.image)
        if not path.exists():
            raise FileNotFoundError(f"图片不存在：{path}")
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"无法解码图片：{path}（格式不支持？）")
        print(f"已载入图片：{path}  {image.shape[1]}x{image.shape[0]}")
        return image

    camera = UsbCamera(
        index=config.camera.index,
        width=config.camera.width,
        height=config.camera.height,
        fps=config.camera.fps,
        fourcc=config.camera.fourcc,
    )
    camera.open()
    try:
        print(f"摄像头：{camera.summary()}")
        frame = camera.read()
        if frame is None:
            raise RuntimeError("摄像头抓帧失败")
        image = frame.image
        if args.capture:
            out = Path(args.capture)
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), image)
            print(f"已保存抓帧：{out}")
        return image
    finally:
        camera.close()


def main() -> int:
    args = parse_args()
    config = load_config(args)
    setup_logging(config.debug.log_level)

    print(banner("色块取色标定", "采样 → 推导 HSV 阈值 → 写回配置"))
    print()

    try:
        image = grab_image(args, config)
    except Exception as exc:
        print(f"❌ 取图失败：{exc}", file=sys.stderr)
        return 1

    # ── 确定采样点 ──────────────────────────────────────────────────
    sample_x, sample_y = args.x, args.y

    if args.auto or sample_x is None or sample_y is None:
        if not args.auto and (sample_x is None or sample_y is None):
            print("未指定采样点，自动寻找画面中最显眼的彩色区域…")
        found = dominant_colored_pixel(image)
        if found is None:
            print("❌ 画面里没有找到足够的彩色区域。", file=sys.stderr)
            print("   请确认色块在视野内，或用 --x/--y 手动指定采样点。",
                  file=sys.stderr)
            return 1
        sample_x, sample_y = found
        print(f"自动定位到彩色区域中心：({sample_x}, {sample_y})")

    print()

    # ── 采样并推导区间 ──────────────────────────────────────────────
    try:
        sampled = sample_region_color(
            image,
            sample_x,
            sample_y,
            radius=args.radius,
            saturation_floor=60,
        )
    except ValueError as exc:
        print(f"❌ 采样失败：{exc}", file=sys.stderr)
        return 1

    print("── 采样结果 ──")
    print(sampled.describe())
    print()

    # ── 用采样区间回检，给出客观的"能不能检出"反馈 ─────────────────
    test_detector = ColorDetector(
        ranges=sampled.ranges,
        min_area=args.detector_min_area or config.detector.min_area,
        morph_kernel=3,
        morph_iterations=1,
    )
    detection = test_detector.detect(image)

    print("── 回检 ──")
    if detection is None:
        print("  ⚠️  用该区间在采样图上检不出色块。")
        print("     可以尝试：加大 --radius 多采一些像素；"
              "或确认采样点确实落在色块上。")
    else:
        dx = detection.center[0] - sample_x
        dy = detection.center[1] - sample_y
        print(f"  ✓ 检出成功，质心 ({detection.center[0]:.0f}, "
              f"{detection.center[1]:.0f})")
        print(f"    与采样点偏差：dx={dx:+.1f}  dy={dy:+.1f}")
        print(f"    轮廓面积：{detection.area:.0f} 像素²")
        print(f"    外接框：{detection.bbox}")

    # ── 可视化 ──────────────────────────────────────────────────────
    canvas = image.copy()
    cv2.circle(canvas, (sample_x, sample_y), args.radius, (255, 255, 0), 2)
    cv2.drawMarker(canvas, (sample_x, sample_y), (0, 255, 255),
                   cv2.MARKER_CROSS, 20, 2)
    if detection is not None:
        x, y, w, h = detection.bbox
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 255, 0), 2)

    out_path = args.save_annotated
    if out_path is None:
        out_path = str(
            Path(config.debug.save_dir) / "captures" / "color_pick_annotated.jpg"
        )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, canvas)
    print(f"\n已保存标注图：{out_path}")

    # ── 输出配置片段 ────────────────────────────────────────────────
    print()
    print("── 写入 config.yaml 的内容 ──")
    print("detector:")
    print("  preset: custom")
    print("  custom_ranges:")
    for hsv_range in sampled.ranges:
        print(f"    - lower: {list(hsv_range.lower)}")
        print(f"      upper: {list(hsv_range.upper)}")
    print()

    if args.write:
        config.detector.preset = "custom"
        config.detector.custom_ranges = [
            hsv_range.to_dict() for hsv_range in sampled.ranges
        ]
        config.save(args.config)
        print(f"✅ 已写回配置：{args.config}")
        print("   下一步：python3 scripts/preview.py --camera 确认效果")
    else:
        print("（加 --write 可自动写回配置文件）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
