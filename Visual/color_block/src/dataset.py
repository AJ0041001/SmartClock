"""数据集读取 —— 解析 Roboflow 导出的 YOLO 标注，用于模型评估。

支持两种标签格式
----------------
Roboflow 导出时有两种标注类型，这个数据集用的是**多边形（分割）**：

1. **检测框**（5 个字段）::

       0 0.539 0.2625 0.539 0.275      # class cx cy w h（归一化）

2. **多边形**（多个坐标对）::

       0 0.539 0.2625 0.539 0.275 0.587 0.298 ...   # class x1 y1 x2 y2 ...

   这是本项目 `redcard.yolo26` 用的格式，来自 Roboflow 的多边形标注。

两种都要支持：训练时 `convert_cards.py` 把多边形转成矩形框，
而评估时我们直接读原始多边形，取外接矩形作为真值 —— 这样
评价的是"模型能不能框住这张卡"，与训练流程解耦。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


# ──────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GroundTruth:
    """一个标注框（像素坐标 xyxy）。"""

    class_id: int
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class Sample:
    """一张图 + 它的标注。"""

    image_path: Path
    label_path: Path
    width: int
    height: int
    boxes: list[GroundTruth]

    @property
    def name(self) -> str:
        return self.image_path.name

    @property
    def stem(self) -> str:
        return self.image_path.stem

    @property
    def has_annotation(self) -> bool:
        return len(self.boxes) > 0


# ──────────────────────────────────────────────────────────────────────
# 标签解析
# ──────────────────────────────────────────────────────────────────────


def parse_label_file(label_path: Path) -> list[tuple[int, float, float, float, float]]:
    """解析 YOLO 标签文件，返回**归一化**坐标的矩形列表。

    返回值：[(class_id, x1, y1, x2, y2), ...]，坐标均为 0~1。

    多边形标签会被转成外接矩形 —— 因为本模型输出的是矩形框，
    用外接矩形作真值才能做同类比较。
    """
    if not label_path.exists():
        return []

    results: list[tuple[int, float, float, float, float]] = []

    for raw_line in label_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 5:
            continue

        try:
            class_id = int(float(parts[0]))
            values = [float(v) for v in parts[1:]]
        except ValueError:
            continue

        if len(values) == 4:
            # 标准检测框：cx cy w h（归一化）
            cx, cy, w, h = values
            results.append((
                class_id,
                cx - w / 2.0,
                cy - h / 2.0,
                cx + w / 2.0,
                cy + h / 2.0,
            ))
        elif len(values) >= 6 and len(values) % 2 == 0:
            # 多边形：x1 y1 x2 y2 ...（归一化）→ 取外接矩形
            xs = values[0::2]
            ys = values[1::2]
            results.append((class_id, min(xs), min(ys), max(xs), max(ys)))
        # 其它长度视为无法识别，跳过而不是崩溃

    return results


def parse_ground_truths(
    label_path: Path,
    width: int,
    height: int,
) -> list[GroundTruth]:
    """解析标签并换算到像素坐标。"""
    return [
        GroundTruth(
            class_id=cid,
            x1=x1 * width,
            y1=y1 * height,
            x2=x2 * width,
            y2=y2 * height,
        )
        for cid, x1, y1, x2, y2 in parse_label_file(label_path)
    ]


# ──────────────────────────────────────────────────────────────────────
# 数据集加载
# ──────────────────────────────────────────────────────────────────────


VALID_SPLITS = ("train", "valid", "test")


def load_split(
    dataset_root: str | Path,
    split: str = "test",
    limit: int | None = None,
    require_annotation: bool = True,
) -> list[Sample]:
    """加载一个数据划分。

    参数
    ----
    dataset_root : 数据集根目录（含 train/ valid/ test/ 和 data.yaml）
    split : "train" / "valid" / "test"
    limit : 最多加载多少张（用于快速抽样）
    require_annotation : 是否只保留有标注的样本
    """
    root = Path(dataset_root)
    if split not in VALID_SPLITS:
        raise ValueError(
            f"split 必须是 {VALID_SPLITS} 之一，收到 {split!r}"
        )

    images_dir = root / split / "images"
    labels_dir = root / split / "labels"
    if not images_dir.is_dir():
        raise FileNotFoundError(f"找不到图片目录：{images_dir}")

    import cv2

    samples: list[Sample] = []
    for image_path in sorted(images_dir.glob("*")):
        if image_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
            continue

        image = cv2.imread(str(image_path))
        if image is None:
            continue
        height, width = image.shape[:2]

        label_path = labels_dir / f"{image_path.stem}.txt"
        boxes = parse_ground_truths(label_path, width, height)

        if require_annotation and not boxes:
            continue

        samples.append(Sample(
            image_path=image_path,
            label_path=label_path,
            width=width,
            height=height,
            boxes=boxes,
        ))

        if limit is not None and len(samples) >= limit:
            break

    return samples


# ──────────────────────────────────────────────────────────────────────
# 评价指标
# ──────────────────────────────────────────────────────────────────────


def iou(box_a: tuple[float, float, float, float],
        box_b: tuple[float, float, float, float]) -> float:
    """两个 xyxy 框的交并比（IoU），范围 0~1。"""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter_area

    if union <= 0:
        return 0.0
    return float(inter_area / union)


@dataclass
class ImageResult:
    """单张图的评估结果。"""

    sample: Sample
    predictions: list[tuple[tuple[int, int, int, int], float]]
    matched_ious: list[float]

    @property
    def true_positives(self) -> int:
        return len(self.matched_ious)

    @property
    def false_positives(self) -> int:
        return max(0, len(self.predictions) - len(self.matched_ious))

    @property
    def false_negatives(self) -> int:
        return max(0, len(self.sample.boxes) - len(self.matched_ious))

    @property
    def best_iou(self) -> float:
        return max(self.matched_ious) if self.matched_ious else 0.0

    @property
    def center_error(self) -> float | None:
        """最佳匹配的中心点偏差（像素）。None 表示没匹配上。"""
        if not self.matched_ious:
            return None
        # 找到 IoU 最高的那个预测
        best_index = int(np.argmax(self.matched_ious))
        if best_index >= len(self.predictions):
            return None
        px1, py1, px2, py2 = self.predictions[best_index][0]
        pred_center = ((px1 + px2) / 2.0, (py1 + py2) / 2.0)

        # 取与之最接近的真值中心
        best_distance = None
        for gt in self.sample.boxes:
            gx, gy = gt.center
            distance = ((pred_center[0] - gx) ** 2
                        + (pred_center[1] - gy) ** 2) ** 0.5
            if best_distance is None or distance < best_distance:
                best_distance = distance
        return best_distance


def match_predictions(
    predictions: list[tuple[tuple[int, int, int, int], float]],
    ground_truths: list[GroundTruth],
    iou_threshold: float = 0.5,
) -> list[float]:
    """把预测框与真值框做贪心匹配，返回匹配上的 IoU 列表。

    贪心策略：按置信度从高到低处理每个预测，找当前**未被占用**的真值中
    IoU 最大的那个；超过阈值就算匹配成功。这是评估检测模型的常规做法。
    """
    used: set[int] = set()
    matched: list[float] = []

    for bbox, _score in predictions:
        best_iou = 0.0
        best_index = -1
        for index, gt in enumerate(ground_truths):
            if index in used:
                continue
            value = iou(bbox, gt.bbox)
            if value > best_iou:
                best_iou = value
                best_index = index
        if best_index >= 0 and best_iou >= iou_threshold:
            used.add(best_index)
            matched.append(best_iou)

    return matched
