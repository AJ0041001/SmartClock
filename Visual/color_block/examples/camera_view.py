#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头查看器 —— 只看画面，不做任何检测
=======================================

用途：确认摄像头能出图、调整角度、看看现场光照。

刻意做得最简单：打开摄像头 → 显示实时画面 → 按 q 退出。
没有模型、没有检测、没有协议，出问题时容易定位。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────

  # 最简单：直接看画面
  python3 examples/camera_view.py

  # 指定设备编号（默认找 /dev/video0）
  python3 examples/camera_view.py --index 2

  # 先看有哪些摄像头
  python3 examples/camera_view.py --list

  # 没有显示器（SSH 连接）时，存一张图然后传到电脑上看
  python3 examples/camera_view.py --save shot.jpg

  # 每隔几秒存一张，观察变化
  python3 examples/camera_view.py --save-dir captures --interval 3

────────────────────────────────────────────────────────────────
窗口里的按键
────────────────────────────────────────────────────────────────

  q / ESC    退出
  s          保存当前画面到 snapshots/
  f          全屏 / 退出全屏
  i          显示/隐藏信息叠加
  空格        暂停 / 继续

提示：用鼠标点一下窗口让它获得焦点，按键才有效。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────
# 摄像头信息
# ──────────────────────────────────────────────────────────────────


def list_cameras() -> int:
    """列出所有 V4L2 视频设备。"""
    print("── 系统中的视频设备 ──")

    found = []
    devices_dir = Path("/sys/class/video4linux")
    if devices_dir.is_dir():
        for node in sorted(devices_dir.glob("video*")):
            name = ""
            name_file = node / "name"
            if name_file.exists():
                try:
                    name = name_file.read_text().strip()
                except OSError:
                    pass
            index = int(node.name.replace("video", ""))
            found.append((index, name))

    if not found:
        print("  （一个都没有）")
        print()
        print("排查：")
        print("  1. USB 摄像头插好了吗？重新插拔试试")
        print("  2. 换个 USB 口（优先用蓝色 USB 3.0 口）")
        print("  3. 执行 dmesg | tail -20 看内核有没有识别到")
        print("  4. 执行 lsusb 看 USB 总线上有没有它")
        return 1

    print()
    for index, name in found:
        device = "/dev/video%d" % index
        exists = "✓" if Path(device).exists() else "✗"
        print("  %s %-16s %s" % (exists, device, name or "(无名称)"))

    print()
    print("  用 --index N 指定要看哪个")
    return 0


def try_open(index: int, width: int, height: int, fps: int,
             fourcc: str) -> cv2.VideoCapture | None:
    """尝试打开摄像头并抓一帧验证。"""
    device = "/dev/video%d" % index
    if not Path(device).exists():
        print("  ✗ %s 不存在" % device)
        return None

    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        print("  ✗ %s 打不开（可能被别的程序占用）" % device)
        return None

    # 先设格式再设分辨率
    cap.set(cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*fourcc[:4].ljust(4)))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # 预热：丢掉自动曝光还没收敛的前几帧
    for _ in range(8):
        cap.read()

    ok, frame = cap.read()
    if not ok or frame is None:
        print("  ✗ %s 能打开但抓不到帧（可能是元数据节点）" % device)
        cap.release()
        return None

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc_val = int(cap.get(cv2.CAP_PROP_FOURCC))
    fourcc_str = "".join(
        chr((fourcc_val >> (8 * i)) & 0xFF) for i in range(4)
    )
    print("  ✓ %s  %dx%d  FOURCC=%s" % (device, actual_w, actual_h,
                                        fourcc_str))
    return cap


def auto_find_camera(width: int, height: int, fps: int,
                     fourcc: str) -> tuple[cv2.VideoCapture | None, int]:
    """自动尝试各个编号，找第一个能出图的。"""
    print("── 自动查找可用摄像头 ──")
    for index in range(6):
        if not Path("/dev/video%d" % index).exists():
            continue
        cap = try_open(index, width, height, fps, fourcc)
        if cap is not None:
            return cap, index
    return None, -1


# ──────────────────────────────────────────────────────────────────
# 显示
# ──────────────────────────────────────────────────────────────────


def draw_overlay(frame: np.ndarray, fps: float, index: int,
                 paused: bool, frame_count: int) -> np.ndarray:
    """在画面左上角画信息。"""
    canvas = frame.copy()
    height, width = canvas.shape[:2]

    lines = [
        "设备: /dev/video%d" % index,
        "分辨率: %dx%d" % (width, height),
        "帧率: %.1f fps" % fps,
        "帧号: %d" % frame_count,
    ]
    if paused:
        lines.append("** 已暂停 **")
    lines.append("q=退出  s=存图  f=全屏  i=隐藏信息  空格=暂停")

    for i, text in enumerate(lines):
        y = 24 + i * 22
        color = (0, 255, 255) if i < len(lines) - 1 else (200, 200, 200)
        # 先画黑边再画字，保证任何背景下都能看清
        cv2.putText(canvas, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 1, cv2.LINE_AA)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="摄像头查看器（只看画面，不做检测）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true",
                        help="列出所有摄像头")
    parser.add_argument("--index", type=int, default=None,
                        help="摄像头编号（默认自动查找）")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--fourcc", default="MJPG",
                        choices=["MJPG", "YUYV", "YUY2"])
    parser.add_argument("--save", default=None,
                        help="存一张图到指定路径后退出（无显示器时用）")
    parser.add_argument("--save-dir", default=None,
                        help="每隔 --interval 秒自动存一张图到该目录")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="自动存图的间隔秒数")
    parser.add_argument("--count", type=int, default=0,
                        help="自动存图模式下的总张数（0=不限）")
    parser.add_argument("--duration", type=float, default=None,
                        help="运行指定秒数后自动退出")
    args = parser.parse_args()

    print("═" * 62)
    print("  摄像头查看器")
    print("═" * 62)
    print()

    if args.list:
        return list_cameras()

    # ── 打开摄像头 ──────────────────────────────────────────────
    if args.index is not None:
        print("── 打开 /dev/video%d ──" % args.index)
        cap = try_open(args.index, args.width, args.height,
                       args.fps, args.fourcc)
        index = args.index
    else:
        cap, index = auto_find_camera(args.width, args.height,
                                      args.fps, args.fourcc)

    if cap is None:
        print()
        print("❌ 没有找到可用的摄像头。")
        print()
        print("排查步骤：")
        print("  1. python3 examples/camera_view.py --list   看系统识别到哪些设备")
        print("  2. lsusb                                    看 USB 总线上有没有摄像头")
        print("  3. dmesg | tail -20                         看内核日志")
        print("  4. 换个 USB 口重新插")
        print("  5. 确认没有别的程序在用（如另一个预览窗口）")
        return 1

    print()

    # ── 只存一张图（无显示器时用）────────────────────────────────
    if args.save:
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            print("❌ 抓帧失败")
            return 1
        out = Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), frame)
        print("✅ 已保存：%s" % out)
        print("   （传到电脑上或直接在板子上打开看）")
        return 0

    # ── 定时存图模式 ────────────────────────────────────────────
    if args.save_dir:
        save_dir = Path(args.save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        print("── 定时存图模式：每 %.1f 秒一张，存到 %s ──"
              % (args.interval, save_dir))
        print("   按 Ctrl+C 停止")
        print()

        saved = 0
        last_save = 0.0
        started = time.monotonic()
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("⚠️  抓帧失败")
                    time.sleep(0.1)
                    continue

                now = time.monotonic()
                if now - last_save >= args.interval:
                    path = save_dir / ("shot_%03d.jpg" % saved)
                    cv2.imwrite(str(path), frame)
                    mean = frame.reshape(-1, 3).mean(axis=0)
                    print("  [%s] %s   均值 BGR=(%.0f,%.0f,%.0f)"
                          % (time.strftime("%H:%M:%S"), path.name,
                             mean[0], mean[1], mean[2]))
                    saved += 1
                    last_save = now
                    if args.count and saved >= args.count:
                        break

                if args.duration and now - started >= args.duration:
                    break
                time.sleep(0.02)
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C")

        cap.release()
        print()
        print("✅ 共保存 %d 张到 %s" % (saved, save_dir))
        return 0

    # ── 实时窗口模式 ────────────────────────────────────────────
    print("── 实时预览 ──")
    print("   按键：q=退出  s=存图  f=全屏  i=隐藏信息  空格=暂停")
    print("   （用鼠标点一下窗口让它获得焦点，按键才有效）")
    print()

    window = "Camera preview"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)

    snapshot_dir = Path("snapshots")
    fps_display = 0.0
    last_time = time.monotonic()
    frame_count = 0
    paused = False
    show_info = True
    fullscreen = False
    last_frame = None
    started = time.monotonic()

    try:
        while True:
            if not paused:
                ok, frame = cap.read()
                if not ok or frame is None:
                    print("⚠️  抓帧失败，重试中…")
                    time.sleep(0.05)
                    continue
                last_frame = frame
                frame_count += 1

                now = time.monotonic()
                delta = now - last_time
                last_time = now
                if delta > 0:
                    instant = 1.0 / delta
                    fps_display = (instant if fps_display == 0
                                   else 0.9 * fps_display + 0.1 * instant)
            else:
                frame = last_frame
                if frame is None:
                    time.sleep(0.05)
                    continue

            display = (draw_overlay(frame, fps_display, index, paused,
                                    frame_count)
                       if show_info else frame)
            cv2.imshow(window, display)

            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), 27):           # q 或 ESC
                break
            elif key == ord("s"):
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                path = snapshot_dir / ("snap_%03d.jpg" % frame_count)
                cv2.imwrite(str(path), frame)
                print("  已保存 %s" % path)
            elif key == ord("f"):
                fullscreen = not fullscreen
                cv2.setWindowProperty(
                    window, cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN if fullscreen
                    else cv2.WINDOW_NORMAL)
            elif key == ord("i"):
                show_info = not show_info
            elif key == ord(" "):
                paused = not paused
                print("  %s" % ("已暂停" if paused else "已继续"))

            if args.duration and time.monotonic() - started >= args.duration:
                break

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C")
    finally:
        cap.release()
        cv2.destroyAllWindows()

    elapsed = time.monotonic() - started
    print()
    print("─" * 62)
    print("  共显示 %d 帧，用时 %.1f 秒，平均 %.1f fps"
          % (frame_count, elapsed, frame_count / max(0.001, elapsed)))
    print("─" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
