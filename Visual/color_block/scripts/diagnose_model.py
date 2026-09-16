#!/usr/bin/env python3
"""模型诊断 —— 一次性验证各种预处理假设，定位"框为什么不对"。

为什么需要这个
--------------
模型输出的框与实际目标严重不符时，可能的原因有很多：

    · 输入预处理方式不对（letterbox vs 拉伸）
    · 颜色通道顺序不对（RGB vs BGR）
    · 框参数格式不对（xywh vs xyxy）
    · 归一化方式不对（/255 vs 不除）
    · 模型本身就没训好

逐个猜太慢，而且容易像之前那样"猜错了还以为修好了"。
这个脚本把**所有主流假设组合**一次跑完，用同一张图对比结果：

    预处理 × 颜色顺序 的组合，每种都报告：
      · 最高分候选的原始数值
      · 解码后的框
      · 与真值的 IoU
      · 中心偏差

**哪种组合的 IoU 高，哪种就是对的。**

用法::

    # 用测试集第一张图，跑遍所有组合
    python3 scripts/diagnose_model.py

    # 指定图片和真值
    python3 scripts/diagnose_model.py --image xxx.jpg --gt 306,239,359,309

    # 也可以只跑某一种组合
    python3 scripts/diagnose_model.py --variant letterbox_rgb
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.cli import banner  # noqa: E402
from src.dataset import load_split  # noqa: E402
from src.rknn_ctypes import RKNNLite, RknnError  # noqa: E402
from src.card_detector import default_model_path  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "redcard.yolo26"
PAD_VALUE = (114, 114, 114)


@dataclass
class VariantResult:
    """一种预处理组合的结果。"""

    name: str
    description: str
    top_conf: float
    raw_box: tuple[float, float, float, float]
    decoded_xyxy: tuple[float, float, float, float] | None
    iou: float
    center_error: float | None
    error: str = ""


# ──────────────────────────────────────────────────────────────────────
# 各种预处理组合
# ──────────────────────────────────────────────────────────────────────


def preprocess_letterbox(image: np.ndarray, size: int) -> tuple[np.ndarray, dict]:
    """letterbox：等比缩放 + 灰边补齐（ultralytics 默认）。"""
    h, w = image.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = cv2.resize(image, (nw, nh))
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas = cv2.copyMakeBorder(
        resized, top, size - nh - top, left, size - nw - left,
        cv2.BORDER_CONSTANT, value=PAD_VALUE,
    )
    return canvas, {"scale": scale, "top": top, "left": left}


def preprocess_stretch(image: np.ndarray, size: int) -> tuple[np.ndarray, dict]:
    """直接拉伸到正方形（会改变长宽比）。"""
    h, w = image.shape[:2]
    canvas = cv2.resize(image, (size, size))
    return canvas, {"scale_x": size / w, "scale_y": size / h,
                    "top": 0, "left": 0}


def preprocess_center_crop(image: np.ndarray, size: int) -> tuple[np.ndarray, dict]:
    """中心裁剪成正方形再缩放（不补齐，而是裁掉两边）。"""
    h, w = image.shape[:2]
    side = min(h, w)
    y0 = (h - side) // 2
    x0 = (w - side) // 2
    cropped = image[y0:y0 + side, x0:x0 + side]
    canvas = cv2.resize(cropped, (size, size))
    return canvas, {"scale": size / side, "crop_x": x0, "crop_y": y0,
                    "top": 0, "left": 0, "mode": "crop"}


PREPROCESSORS = {
    "letterbox": preprocess_letterbox,
    "stretch": preprocess_stretch,
    "crop": preprocess_center_crop,
}


def decode_variant(
    output: np.ndarray,
    geom: dict,
    orig_shape: tuple[int, int],
    size: int,
    box_format: str,
) -> tuple[tuple[float, float, float, float] | None, float, tuple[float, float, float, float]]:
    """按给定框格式解码最高分候选。

    返回 (解码后的 xyxy 或 None, 置信度, 原始框参数)
    """
    predictions = output[0].T
    scores = predictions[:, 4]
    best = int(np.argmax(scores))
    raw = tuple(float(v) for v in predictions[best][:4])
    conf = float(scores[best])

    cx, cy, w, h = raw
    if box_format == "xywh":
        box = (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
    else:
        box = raw

    # 还原到原图坐标
    if geom.get("mode") == "crop":
        scale = geom["scale"]
        x1, y1, x2, y2 = box
        x1 = x1 / scale + geom["crop_x"]
        x2 = x2 / scale + geom["crop_x"]
        y1 = y1 / scale + geom["crop_y"]
        y2 = y2 / scale + geom["crop_y"]
    elif "scale_x" in geom:      # stretch
        x1, y1, x2, y2 = box
        x1 /= geom["scale_x"]; x2 /= geom["scale_x"]
        y1 /= geom["scale_y"]; y2 /= geom["scale_y"]
    else:                        # letterbox
        scale = geom["scale"]
        x1, y1, x2, y2 = box
        x1 = (x1 - geom["left"]) / scale
        x2 = (x2 - geom["left"]) / scale
        y1 = (y1 - geom["top"]) / scale
        y2 = (y2 - geom["top"]) / scale

    # 裁剪到画面内
    height, width = orig_shape
    x1 = max(0.0, min(x1, width - 1))
    x2 = max(0.0, min(x2, width - 1))
    y1 = max(0.0, min(y1, height - 1))
    y2 = max(0.0, min(y2, height - 1))

    if x2 <= x1 or y2 <= y1:
        return None, conf, raw
    return (x1, y1, x2, y2), conf, raw


def compute_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


# ──────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="diagnose_model.py",
        description="一次性验证多种预处理假设，定位框错位的原因",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--split", default="test")
    parser.add_argument("--index", type=int, default=0,
                        help="用第几张测试图（默认第 1 张）")
    parser.add_argument("--image", default=None,
                        help="直接指定图片路径（优先于 --index）")
    parser.add_argument("--gt", default=None,
                        help="真值框 x1,y1,x2,y2（不指定则从数据集标注读取）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--variant", default=None,
                        help="只跑指定组合，如 letterbox_rgb")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print(banner("模型诊断", "一次跑遍所有预处理假设，看哪种 IoU 最高"))
    print()

    # ── 准备图片与真值 ──────────────────────────────────────────────
    gt_box: tuple[float, float, float, float] | None = None
    image_path: Path | None = None

    if args.image:
        image_path = Path(args.image)
    else:
        samples = load_split(Path(args.dataset), args.split)
        if not samples:
            print("❌ 数据集为空", file=sys.stderr)
            return 1
        index = max(0, min(args.index, len(samples) - 1))
        sample = samples[index]
        image_path = sample.image_path
        if sample.boxes:
            gt_box = sample.boxes[0].bbox

    if gt_box is None and args.gt:
        gt_box = tuple(float(v) for v in args.gt.split(","))  # type: ignore

    if image_path is None or not image_path.exists():
        print(f"❌ 图片不存在：{image_path}", file=sys.stderr)
        return 1

    image = cv2.imread(str(image_path))
    if image is None:
        print(f"❌ 无法读取图片：{image_path}", file=sys.stderr)
        return 1

    height, width = image.shape[:2]
    print(f"图片   : {image_path.name}")
    print(f"尺寸   : {width}x{height}")
    print(f"真值框 : {gt_box if gt_box else '（未知，只报告原始输出）'}")
    if gt_box:
        gw, gh = gt_box[2] - gt_box[0], gt_box[3] - gt_box[1]
        print(f"         尺寸 {gw:.0f}x{gh:.0f}  "
              f"中心 ({(gt_box[0]+gt_box[2])/2:.0f},{(gt_box[1]+gt_box[3])/2:.0f})")
    print()

    # ── 初始化模型 ──────────────────────────────────────────────────
    model_path = Path(args.model) if args.model else default_model_path()
    rknn = RKNNLite(verbose=False)
    try:
        rknn.load_rknn(model_path)
        rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0_1_2)
    except RknnError as exc:
        print(f"❌ 模型初始化失败：{exc}", file=sys.stderr)
        return 2

    # 硬件确认：先跑一次确保能出结果
    print(f"模型   : {model_path.name}")
    print()

    # ── 遍历所有组合 ────────────────────────────────────────────────
    variants = []
    for prep_name, prep in PREPROCESSORS.items():
        for color in ("rgb", "bgr"):
            variants.append((f"{prep_name}_{color}", prep_name, color))

    if args.variant:
        variants = [v for v in variants if v[0] == args.variant]
        if not variants:
            print(f"❌ 未知组合 {args.variant}", file=sys.stderr)
            return 1

    results: list[VariantResult] = []

    print("═" * 92)
    print(f"{'组合':<18} {'置信度':>7}  {'原始框参数':<34} "
          f"{'解码后尺寸':>12} {'IoU':>7}")
    print("═" * 92)

    try:
        for name, prep_name, color in variants:
            prep = PREPROCESSORS[prep_name]
            try:
                canvas, geom = prep(image, args.input_size)
                if color == "rgb":
                    canvas = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
                batch = np.ascontiguousarray(canvas[None, ...])
                output = rknn.inference(inputs=[batch], data_format="nhwc")[0]
            except Exception as exc:
                print(f"{name:<18} ❌ {exc}")
                continue

            # 两种框格式都试
            for fmt in ("xywh", "xyxy"):
                box, conf, raw = decode_variant(
                    output, geom, (height, width), args.input_size, fmt
                )
                if box is None:
                    continue
                bw, bh = box[2] - box[0], box[3] - box[1]
                iou_value = compute_iou(box, gt_box) if gt_box else 0.0

                label = f"{name}" + ("" if fmt == "xywh" else " [xyxy]")
                print(f"{label:<18} {conf:>7.4f}  "
                      f"({raw[0]:>7.1f},{raw[1]:>7.1f},"
                      f"{raw[2]:>7.1f},{raw[3]:>7.1f})  "
                      f"{bw:>5.0f}x{bh:<5.0f} {iou_value:>7.3f}")

                results.append(VariantResult(
                    name=label,
                    description=f"{prep_name}/{color}/{fmt}",
                    top_conf=conf,
                    raw_box=raw,
                    decoded_xyxy=box,
                    iou=iou_value,
                    center_error=(
                        ((box[0] + box[2]) / 2 - (gt_box[0] + gt_box[2]) / 2) ** 2
                        + ((box[1] + box[3]) / 2 - (gt_box[1] + gt_box[3]) / 2) ** 2
                    ) ** 0.5 if gt_box else None,
                ))
    finally:
        rknn.release()

    # ── 结论 ────────────────────────────────────────────────────────
    print("═" * 92)
    print()

    if not results:
        print("❌ 所有组合都失败了", file=sys.stderr)
        return 3

    best = max(results, key=lambda r: r.iou)
    print("【结论】")
    if gt_box is None:
        print("  未提供真值，无法判定。请用数据集里的图片或 --gt 指定真值框。")
    elif best.iou >= 0.5:
        print(f"  ✅ 找到匹配的组合：**{best.name}**   IoU={best.iou:.3f}")
        print(f"     说明当前实现用错了预处理方式，应改用：{best.description}")
        print(f"     中心偏差 {best.center_error:.1f} px")
    else:
        print(f"  ❌ 所有组合的 IoU 都低于 0.5（最好的是 {best.name}，"
              f"IoU={best.iou:.3f}）")
        print()
        print("     这说明问题**不在预处理方式**上。可能的原因：")
        print("       · 模型文件与数据集不匹配（用别的数据训的？）")
        print("       · RKNN 转换过程出错（建议重新转换并核对）")
        print("       · 模型本身训练不充分 / 欠拟合")
        print()
        print("     建议下一步：")
        print("       1. 用 ONNX 模型在 PC 上跑同一张图，对比输出")
        print("          （若 ONNX 正确而 RKNN 不对，就是转换环节的问题）")
        print("       2. 用训练集里的图片测试（模型见过的数据）")
        print("          （若训练集也检不对，说明模型根本没学会）")
        print("       3. 检查 best.pt 的训练日志/指标（mAP 是多少？）")

    # 原始输出对比：让用户能直接看到模型吐出的数字
    print()
    print("【最高分候选的原始数值对照】")
    print(f"  {'组合':<24} {'cx/cy (或 x1/y1)':>24} {'w/h (或 x2/y2)':>24}")
    for r in results:
        rb = r.raw_box
        print(f"  {r.name:<24} ({rb[0]:>9.1f},{rb[1]:>9.1f})      "
              f"({rb[2]:>9.1f},{rb[3]:>9.1f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
