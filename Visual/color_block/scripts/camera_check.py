#!/usr/bin/env python3
"""摄像头诊断工具 —— 确认 USB 摄像头是否被系统识别并能正常出图。

这个脚本解决一个高频问题："程序报错说没有摄像头，到底是没插好，
还是被占用了，还是驱动不认？"把它跑一遍就有答案。

用法::

    python3 scripts/camera_check.py                 # 检查默认设备
    python3 scripts/camera_check.py --index 2       # 检查 /dev/video2
    python3 scripts/camera_check.py --probe-all     # 逐个试所有节点
    python3 scripts/camera_check.py --save shot.jpg # 抓一张图存下来
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from src.camera import (  # noqa: E402
    CameraError,
    UsbCamera,
    describe_environment,
    list_video_devices,
)
from src.cli import banner  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="camera_check.py",
        description="USB 摄像头诊断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--index", type=int, default=0,
                        help="要检查的设备号 /dev/videoN（默认 0）")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--probe-all", action="store_true",
                        help="逐个尝试所有视频节点，找出哪个能出图")
    parser.add_argument("--save", default=None,
                        help="把抓到的第一帧保存到指定路径")
    parser.add_argument("--skip-usb", action="store_true",
                        help="跳过 lsusb 检查")
    return parser.parse_args()


def show_usb_devices() -> None:
    """打印 USB 总线上的设备，用于确认摄像头在不在。"""
    print("── USB 总线设备 ──")
    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                print(f"  {line}")
            lowered = result.stdout.lower()
            hints = [
                ("camera", "摄像头"),
                ("webcam", "摄像头"),
                ("uvc", "UVC 摄像头"),
                ("imaging", "摄像头"),
                ("video", "视频设备"),
            ]
            found = [zh for key, zh in hints if key in lowered]
            if found:
                print(f"  → 检测到疑似摄像头设备：{found[0]}")
            else:
                print("  → 未在 USB 总线上发现明显的摄像头条目")
                print("     （若摄像头通过 USB Hub 接入，名称可能不明显，"
                      "以 v4l2 结果为准）")
        else:
            print("  lsusb 无输出")
    except FileNotFoundError:
        print("  lsusb 未安装（可 apt install usbutils）")
    except Exception as exc:
        print(f"  执行失败：{exc}")
    print()


def show_v4l2_devices() -> None:
    """用 v4l2-ctl 交叉验证（v4l-utils 已预装）。"""
    print("── v4l2-ctl 交叉验证 ──")
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=10,
        )
        text = (result.stdout or "").strip()
        print(f"  {text}" if text else "  （无输出）")
    except FileNotFoundError:
        print("  v4l2-ctl 未安装（可 apt install v4l-utils）")
    except Exception as exc:
        print(f"  执行失败：{exc}")
    print()


def try_device(index: int, args: argparse.Namespace) -> bool:
    """尝试打开并抓帧，返回是否成功。"""
    print(f"── 尝试 /dev/video{index}  "
          f"{args.width}x{args.height}@{args.fps} {args.fourcc} ──")
    camera = UsbCamera(
        index=index,
        width=args.width,
        height=args.height,
        fps=args.fps,
        fourcc=args.fourcc,
    )
    try:
        camera.open()
    except CameraError as exc:
        print(f"  ✗ 打开失败：{exc}")
        print()
        return False

    try:
        print(f"  实际参数：{camera.summary()}")
        frame = camera.read()
        if frame is None:
            print("  ✗ 打开成功但抓不到帧")
            print("    可能原因：该节点是元数据节点而非采集口；"
                  "或摄像头被其它程序占用。")
            print()
            return False

        image = frame.image
        print(f"  ✓ 抓帧成功：{image.shape[1]}x{image.shape[0]}, "
              f"dtype={image.dtype}")
        mean = image.reshape(-1, 3).mean(axis=0)
        print(f"  画面均值 BGR = "
              f"({mean[0]:.0f}, {mean[1]:.0f}, {mean[2]:.0f})")
        if mean.max() < 5:
            print("  ⚠️  画面几乎全黑 —— 可能是镜头盖没摘、曝光未收敛，"
                  "或该节点不对")

        if args.save:
            path = Path(args.save)
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), image)
            print(f"  已保存到：{path}")

        # 顺便统计一下实测帧率，判断 USB 带宽是否吃紧
        import time
        start = time.monotonic()
        got = 0
        while time.monotonic() - start < 1.0 and got < 60:
            if camera.read(retries=1) is not None:
                got += 1
        elapsed = time.monotonic() - start
        print(f"  实测帧率：{got / elapsed:.1f} fps")
        print()
        return True
    finally:
        camera.close()


def main() -> int:
    args = parse_args()

    print(banner("SmartClock 摄像头诊断", "确认 USB 摄像头是否可用"))
    print()

    print(describe_environment())
    print()

    if not args.skip_usb:
        show_usb_devices()
    show_v4l2_devices()

    devices = list_video_devices()
    if not devices:
        print("❌ 系统当前没有任何视频设备。")
        print()
        print("请依次确认：")
        print("  1. USB 摄像头插好了吗？（重新插拔听系统提示音）")
        print("  2. 换一个 USB 口试试（优先用板子上的 USB 3.0 蓝口）")
        print("  3. 执行 `dmesg | tail -30` 看内核有没有识别到新设备")
        print("  4. 摄像头本身在别的电脑上能用吗？")
        return 1

    if args.probe_all:
        print(f"── 逐个探测 {len(devices)} 个节点 ──")
        print()
        success = []
        for info in devices:
            if try_device(info.index, args):
                success.append(info.index)
        print("═" * 66)
        if success:
            print(f"✅ 可用节点：{', '.join(f'/dev/video{i}' for i in success)}")
            print(f"   在 config.yaml 里设置 camera.index = {success[0]}")
            return 0
        print("❌ 所有节点都无法出图。")
        return 1

    ok = try_device(args.index, args)
    print("═" * 66)
    if ok:
        print(f"✅ /dev/video{args.index} 工作正常。")
        return 0
    print(f"❌ /dev/video{args.index} 不可用，"
          f"试试 --probe-all 找出正确的节点。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
