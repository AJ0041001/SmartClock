#!/usr/bin/env python3
"""演示"色块出界时中心算偏"这个问题，以及双 ROI 是怎么修好它的。

为什么需要这个脚本
------------------
用户遇到的现象是：**挡板能贴边，但到不了最边上**，紧贴左右边界落下的
星星接不到。根因很反直觉 —— 检测的那一步就把中心算错了：

    卡片中心移到映射区域 A 的边界时，卡片有一半落在 A 之外。
    如果"检测区域"和 A 是同一个框，模型只能看到剩下的那一半，
    算出来的中心是**可见部分的中心**，而不是卡片真实中心。

这个脚本用合成图像把这件事算清楚：同一个卡片放在同一个位置，
只改"检测区域"的大小，看算出来的中心差多少。

它模拟的正是 YOLO 那条路径：**先按检测区域裁剪 → 在裁剪图里检测
→ 把中心换算回整图坐标**（见 src/card_detector.py 的 detect_all）。

用法::

    python3 scripts/demo_edge_center.py
    python3 scripts/demo_edge_center.py --card-width 90 --card-height 170
    python3 scripts/demo_edge_center.py --margin 40      # 故意用不足的边距
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.mapper import Roi  # noqa: E402

# 与仓库 config.yaml 一致的现场参数
FRAME_W, FRAME_H = 640, 480
DEFAULT_ROI_A = Roi(156, 20, 326, 428)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_edge_center.py",
        description="演示双 ROI 对边缘中心精度的影响",
    )
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "W", "H"),
                        default=None, help="映射区域 A，默认用 config.yaml 的值")
    parser.add_argument("--margin", type=int, default=60,
                        help="检测区域 B 比 A 每边大多少像素（默认 60）")
    parser.add_argument("--card-width", type=int, default=90,
                        help="卡片在画面里的宽度（默认 90）")
    parser.add_argument("--card-height", type=int, default=170)
    parser.add_argument("--steps", type=int, default=7,
                        help="每个边界上采样几个位置")
    return parser.parse_args()


def centre_of_visible_block(frame: np.ndarray,
                            box: tuple[int, int, int, int]
                            ) -> tuple[float, float] | None:
    """在 ``box`` 区域内找红色卡片，返回换算回整图坐标的中心。

    这就是 YOLO 路径的简化版：裁剪 → 检测（这里用阈值+质心代替）
    → 加回裁剪偏移。
    """
    rx, ry, rw, rh = box
    rx = max(0, min(rx, FRAME_W - 1))
    ry = max(0, min(ry, FRAME_H - 1))
    rw = max(1, min(rw, FRAME_W - rx))
    rh = max(1, min(rh, FRAME_H - ry))
    crop = frame[ry:ry + rh, rx:rx + rw]

    # "检测"：红色像素的质心（黑底红卡，阈值足够可靠）
    b, g, r = crop[:, :, 0].astype(int), crop[:, :, 1].astype(int), \
        crop[:, :, 2].astype(int)
    mask = (r > 120) & (r - g > 60) & (r - b > 60)
    if not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    return (float(xs.mean()) + rx, float(ys.mean()) + ry)


def render_frame(card_centre: tuple[int, int],
                 card_w: int, card_h: int) -> np.ndarray:
    frame = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    cx, cy = card_centre
    cv2.rectangle(frame,
                  (cx - card_w // 2, cy - card_h // 2),
                  (cx + card_w // 2, cy + card_h // 2),
                  (40, 40, 210), -1)
    return frame


def main() -> int:
    args = parse_args()
    roi_a = Roi(*args.roi) if args.roi else DEFAULT_ROI_A
    margin = max(0, args.margin)

    # B = A 向外扩 margin，并夹到画面内（与 RoiEditor 的算法一致）
    w = min(roi_a.w + 2 * margin, FRAME_W)
    h = min(roi_a.h + 2 * margin, FRAME_H)
    x = max(0, min(roi_a.x - margin, FRAME_W - w))
    y = max(0, min(roi_a.y - margin, FRAME_H - h))
    roi_b = Roi(x, y, w, h)

    card_w, card_h = args.card_width, args.card_height
    half_w = card_w // 2

    print("=" * 74)
    print("双 ROI 对边缘中心精度的影响")
    print("=" * 74)
    print(f"  画面      : {FRAME_W}x{FRAME_H}")
    print(f"  映射区 A  : ({roi_a.x},{roi_a.y}) {roi_a.w}x{roi_a.h}")
    print(f"  检测区 B  : ({roi_b.x},{roi_b.y}) {roi_b.w}x{roi_b.h}"
          f"   （A 每边外扩 {margin}px）")
    print(f"  卡片尺寸  : {card_w}x{card_h}")
    print()
    print(f"  ⚠️ 要让卡片在 A 边界处仍完整落在 B 内，边距必须 > 卡片半宽 "
          f"{half_w}px")
    print(f"     当前边距 {margin}px → "
          f"{'✅ 够用' if margin >= half_w else '❌ 不够，卡片会被裁掉一角'}")
    print()

    # 边界采样：A 的左右边界 + 往外一点（真实比赛时卡片会略微出界）
    positions: list[tuple[str, float]] = []
    for step in range(args.steps):
        ratio = step / max(1, args.steps - 1)
        positions.append(("左边", roi_a.x + ratio * (roi_a.w * 0.5)))
        positions.append(("右边", roi_a.x + roi_a.w - ratio * (roi_a.w * 0.5)))
    # 再加两组"明确出界"的位置
    for label, cx in (("左出界", roi_a.x - half_w // 2),
                      ("右出界", roi_a.x + roi_a.w + half_w // 2)):
        positions.append((label, cx))

    cy = roi_a.y + roi_a.h // 2

    def measure(box: Roi, cx_true: float) -> float | None:
        frame = render_frame((int(round(cx_true)), cy), card_w, card_h)
        found = centre_of_visible_block(frame, box.as_tuple())
        if found is None:
            return None
        return abs(found[0] - cx_true)

    print("─" * 74)
    print(f"  {'位置':<8}{'卡片真实中心X':>14}{'B=A 误差':>12}"
          f"{'B=A+边距 误差':>16}")
    print("─" * 74)

    err_same: list[float] = []
    err_dual: list[float] = []
    for label, cx_true in positions:
        e_same = measure(roi_a, cx_true)
        e_dual = measure(roi_b, cx_true)
        if e_same is not None:
            err_same.append(e_same)
        if e_dual is not None:
            err_dual.append(e_dual)
        print(f"  {label:<8}{cx_true:>14.1f}"
              f"{(f'{e_same:.1f} px' if e_same is not None else '未检出'):>12}"
              f"{(f'{e_dual:.1f} px' if e_dual is not None else '未检出'):>16}")

    print("─" * 74)
    if err_same:
        print(f"  B=A       ：最大误差 {max(err_same):>6.1f} px  "
              f"平均 {sum(err_same) / len(err_same):>5.1f} px")
    if err_dual:
        print(f"  B=A+边距  ：最大误差 {max(err_dual):>6.1f} px  "
              f"平均 {sum(err_dual) / len(err_dual):>5.1f} px")
    print()

    # 换算成游戏坐标的偏差，更有体感
    game_span = 389 - 35            # 挡板中心有效范围 35..389
    for name, errors in (("B=A", err_same), ("B=A+边距", err_dual)):
        if not errors:
            continue
        worst = max(errors)
        game_px = worst / roi_a.w * game_span
        print(f"  {name:<9} 最坏情况下，挡板离墙还差约 {game_px:.0f} "
              f"游戏像素")
    print()

    if err_same and err_dual:
        improved = max(err_same) - max(err_dual)
        if improved > 1:
            print(f"✅ 双 ROI 把最坏情况下的中心误差从 {max(err_same):.1f}px "
                  f"降到 {max(err_dual):.1f}px")
            print("   这正是「挡板能贴到最边上、接得到边缘星星」的前提。")
        elif margin >= half_w:
            print("ℹ️  当前 A 的边界离画面边缘较远，卡片没有真的出界，")
            print("   两种配置差别不大。把卡片继续往外移就能看出差异。")
        else:
            print("❌ 边距不足，卡片贴边时仍被裁掉一角 ——")
            print(f"   把 --margin 提到 {half_w + 10} 以上再试。")
    print("=" * 74)

    # 顺带把"可视化对照图"存下来，方便肉眼确认
    out_dir = Path("assets/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    edge_x = roi_a.x
    frame = render_frame((edge_x, cy), card_w, card_h)
    for name, box, color in (("B=A", roi_a, (0, 0, 255)),
                             ("B=A+margin", roi_b, (120, 255, 120))):
        canvas = frame.copy()
        cv2.rectangle(canvas, (box.x, box.y),
                      (box.x + box.w, box.y + box.h), color, 2)
        found = centre_of_visible_block(frame, box.as_tuple())
        if found is not None:
            cv2.drawMarker(canvas, (int(found[0]), int(found[1])),
                           (0, 255, 255), cv2.MARKER_CROSS, 26, 2)
        cv2.drawMarker(canvas, (edge_x, cy), (255, 255, 255),
                       cv2.MARKER_TILTED_CROSS, 26, 2)
        path = out_dir / f"edge_center_{name.replace('=', '').replace('+', '_')}.png"
        cv2.imwrite(str(path), canvas)
    print(f"对照图已保存到 {out_dir}/edge_center_*.png")
    print("  白色叉 = 卡片真实中心，黄色叉 = 检测算出的中心")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
