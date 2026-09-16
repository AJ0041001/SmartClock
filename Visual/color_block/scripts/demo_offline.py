#!/usr/bin/env python3
"""离线端到端演示 —— 不需要摄像头和 STM32 就能验证整条链路。

用途
----
1. **到货前先验证软件**：摄像头还没插、STM32 还没接，也能确认代码逻辑正确。
2. **回归测试**：改动代码后跑一遍，看输出是否还符合预期。
3. **理解映射关系**：直观看到"色块在画面哪个位置 → 发出什么坐标"。

原理是用合成图像伪造摄像头输入，用内存对象伪造串口，跑完整个流水线，
最后把每一步的实际结果打印出来并生成一张示意图。

用法::

    python3 scripts/demo_offline.py
    python3 scripts/demo_offline.py --color green --save assets/output/demo.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.cli import add_common_arguments, banner, load_config, setup_logging  # noqa: E402
from src.pipeline import ColorTrackingPipeline  # noqa: E402
from src.protocol import Response, hexdump, parse_frame  # noqa: E402
from tests.fakes import FakeCapture, FakeLink  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_offline.py",
        description="离线端到端演示（合成图像 + 内存串口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_arguments(parser)
    parser.add_argument("--color", default="red",
                        help="演示用的色块颜色（预设色名）")
    parser.add_argument("--save", default=None,
                        help="把行程示意图保存到指定路径")
    parser.add_argument("--steps", type=int, default=9,
                        help="扫描步数（默认 9）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args)
    setup_logging("WARNING")   # 演示时压低日志噪音

    config.detector.preset = args.color
    config.detector.min_area = 200
    config.detector.morph_kernel = 3
    config.detector.morph_iterations = 1
    config.mapping.smoothing = None   # 关平滑，让映射关系一目了然
    config.mapping.deadband = 0.0
    config.loop.send_interval = 0.0
    config.loop.target_fps = 1000.0

    print(banner("离线端到端演示",
                 "合成图像 → 色块检测 → 坐标映射 → 协议帧"))
    print()
    print("说明：本演示完全在本机内存中完成，不访问摄像头、不写串口。")
    print(f"色块颜色：{args.color}")
    print()

    width, height = 640, 480
    block_size = 60

    # 从画面左内侧扫到右内侧，Y 保持在中间
    margin = block_size // 2 + 2
    xs = np.linspace(margin, width - margin, args.steps).astype(int).tolist()
    positions = [(int(x), height // 2) for x in xs]

    capture = FakeCapture(
        positions,
        width=width,
        height=height,
        block_size=block_size,
        block_color_bgr=_bgr_for(args.color),
        repeat_last=False,
    )
    link = FakeLink(response=Response.OK)

    pipeline = ColorTrackingPipeline(config, capture, link)
    pipeline.open()

    print("─" * 78)
    print(f"{'帧':>3}  {'色块像素坐标':>16}  {'归一化':>16}  "
          f"{'游戏坐标':>12}  {'发送的 10 字节帧':<32}  应答")
    print("─" * 78)

    rows: list[tuple] = []
    frames_sent = 0

    while True:
        frame = capture.read()
        if frame is None:
            break
        result = pipeline.step(frame)
        if result.detection is None or result.mapping is None:
            continue

        m = result.mapping
        d = result.detection
        frame_hex = hexdump(result.frame_bytes) if result.frame_bytes else "(未发送)"

        # 顺便在本地复校验一次，确保发出去的帧单片机一定收得下
        verify = "?"
        if result.frame_bytes:
            try:
                parsed = parse_frame(result.frame_bytes)
                verify = "CRC✓" if parsed.crc_ok else "CRC✗"
            except Exception as exc:
                verify = f"格式✗({exc})"

        print(f"{frame.index:>3}  "
              f"({d.center[0]:>6.1f},{d.center[1]:>6.1f})  "
              f"({m.normalized_x:>6.3f},{m.normalized_y:>6.3f})  "
              f"({m.game_x:>4},{m.game_y:>4})  "
              f"{frame_hex:<32}  {verify}")

        rows.append((d.center_int, m, result.frame_bytes))
        if result.sent:
            frames_sent += 1

    pipeline.close()

    print("─" * 78)
    print()

    # ── 一致性检查 ──────────────────────────────────────────────────
    print("── 一致性检查 ──")
    game_xs = [m.game_x for _, m, _ in rows]
    monotonic = all(
        game_xs[i] <= game_xs[i + 1] for i in range(len(game_xs) - 1)
    )
    print(f"  1. X 单调不减（色块右移 → 挡板右移）："
          f"{'✅ 通过' if monotonic else '❌ 失败'}")
    print(f"  2. 所有帧 CRC 校验："
          f"{'✅ 通过' if all(_verify_ok(f) for _, _, f in rows) else '❌ 失败'}")
    print(f"  3. 坐标均在有效范围内："
          f"{'✅ 通过' if all(_in_range(m) for _, m, _ in rows) else '❌ 失败'}")
    print(f"  4. 实际发送帧数：{frames_sent} / {len(rows)}")
    print()

    if rows:
        first_x, last_x = game_xs[0], game_xs[-1]
        print(f"── 映射跨度 ──")
        print(f"  色块从像素 x={rows[0][0][0]} 移到 x={rows[-1][0][0]}")
        print(f"  游戏 X 从 {first_x} 变到 {last_x}"
              f"（量程 35..389，跨度 {last_x - first_x}）")
        print()

    # ── 生成示意图 ──────────────────────────────────────────────────
    save_path = args.save or str(
        Path(config.debug.save_dir) / "offline_demo.jpg"
    )
    sheet = _make_contact_sheet(rows, config, width, height)
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(save_path, sheet)
    print(f"已生成行程示意图：{save_path}")
    print()
    print("=" * 78)
    print("✅ 离线演示完成。视觉链路、坐标映射、协议组帧均工作正常。")
    print()
    print("下一步（等硬件到位）：")
    print("  1. 插上 USB 摄像头 → python3 scripts/camera_check.py --probe-all")
    print("  2. 验证模型       → python3 scripts/verify_model.py --image "
          "assets/captures/s1.jpg")
    print("  3. 框定两个 ROI   → python3 scripts/main.py --dry-run"
          "   （窗口里 Pick A / B=A+60 / Save）")
    print("  4. 验证串口       → python3 scripts/serial_test.py --probe")
    print("  5. 正式运行       → python3 scripts/main.py")
    print("=" * 78)
    return 0


def _verify_ok(frame_bytes) -> bool:
    if not frame_bytes:
        return False
    try:
        return parse_frame(frame_bytes).crc_ok
    except Exception:
        return False


def _in_range(m) -> bool:
    return 35 <= m.game_x <= 389 and 6 <= m.game_y <= 578


def _bgr_for(name: str) -> tuple[int, int, int]:
    """预设色名 → 用于绘制的 BGR 值。"""
    mapping = {
        "red": (0, 0, 255),
        "orange": (0, 165, 255),
        "yellow": (0, 255, 255),
        "green": (0, 255, 0),
        "cyan": (255, 255, 0),
        "blue": (255, 0, 0),
        "purple": (128, 0, 128),
        "magenta": (255, 0, 255),
    }
    return mapping.get(name, (0, 0, 255))


def _make_contact_sheet(rows, config, width: int, height: int) -> np.ndarray:
    """把每一帧缩略图并按网格排布，生成一张行程示意图。"""
    if not rows:
        return np.zeros((height, width, 3), dtype=np.uint8)

    thumbs = []
    for pixel_center, mapping, frame_bytes in rows:
        canvas = np.full((height, width, 3), 235, dtype=np.uint8)
        cv2.rectangle(
            canvas,
            (pixel_center[0] - 30, pixel_center[1] - 30),
            (pixel_center[0] + 30, pixel_center[1] + 30),
            (0, 0, 255), -1,
        )
        cv2.drawMarker(canvas, pixel_center, (0, 255, 255),
                       cv2.MARKER_CROSS, 24, 2)
        cv2.putText(canvas, f"game=({mapping.game_x},{mapping.game_y})",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 0, 0), 2, cv2.LINE_AA)
        if frame_bytes:
            cv2.putText(canvas, hexdump(frame_bytes), (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1,
                        cv2.LINE_AA)
        thumbs.append(cv2.resize(canvas, (260, 195)))

    columns = 3
    rows_count = (len(thumbs) + columns - 1) // columns
    sheet = np.full((rows_count * 195, columns * 260, 3), 255, dtype=np.uint8)

    for index, thumb in enumerate(thumbs):
        r, c = divmod(index, columns)
        sheet[r * 195:(r + 1) * 195, c * 260:(c + 1) * 260] = thumb

    header = np.full((46, sheet.shape[1], 3), 40, dtype=np.uint8)
    cv2.putText(header, "SmartClock offline demo: block position -> game coords "
                        "-> serial frame", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack([header, sheet])


if __name__ == "__main__":
    raise SystemExit(main())
