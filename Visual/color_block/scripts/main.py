#!/usr/bin/env python3
"""SmartClock 色块追踪主程序。

把摄像头画面里的色块中心实时映射成游戏坐标，按协议帧发给 STM32。

典型用法
--------
先空跑确认坐标对（不接串口）::

    python3 scripts/main.py --dry-run --preview

接上 STM32 正式运行::

    python3 scripts/main.py --serial-port /dev/ttyS3

自动探测串口::

    python3 scripts/main.py --serial-port auto

先跑 30 秒做验证::

    python3 scripts/main.py --duration 30 --serial-port auto
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cli import (  # noqa: E402
    add_camera_arguments,
    add_common_arguments,
    add_detector_arguments,
    add_mapping_arguments,
    add_serial_arguments,
    banner,
    load_config,
    report_config_problems,
    setup_logging,
)
from src.pipeline import (  # noqa: E402
    ColorTrackingPipeline,
    build_capture,
    build_link,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="SmartClock 色块追踪 → 串口坐标回传",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_arguments(parser)
    add_camera_arguments(parser)
    add_detector_arguments(parser)
    add_mapping_arguments(parser)
    add_serial_arguments(parser)

    parser.add_argument("--dry-run", action="store_true",
                        help="只做检测与映射，不向串口写任何字节")
    parser.add_argument("--duration", type=float, default=None,
                        help="运行指定秒数后自动退出；缺省则一直跑到 Ctrl+C")
    parser.add_argument("--preview", action="store_true", default=None,
                        help="强制开启实时预览窗口（默认已开启）")
    parser.add_argument("--no-preview", action="store_true",
                        help="关闭实时预览窗口（省 CPU，或纯 SSH 环境）")
    parser.add_argument("--save-frames", action="store_true",
                        help="把处理后的画面落盘，便于事后分析")
    parser.add_argument("--require-ack", action="store_true",
                        help="单片机没回 OK 时打印警告")
    return parser.parse_args()


def has_display() -> bool:
    """检测是否有可用的图形界面。

    没有显示器时不该弹预览 —— 那会让 OpenCV 直接报错崩掉。
    这里提前判断，优雅降级。
    """
    import os
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("DISPLAY"):
        return True
    return False


def main() -> int:
    args = parse_args()

    config = load_config(args)
    setup_logging(config.debug.log_level)

    # ── 预览窗口：默认开，没显示器或显式关闭时优雅降级 ──
    if args.no_preview:
        config.debug.preview = False
    elif not has_display():
        if config.debug.preview:
            print("⚠️  未检测到图形界面（无 DISPLAY/WAYLAND_DISPLAY），"
                  "已自动关闭预览窗口")
            print("    画面仍会正常处理，只是不显示。需要看图请用：")
            print("      cam-view --save shot.jpg    （存一张图）")
            print()
        config.debug.preview = False

    if args.save_frames:
        config.debug.save_frames = True
    if args.require_ack:
        config.serial.require_ack = True

    print(banner(
        "SmartClock 色块追踪",
        "USB摄像头 → YOLO检测 → 坐标映射 → USART2 串口回传",
    ))
    print()
    print(config.describe())
    print()

    # 界面中文渲染依赖系统字体。缺字体不会崩，但画面上的提示会少字，
    # 所以启动时明确说一句，免得事后当成 bug 查。
    if config.debug.preview:
        try:
            from src.text_cjk import describe as describe_font

            print(describe_font())
        except Exception:
            pass
        print()
        print("── 实时画面里的操作（画面上也会显示同样的指引）──")
        print("   按 r → 在画面里点两下，框出卡片会移动到的范围（A 映射区）")
        print("   按 0 → B 检测区自动 = A 向外扩一圈")
        print("   按 s → 保存到 config.yaml")
        print("   按 t → 单独框选 B      按 +/- 调 B 大小    按 q 退出")
        print("   验收：把卡片沿 A 的四条边和四个角走一遍，")
        print("         只要 B（绿色虚线）变红，就按 + 放大，直到不再变红。")
        print()

    if report_config_problems(config):
        print("请先修正上述配置问题（可直接编辑 config.yaml）。",
              file=sys.stderr)
        return 2

    # ── 建立摄像头 ──────────────────────────────────────────────────
    try:
        capture = build_capture(config)
        capture.open()
    except Exception as exc:
        print(f"\n❌ 摄像头打开失败：{exc}", file=sys.stderr)
        print("\n排查建议：", file=sys.stderr)
        print("  1. 运行 python3 scripts/camera_check.py 看设备是否被识别",
              file=sys.stderr)
        print("  2. 确认 USB 摄像头已插好（lsusb 能看到）", file=sys.stderr)
        print("  3. 确认没有别的程序（预览窗口/guvcview/motion）占用它",
              file=sys.stderr)
        return 3

    print(f"摄像头就绪：{capture.summary()}")

    # 帧率异常检查：UVC 摄像头有时会协商到远高于配置的帧率，
    # 那样曝光时间被压缩、画面会偏暗，直接影响检测率。
    try:
        actual_fps = capture.actual_fps
        want_fps = config.camera.fps
        if actual_fps > 0 and want_fps > 0 and actual_fps > want_fps * 1.5:
            print()
            print(f"⚠️  摄像头实际帧率 {actual_fps:.0f}fps，远高于配置的 "
                  f"{want_fps}fps")
            print(f"    高帧率会压缩曝光时间，画面可能偏暗、噪声变大，"
                  f"影响检测率。")
            print(f"    如果稍后发现检出率异常，试试把 config.yaml 里的 "
                  f"camera.fps 改小（如 15）。")
            print()
    except Exception:
        pass

    # ── 建立串口 ────────────────────────────────────────────────────
    link = None
    if args.dry_run:
        print("串口：dry-run 模式，不建立连接")
    else:
        try:
            link = build_link(config)
        except Exception as exc:
            capture.close()
            print(f"\n❌ 串口打开失败：{exc}", file=sys.stderr)
            return 4

        if link is None:
            capture.close()
            print("\n❌ 未能建立串口连接。", file=sys.stderr)
            print("   可运行 python3 scripts/serial_test.py 单独排查。",
                  file=sys.stderr)
            print("   或先用 --dry-run 验证视觉部分。", file=sys.stderr)
            return 5
        print(f"串口就绪：{link.device} @ {link.baudrate}")

    print()
    print("开始运行（Ctrl+C 停止）…")
    print()

    # ── 跑流水线 ────────────────────────────────────────────────────
    pipeline = ColorTrackingPipeline(
        config=config,
        capture=capture,
        link=link,
        dry_run=args.dry_run,
        # 传配置路径，这样窗口里按 s 能把 ROI 写回 config.yaml
        config_path=args.config,
    )
    # 必须调用 open()：ROI 控制面板（两个大框 / 按钮 / 操作指引）
    # 就是在这里建立的。以前漏了这一句，结果窗口能弹出、检测也正常，
    # 但整个控制面板都不见了。
    pipeline.open()
    try:
        stats = pipeline.run(duration=args.duration)
    finally:
        pipeline.close()

    print()
    print(stats.report())

    # 收尾判定：给一个明确结论，而不是让人自己看数字
    if args.dry_run:
        print("\n✅ dry-run 完成。视觉链路正常。")
        if stats.detections == 0:
            print("⚠️  但一帧都没检出色块 —— 请检查颜色阈值与"
                  "detector.min_area，可运行 scripts/color_pick.py 重新标定。")
    elif stats.sent == 0:
        print("\n⚠️  没有发出任何数据帧 —— 检查是否一直没检出色块。")
    elif stats.ack_ok == 0:
        print("\n⚠️  发出数据帧但单片机一次都没回 OK —— 重点排查：")
        print("   · 波特率是否与 STM32 一致")
        print("   · TX/RX 是否交叉连接、是否共地")
        print("   · STM32 是否真的在运行且已启动 USART2 接收")
    else:
        print(f"\n✅ 追踪正常：{stats.ack_ok} 帧获单片机确认。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
