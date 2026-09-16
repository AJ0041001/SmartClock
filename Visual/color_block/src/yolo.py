"""YOLO 检测的公共逻辑 —— 预处理、输出解码、NMS。

抽出来单独放的原因：`scripts/verify_model.py`（验证工具）和
`src/card_detector.py`（正式检测器）都要用同一套逻辑。

**两份实现是 bug 的温床** —— 验证时对、上线时错，这种问题最难查。
所以这里只保留唯一一份，两边都从这里导入。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

#: ultralytics 模型的默认输入边长
DEFAULT_INPUT_SIZE = 640

#: letterbox 填充色（与 ultralytics 训练时一致）
PAD_VALUE = (114, 114, 114)


# ──────────────────────────────────────────────────────────────────────
# 预处理
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LetterboxInfo:
    """letterbox 的几何参数，用于把检测框还原回原图坐标。"""

    scale: float
    top: int
    left: int
    input_size: int

    def restore(self, boxes: np.ndarray) -> np.ndarray:
        """把 letterbox 坐标系下的框还原到原图坐标系。

        boxes: (N, 4) 的 xyxy
        """
        offset = np.array([self.left, self.top, self.left, self.top])
        return (boxes - offset) / self.scale


def letterbox(
    image: np.ndarray,
    size: int = DEFAULT_INPUT_SIZE,
) -> tuple[np.ndarray, LetterboxInfo]:
    """等比缩放 + 灰边补齐到 ``size × size``。

    与 ultralytics 训练/推理时的预处理保持一致：长边缩放到 ``size``，
    短边两侧补灰边（114），**不做拉伸变形** —— 变形会让框的宽高比失真。

    返回 (处理后的图, 几何信息)。
    """
    height, width = image.shape[:2]
    scale = size / max(height, width)
    new_h = int(round(height * scale))
    new_w = int(round(width * scale))

    resized = cv2.resize(image, (new_w, new_h))
    top = (size - new_h) // 2
    left = (size - new_w) // 2

    canvas = cv2.copyMakeBorder(
        resized,
        top, size - new_h - top,
        left, size - new_w - left,
        cv2.BORDER_CONSTANT,
        value=PAD_VALUE,
    )
    return canvas, LetterboxInfo(
        scale=scale, top=top, left=left, input_size=size
    )


# ──────────────────────────────────────────────────────────────────────
# 输出解码
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectedBox:
    """一个检测框（原图坐标系）。"""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float

    @property
    def center(self) -> tuple[float, float]:
        """边界框中心 —— 这就是要回传给 STM32 的二维坐标。"""
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return float(self.width * self.height)


def resolve_box_format(
    raw_boxes: np.ndarray,
    box_format: str = "auto",
) -> str:
    """决定框参数该按哪种格式解读。

    ⚠️ 这是本项目踩过的最深的一个坑，务必读完再改
    ------------------------------------------------
    ultralytics 导出的 ONNX 原始输出是 **cxcywh**（中心x, 中心y, 宽, 高），
    不是 xyxy。这由输出形状决定：``(1, 4+nc, N)`` 是**未内嵌 NMS** 的格式，
    其框参数必然是 cxcywh。

    **曾用过的错误做法**：用「x2 >= x1 且 y2 >= y1」来判别是不是 xyxy。
    对 cxcywh 数据，这个条件等价于 **``w >= cx`` 且 ``h >= cy``** ——
    它只对"目标位于画面左上方"的检测偶然成立：

        · 目标在画面中部（cx=322, w=165）→ 165 < 322 → 判为 xywh ✅
        · 目标在画面左侧（cx=48, w=411）→ 411 > 48  → 判为 xyxy ❌

    结果就是：**同一个模型，在中心位置的合成图上完全正确，
    在真实数据上（卡片偏左）框全部错位**。这种"换个场景就坏"的
    判别逻辑比没有判别更危险，因为它会让人误以为验证通过了。

    现在的策略
    ----------
    ``auto`` 一律返回 **xywh** —— 因为形状 ``(1, 5, 8400)`` 已经明确
    表明这是无 NMS 的 ultralytics 输出，格式是确定的，不需要猜。

    只有换成**内嵌了 NMS** 的导出（那种输出是 ``(1, N, 6)``，
    最后一维是 x1,y1,x2,y2,conf,cls）时，才需要显式传 ``"xyxy"``。

    参数
    ----
    raw_boxes : 原始框参数（仅用于将来可能的启发式判别，当前未使用）
    box_format : ``"auto"`` / ``"xywh"`` / ``"xyxy"``
    """
    if box_format in ("xywh", "xyxy"):
        return box_format

    # auto：形状已确定格式，直接返回 xywh。
    #
    # 保留 raw_boxes 参数是为了兼容既有调用签名，并给将来的启发式
    # 判别留位置 —— 但绝不会再用"x2>=x1"这种不可靠的判据。
    return "xywh"


def decode_predictions(
    output: np.ndarray,
    conf_threshold: float = 0.5,
    nms_threshold: float = 0.45,
    info: LetterboxInfo | None = None,
    orig_shape: tuple[int, int] | None = None,
    box_format: str = "auto",
) -> list[DetectedBox]:
    """把 YOLO 原始输出解码成原图坐标系下的检测框列表。

    参数
    ----
    output : 形状 (1, 4+nc, N) 的原始输出。本模型是 (1, 5, 8400)。
             前 4 个是框参数，之后是各类别分数（单类时只有 1 个）。
    info : letterbox 几何信息；None 表示不做还原
    orig_shape : 原图 (height, width)，用于裁剪越界框
    box_format : "auto" / "xywh" / "xyxy"

    返回按置信度降序排列的 :class:`DetectedBox` 列表。
    """
    if output is None or output.size == 0:
        return []

    # (1, 5, N) → (N, 5)
    if output.ndim == 3:
        predictions = output[0].T
    elif output.ndim == 2:
        predictions = output.T if output.shape[0] < output.shape[1] else output
    else:
        return []

    if predictions.shape[1] < 5:
        return []

    raw_boxes = predictions[:, :4].astype(np.float64)
    scores = predictions[:, 4].astype(np.float64)

    # 原始输出里可能混有个别 Inf/NaN（低置信度候选在 dist2bbox 时溢出）。
    # 先剔除，避免污染坐标换算和 NMS。
    finite_mask = np.isfinite(raw_boxes).all(axis=1) & np.isfinite(scores)

    keep = (scores > conf_threshold) & finite_mask
    raw_boxes = raw_boxes[keep]
    scores = scores[keep]
    if len(raw_boxes) == 0:
        return []

    fmt = resolve_box_format(raw_boxes, box_format)
    if fmt == "xyxy":
        boxes = raw_boxes
    else:
        # cxcywh → xyxy
        cx, cy, w, h = (raw_boxes[:, i] for i in range(4))
        boxes = np.stack(
            [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0], axis=1
        )

    # 还原到原图坐标
    if info is not None:
        boxes = info.restore(boxes)

    # 裁剪到原图范围：越界坐标会让 NMSBoxes 报错
    if orig_shape is not None:
        height, width = orig_shape
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, width - 1)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, height - 1)

    # 过滤掉几何上非法的框
    valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes = boxes[valid]
    scores = scores[valid]
    if len(boxes) == 0:
        return []

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(), scores.tolist(), conf_threshold, nms_threshold
    )
    if len(indices) == 0:
        return []

    results: list[DetectedBox] = []
    for idx in np.array(indices).flatten():
        x1, y1, x2, y2 = (int(v) for v in boxes[idx])
        results.append(DetectedBox(x1, y1, x2, y2, float(scores[idx])))

    results.sort(key=lambda box: box.score, reverse=True)
    return results


# ──────────────────────────────────────────────────────────────────────
# 可视化
# ──────────────────────────────────────────────────────────────────────


def draw_boxes(
    image: np.ndarray,
    boxes: list[DetectedBox],
    color: tuple[int, int, int] = (0, 255, 0),
    label_prefix: str = "",
    draw_center: bool = True,
) -> np.ndarray:
    """在图像副本上画出检测框与中心点。不修改原图。"""
    canvas = image.copy()
    for index, box in enumerate(boxes):
        cv2.rectangle(canvas, (box.x1, box.y1), (box.x2, box.y2), color, 2)

        if draw_center:
            cx, cy = (int(round(v)) for v in box.center)
            cv2.drawMarker(
                canvas, (cx, cy), (0, 0, 255),
                markerType=cv2.MARKER_CROSS, markerSize=18, thickness=2,
            )
            cv2.circle(canvas, (cx, cy), 3, (255, 255, 255), -1)

        label = f"{label_prefix}{index} {box.score:.2f}"
        cv2.putText(
            canvas, label, (box.x1, max(20, box.y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA,
        )
    return canvas
