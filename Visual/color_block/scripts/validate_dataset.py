#!/usr/bin/env python3
"""用带标注的数据集定量评估模型 —— 给出精确率/召回率/IoU。

为什么需要这个
--------------
"看图感觉不错"不能算验证。这个脚本在**有标注真值**的测试集上跑一遍，
算出量化指标：

    · **精确率 Precision** —— 检出的框里有多少是真的（低 = 误检多）
    · **召回率 Recall**    —— 真值框里有多少被检出（低 = 漏检多）
    · **IoU**              —— 检出的框和真值贴合得多好
    · **中心偏差**          —— 直接反映"算出来的中心坐标准不准"
                                        ↑ 这正是本项目最关心的

数据集目录结构（Roboflow 导出格式）::

    redcard.yolo26/
    ├── data.yaml
    ├── train/{images,labels}
    ├── valid/{images,labels}
    └── test/{images,labels}

用法::

    # 在测试集上评估（默认）
    python3 scripts/validate_dataset.py

    # 评估验证集
    python3 scripts/validate_dataset.py --split valid

    # 只跑前 5 张看看
    python3 scripts/validate_dataset.py --limit 5

    # 调整 IoU 判定阈值（默认 0.5）
    python3 scripts/validate_dataset.py --iou-threshold 0.3

    # 不用 NPU，用 HSV 引擎做对照
    python3 scripts/validate_dataset.py --engine hsv --preset red
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.cli import banner  # noqa: E402
from src.dataset import (  # noqa: E402
    ImageResult,
    Sample,
    iou,
    load_split,
    match_predictions,
)

DEFAULT_DATASET = (
    Path(__file__).resolve().parent.parent / "redcard.yolo26"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate_dataset.py",
        description="在带标注的数据集上定量评估检测模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET),
                        help=f"数据集根目录（默认 {DEFAULT_DATASET.name}）")
    parser.add_argument("--split", default="test",
                        choices=["train", "valid", "test"],
                        help="用哪个划分评估（默认 test）")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多评估多少张（默认全部）")
    parser.add_argument("--model", default=None,
                        help=".rknn 模型路径（默认自动定位）")
    parser.add_argument("--engine", default="yolo", choices=["yolo", "hsv"],
                        help="检测引擎")
    parser.add_argument("--preset", default="red",
                        help="HSV 引擎的预设色")
    parser.add_argument("--conf", type=float, default=0.5,
                        help="置信度阈值")
    parser.add_argument("--nms", type=float, default=0.45,
                        help="NMS 阈值")
    parser.add_argument("--iou-threshold", type=float, default=0.5,
                        help="判定「匹配成功」的 IoU 阈值（默认 0.5）")
    parser.add_argument("--save-dir", default="assets/output/validation",
                        help="标注图保存目录（设为空字符串则不保存）")
    parser.add_argument("--save-every", type=int, default=1,
                        help="每 N 张保存一张标注图（默认全存）")
    parser.add_argument("--sheet", action="store_true",
                        help="额外生成一张结果九宫格总览图")
    parser.add_argument("--quiet", action="store_true",
                        help="只输出汇总，不逐张打印")
    parser.add_argument("--debug-n", type=int, default=0,
                        help="对前 N 张图打印原始模型输出与框坐标（排查用）")
    return parser.parse_args()


def dump_raw_diagnosis(detector, sample: Sample) -> None:
    """打印一张图的原始模型输出，用于定位"框为什么不对"。

    这是排查映射类问题的关键手段：把**模型吐出来的原始数字**和
    **最终画出来的框**都列出来，一眼就能看出是哪一环出的问题。
    """
    from src.yolo import letterbox, resolve_box_format

    if not hasattr(detector, "_run_inference"):
        print(f"\n  （{type(detector).__name__} 没有原始张量，"
              f"诊断仅适用于 YOLO 引擎）")
        return

    image = cv2.imread(str(sample.image_path))
    if image is None:
        return

    canvas, info = letterbox(image, detector.input_size)
    output = detector._run_inference(canvas)
    predictions = output[0].T
    scores = predictions[:, 4]
    top = np.argsort(scores)[-5:][::-1]

    print(f"\n  ┌─ 原始输出诊断：{sample.stem[:40]}")
    print(f"  │ 图像 {sample.width}x{sample.height}   "
          f"letterbox scale={info.scale:.4f} top={info.top} left={info.left}")
    print(f"  │ 原始输出 shape={output.shape}")

    raw_top = predictions[top][:, :4]
    fmt = resolve_box_format(raw_top, "auto")
    print(f"  │ 自动判别的框格式：{fmt}")
    print(f"  │ 置信度最高的 5 个候选（原始数值）：")
    for rank, index in enumerate(top):
        b = predictions[index][:4]
        print(f"  │   [{rank + 1}] conf={scores[index]:.4f}  "
              f"raw=({b[0]:>8.2f},{b[1]:>8.2f},{b[2]:>8.2f},{b[3]:>8.2f})")

    # 解码后的框（原图坐标）
    if hasattr(detector, "detect_all"):
        dets = detector.detect_all(image)
        print(f"  │ 解码后的预测框（原图坐标）：")
        for rank, det in enumerate(dets[:5]):
            x1, y1, x2, y2 = det.bbox_xyxy
            print(f"  │   [{rank + 1}] conf={det.score:.3f}  "
                  f"xyxy=({x1},{y1})-({x2},{y2})  "
                  f"中心={det.center_int}  尺寸={x2 - x1}x{y2 - y1}")

    print(f"  │ 真值框（原图坐标）：")
    for rank, gt in enumerate(sample.boxes):
        print(f"  │   [{rank + 1}] "
              f"({gt.x1:.0f},{gt.y1:.0f})-({gt.x2:.0f},{gt.y2:.0f})  "
              f"中心=({gt.center[0]:.0f},{gt.center[1]:.0f})  "
              f"尺寸={gt.width:.0f}x{gt.height:.0f}")

    # 关键判断：预测框尺寸 vs 真值框尺寸
    if hasattr(detector, "detect_all"):
        dets = detector.detect_all(image)
        if dets and sample.boxes:
            pred_area = dets[0].bbox[2] * dets[0].bbox[3]
            gt_area = sample.boxes[0].area
            ratio = pred_area / gt_area if gt_area else 0
            print(f"  │ ⚠️  预测框面积 / 真值框面积 = {ratio:.1f} 倍")
            if ratio > 3:
                print(f"  │    预测框明显过大 → 怀疑框参数解读方式不对")
            elif ratio < 0.33:
                print(f"  │    预测框明显过小 → 怀疑框参数解读方式不对")
    print("  └─")


# ──────────────────────────────────────────────────────────────────────
# 可视化
# ──────────────────────────────────────────────────────────────────────


def draw_result(sample: Sample, result: ImageResult) -> np.ndarray:
    """画出真值（绿）与预测（红/青）对比图。"""
    image = cv2.imread(str(sample.image_path))
    if image is None:
        return np.zeros((sample.height, sample.width, 3), dtype=np.uint8)

    # 真值：绿色
    for gt in sample.boxes:
        x1, y1, x2, y2 = (int(v) for v in gt.bbox)
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(image, "GT", (x1, max(15, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)

    # 预测：命中用青色，误检用红色
    matched_set = set()
    for bbox, score in result.predictions:
        x1, y1, x2, y2 = bbox
        best = 0.0
        for gt in sample.boxes:
            best = max(best, iou(bbox, gt.bbox))

        hit = best >= 0.5
        color = (255, 255, 0) if hit else (0, 0, 255)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)

        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        cv2.drawMarker(image, (cx, cy), (255, 0, 255),
                       cv2.MARKER_CROSS, 16, 2)
        cv2.putText(image, f"{score:.2f} IoU={best:.2f}",
                    (x1, min(sample.height - 6, y2 + 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    # 左上角信息条
    status = "OK" if result.best_iou >= 0.5 else "MISS"
    header = (f"{sample.stem[:24]}  GT={len(sample.boxes)} "
              f"PRED={len(result.predictions)}  bestIoU={result.best_iou:.2f}"
              f"  [{status}]")
    cv2.rectangle(image, (0, 0), (sample.width, 26), (0, 0, 0), -1)
    cv2.putText(image, header, (6, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

    return image


def make_contact_sheet(images: list[np.ndarray], columns: int = 3,
                       tile: tuple[int, int] = (320, 240)) -> np.ndarray:
    """把若干张图拼成网格总览。"""
    if not images:
        return np.zeros((100, 100, 3), dtype=np.uint8)

    thumbs = [cv2.resize(img, tile) for img in images]
    rows = []
    for start in range(0, len(thumbs), columns):
        chunk = thumbs[start:start + columns]
        while len(chunk) < columns:
            chunk.append(np.zeros((tile[1], tile[0], 3), dtype=np.uint8))
        rows.append(np.hstack(chunk))
    return np.vstack(rows)


# ──────────────────────────────────────────────────────────────────────
# 评估主流程
# ──────────────────────────────────────────────────────────────────────


def build_detector(args):
    """按参数构建检测器。"""
    if args.engine == "hsv":
        from src.detector import ColorDetector
        from src.detector import preset_ranges

        return ColorDetector(
            ranges=preset_ranges(args.preset),
            min_area=100.0,
            morph_kernel=3,
            morph_iterations=1,
            max_blocks=5,
        )

    from src.card_detector import CardDetector, default_model_path

    model_path = Path(args.model) if args.model else default_model_path()
    return CardDetector(
        model_path=model_path,
        conf_threshold=args.conf,
        nms_threshold=args.nms,
        max_blocks=5,
        warmup=True,
    )


def main() -> int:
    args = parse_args()

    print(banner("数据集定量评估", "精确率 / 召回率 / IoU / 中心偏差"))
    print()

    dataset_root = Path(args.dataset).expanduser()
    if not dataset_root.is_dir():
        print(f"❌ 数据集目录不存在：{dataset_root}", file=sys.stderr)
        return 1

    # ── 加载数据集 ──────────────────────────────────────────────────
    try:
        samples = load_split(dataset_root, args.split, limit=args.limit)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ 加载数据集失败：{exc}", file=sys.stderr)
        return 1

    if not samples:
        print(f"❌ {args.split} 划分里没有可用样本", file=sys.stderr)
        return 1

    total_gt = sum(len(s.boxes) for s in samples)
    print(f"数据集   : {dataset_root.name} / {args.split}")
    print(f"样本数   : {len(samples)} 张")
    print(f"真值框   : {total_gt} 个"
          f"（平均 {total_gt / len(samples):.2f} 个/张）")
    print(f"检测引擎 : {args.engine}"
          + (f"（{args.preset}）" if args.engine == "hsv" else ""))
    print(f"阈值     : conf={args.conf}  nms={args.nms}  "
          f"IoU判定={args.iou_threshold}")
    print()

    # ── 构建检测器 ──────────────────────────────────────────────────
    try:
        detector = build_detector(args)
        detector.open()
    except Exception as exc:
        print(f"❌ 检测器初始化失败：{exc}", file=sys.stderr)
        print("\n提示：NPU 相关的排查见 docs/验收与排查.md", file=sys.stderr)
        return 2

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    # ── 逐张评估 ────────────────────────────────────────────────────
    results: list[ImageResult] = []
    saved_images: list[np.ndarray] = []

    print("─" * 78)
    print(f"{'#':>3}  {'文件名':<30} {'真值':>4} {'预测':>4} "
          f"{'命中':>4} {'最好IoU':>8} {'中心偏差':>9}")
    print("─" * 78)

    try:
        for index, sample in enumerate(samples, 1):
            image = cv2.imread(str(sample.image_path))
            if image is None:
                continue

            if args.debug_n and index <= args.debug_n:
                dump_raw_diagnosis(detector, sample)

            detections = detector.detect_all(image)
            # ⚠️ 必须用 bbox_xyxy。Detection.bbox 是 (x,y,w,h)，
            #    直接喂给 iou() 会得到 x2<x1 的非法框，IoU 恒为 0。
            predictions = [
                (det.bbox_xyxy, det.score if det.score is not None else 1.0)
                for det in detections
            ]

            matched = match_predictions(
                predictions, sample.boxes, args.iou_threshold
            )
            result = ImageResult(
                sample=sample, predictions=predictions, matched_ious=matched
            )
            results.append(result)

            if not args.quiet:
                error = result.center_error
                error_text = f"{error:>8.1f}px" if error is not None else "     —"
                print(f"{index:>3}  {sample.stem[:30]:<30} "
                      f"{len(sample.boxes):>4} {len(predictions):>4} "
                      f"{result.true_positives:>4} "
                      f"{result.best_iou:>8.3f} {error_text}")

            if save_dir and (index % max(1, args.save_every) == 0):
                annotated = draw_result(sample, result)
                cv2.imwrite(str(save_dir / f"{index:03d}_{sample.stem[:40]}.jpg"),
                            annotated)
                if args.sheet:
                    saved_images.append(annotated)

    finally:
        if hasattr(detector, "close"):
            detector.close()

    # ── 汇总指标 ────────────────────────────────────────────────────
    total_tp = sum(r.true_positives for r in results)
    total_fp = sum(r.false_positives for r in results)
    total_fn = sum(r.false_negatives for r in results)

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    all_ious = [v for r in results for v in r.matched_ious]
    errors = [r.center_error for r in results if r.center_error is not None]
    perfect = sum(1 for r in results if r.best_iou >= 0.5)
    missed = [r for r in results if r.best_iou < 0.5 and r.sample.boxes]
    false_alarms = [r for r in results if r.false_positives > 0]

    print()
    print("═" * 78)
    print("  评估结果")
    print("═" * 78)
    print(f"  样本总数      : {len(results)}")
    print(f"  真值框总数    : {total_gt}")
    print(f"  命中 (TP)     : {total_tp}")
    print(f"  误检 (FP)     : {total_fp}")
    print(f"  漏检 (FN)     : {total_fn}")
    print()
    print(f"  精确率 Precision : {precision * 100:6.2f}%   "
          f"（检出的框里有多少是真的）")
    print(f"  召回率 Recall    : {recall * 100:6.2f}%   "
          f"（真值框里有多少被检出）")
    print(f"  F1 分数          : {f1 * 100:6.2f}%")
    print()
    print(f"  检出成功率    : {perfect}/{len(results)} 张图至少检出一个正确框 "
          f"({perfect / max(1, len(results)) * 100:.1f}%)")
    if all_ious:
        print(f"  平均 IoU      : {statistics.mean(all_ious):.3f}   "
              f"（中位 {statistics.median(all_ious):.3f}）")
    if errors:
        print(f"  中心偏差      : 平均 {statistics.mean(errors):.1f} px   "
              f"中位 {statistics.median(errors):.1f} px   "
              f"最大 {max(errors):.1f} px")
        print(f"                  ← 这个数字直接决定挡板定位准不准")
    print("═" * 78)

    # ── 问题样本 ────────────────────────────────────────────────────
    if missed:
        print()
        print(f"⚠️  未检出的样本（{len(missed)} 张）：")
        for result in missed[:10]:
            gt = result.sample.boxes[0]
            print(f"     {result.sample.stem[:36]:<38} "
                  f"真值框 {gt.width:.0f}x{gt.height:.0f}  "
                  f"预测 {len(result.predictions)} 个")
        if len(missed) > 10:
            print(f"     …还有 {len(missed) - 10} 张")

    if false_alarms:
        print()
        print(f"⚠️  存在误检的样本（{len(false_alarms)} 张）：")
        for result in false_alarms[:10]:
            print(f"     {result.sample.stem[:36]:<38} "
                  f"多检出 {result.false_positives} 个")
        if len(false_alarms) > 10:
            print(f"     …还有 {len(false_alarms) - 10} 张")

    # ── 保存总览图 ──────────────────────────────────────────────────
    if args.sheet and saved_images:
        sheet = make_contact_sheet(saved_images)
        sheet_path = (save_dir or Path(".")) / "00_sheet.jpg"
        cv2.imwrite(str(sheet_path), sheet)
        print()
        print(f"已生成结果总览图：{sheet_path}")

    if save_dir:
        print(f"标注图保存目录：{save_dir}")

    # ── 结论与建议 ──────────────────────────────────────────────────
    print()
    print("【结论】")
    if recall >= 0.95 and precision >= 0.95:
        print("  ✅ 模型表现优秀，可以进入摄像头联调阶段。")
    elif recall >= 0.85 and precision >= 0.85:
        print("  ✅ 模型表现良好，可用于实际追踪。")
        print("     若现场有漏检，优先降低 conf_threshold。")
    elif recall < 0.7:
        print("  ⚠️  漏检偏多。建议排查：")
        print("     · 现场光照/背景是否与训练数据差异过大")
        print("     · 卡片尺寸是否超出训练分布（训练里最小 36x64）")
        print("     · 适当降低 conf_threshold（如 0.3）")
    else:
        print("  ⚠️  误检偏多。建议提高 conf_threshold，")
        print("     或在 ROI 里排除干扰区域。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
