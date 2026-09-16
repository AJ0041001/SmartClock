"""卡片检测器 —— 用你们的 YOLO26 / RKNN 模型在 NPU 上检测红卡。

与 :class:`src.detector.ColorDetector` **接口完全一致**（都有 ``detect()``
/ ``detect_all()`` / ``draw()`` / ``draw_roi()``），因此流水线
``src/pipeline.py`` 不需要改任何代码就能从 HSV 切换到 YOLO。

输出目标（本项目的核心需求）
--------------------------
**检测到红卡 → 输出它的二维中心坐标。**

中心由检测框算得::

    center = ((x1 + x2) / 2, (y1 + y2) / 2)

然后交给 :class:`src.mapper.CoordinateMapper` 映射成游戏坐标，
再按 10 字节协议发给 STM32。

关于黑背景
----------
你们的使用场景是黑底红卡，这对检测非常有利：
  · 背景干净 → 误检极少
  · 对比度高 → 置信度稳定
因此 ``conf_threshold`` 可以设得偏高（0.5~0.6），进一步压低误检。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np

from .detector import Detection
from .rknn_ctypes import RKNNLite, RknnError, find_librknnrt
from .yolo import (
    DEFAULT_INPUT_SIZE,
    DetectedBox,
    LetterboxInfo,
    decode_predictions,
    draw_boxes,
    letterbox,
)

log = logging.getLogger("color_block.card_detector")


class CardDetectorError(RuntimeError):
    """卡片检测器初始化或推理失败。"""


class CardDetector:
    """基于 RKNN 的 YOLO 卡片检测器。

    典型用法::

        with CardDetector("best.rknn") as detector:
            result = detector.detect(frame)      # 返回 Detection 或 None
            if result is not None:
                print(result.center)             # ← 二维中心坐标

    参数
    ----
    model_path : .rknn 模型路径
    conf_threshold : 置信度阈值。黑背景场景可以偏高（0.5~0.6）
    nms_threshold : NMS 去重阈值
    input_size : 模型输入边长（本模型是 640）
    core_mask : NPU 核心掩码，默认三核全用
    roi : (x, y, w, h) 检测区域；None 表示全图。
          裁到 ROI 再推理能让卡片在输入里显得更大，精度更高、速度更快。
    max_blocks : 最多返回几个目标
    warmup : 打开时是否先跑一次空推理（触发 NPU 初始化与缓存预热）
    """

    def __init__(
        self,
        model_path: str | Path,
        conf_threshold: float = 0.5,
        nms_threshold: float = 0.45,
        input_size: int = DEFAULT_INPUT_SIZE,
        core_mask: int = RKNNLite.NPU_CORE_0_1_2,
        roi: tuple[int, int, int, int] | None = None,
        max_blocks: int = 1,
        warmup: bool = True,
        box_format: str = "auto",
    ) -> None:
        self.model_path = Path(model_path)
        self.conf_threshold = float(conf_threshold)
        self.nms_threshold = float(nms_threshold)
        self.input_size = int(input_size)
        self.core_mask = int(core_mask)
        self.roi = roi
        self.max_blocks = max(1, int(max_blocks))
        self.warmup = bool(warmup)
        self.box_format = box_format

        self._rknn: RKNNLite | None = None
        self._opened = False

        # 统计
        self.inference_count = 0
        self.total_inference_ms = 0.0
        self.last_inference_ms = 0.0

    # ── 生命周期 ────────────────────────────────────────────────────

    def open(self) -> "CardDetector":
        """载入模型并初始化 NPU 运行时。

        延迟到显式调用或首次 detect 时才做 —— 这样导入模块不会
        因为找不到 NPU 而失败。
        """
        if self._opened:
            return self

        if not self.model_path.exists():
            raise CardDetectorError(
                f"模型文件不存在：{self.model_path}\n"
                f"提示：默认位置是 Visual/lbm/model/best.rknn"
            )

        if find_librknnrt() is None:
            raise CardDetectorError(
                "找不到 librknnrt.so。解决办法（任选其一）：\n"
                "  sudo cp Visual/lbm/runtime/librknnrt.so /usr/lib/ "
                "&& sudo ldconfig\n"
                "  或 export RKNN_RT_PATH=/绝对路径/librknnrt.so"
            )

        rknn = RKNNLite(verbose=False)
        try:
            rknn.load_rknn(self.model_path)
            rknn.init_runtime(core_mask=self.core_mask)
        except RknnError as exc:
            rknn.release()
            raise CardDetectorError(
                f"模型初始化失败：{exc}\n"
                f"常见原因：\n"
                f"  · /dev/rknpu 不可访问（权限或容器未映射设备）\n"
                f"  · NPU 驱动与 librknnrt.so 版本不匹配\n"
                f"  排查：python3 scripts/verify_model.py"
            ) from exc

        self._rknn = rknn
        self._opened = True
        log.info(
            "卡片检测器就绪：%s  conf=%.2f  nms=%.2f  input=%d",
            self.model_path.name, self.conf_threshold,
            self.nms_threshold, self.input_size,
        )

        if self.warmup:
            self._do_warmup()
        return self

    def _do_warmup(self) -> None:
        """跑一次空推理。

        第一帧通常明显慢于后续帧（NPU 需要分配内存、载入权重、编译内核），
        预热可以避免把这段延迟算进正式运行的第一帧。
        """
        blank = np.zeros(
            (self.input_size, self.input_size, 3), dtype=np.uint8
        )
        try:
            self._run_inference(blank)
            log.debug("NPU 预热完成")
        except Exception as exc:  # 预热失败不该阻断启动
            log.warning("预热失败（不影响后续运行）：%s", exc)

    def close(self) -> None:
        if self._rknn is not None:
            self._rknn.release()
            self._rknn = None
        self._opened = False

    def __enter__(self) -> "CardDetector":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._opened

    # ── 推理 ────────────────────────────────────────────────────────

    def _run_inference(self, bgr_image: np.ndarray) -> np.ndarray:
        """跑一次推理，返回原始输出张量。

        注意：输入必须是 **uint8 RGB**，且 **不能用直通模式** ——
        本模型的输入节点是 float16，归一化 (std=255) 烘焙在 RKNN 内部，
        必须让 RKNN 自己完成「uint8 → 除 255 → float16」的转换。
        用错模式会导致输出全 NaN（详见 src/rknn_ctypes.py 的说明）。
        """
        if self._rknn is None:
            raise CardDetectorError("检测器尚未打开，请先调用 open()")

        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        batch = np.ascontiguousarray(rgb[None, ...])

        start = time.monotonic()
        outputs = self._rknn.inference(inputs=[batch], data_format="nhwc")
        self.last_inference_ms = (time.monotonic() - start) * 1000.0

        self.inference_count += 1
        self.total_inference_ms += self.last_inference_ms

        return outputs[0]

    @property
    def average_inference_ms(self) -> float:
        if self.inference_count == 0:
            return 0.0
        return self.total_inference_ms / self.inference_count

    # ── 检测 ────────────────────────────────────────────────────────

    def detect_all(self, image: np.ndarray) -> list[Detection]:
        """返回画面中所有卡片，按置信度降序。

        坐标一律是**整图坐标系**，即使配置了 ROI 也会换算回去 ——
        否则映射到游戏坐标时会整体偏移。
        """
        if image is None or image.size == 0:
            return []

        if not self._opened:
            self.open()

        offset_x, offset_y = 0, 0
        working = image

        # 裁 ROI：让卡片在模型输入里占比更大
        if self.roi is not None:
            rx, ry, rw, rh = self.roi
            height, width = image.shape[:2]
            rx = max(0, min(int(rx), width - 1))
            ry = max(0, min(int(ry), height - 1))
            rw = max(1, min(int(rw), width - rx))
            rh = max(1, min(int(rh), height - ry))
            working = image[ry:ry + rh, rx:rx + rw]
            offset_x, offset_y = rx, ry

        # 预处理
        canvas, info = letterbox(working, self.input_size)

        # 推理
        try:
            output = self._run_inference(canvas)
        except RknnError as exc:
            log.error("推理失败：%s", exc)
            return []

        # 解码
        boxes = decode_predictions(
            output,
            conf_threshold=self.conf_threshold,
            nms_threshold=self.nms_threshold,
            info=info,
            orig_shape=working.shape[:2],
            box_format=self.box_format,
        )

        # 换算回整图坐标 + 转成统一的 Detection 结构
        detections: list[Detection] = []
        for box in boxes[: self.max_blocks]:
            x1 = box.x1 + offset_x
            y1 = box.y1 + offset_y
            x2 = box.x2 + offset_x
            y2 = box.y2 + offset_y

            w = x2 - x1
            h = y2 - y1
            # YOLO 只给矩形框，没有像素级轮廓。
            # 合成一个矩形轮廓，让 draw() 等下游代码无需区分检测器类型。
            contour = np.array(
                [[[x1, y1]], [[x2, y1]], [[x2, y2]], [[x1, y2]]],
                dtype=np.int32,
            )

            detections.append(
                Detection(
                    # ↓ 这就是要回传给 STM32 的二维中心坐标
                    center=((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                    bbox=(x1, y1, w, h),
                    area=float(w * h),
                    contour=contour,
                    score=box.score,
                )
            )

        return detections

    def detect(self, image: np.ndarray) -> Detection | None:
        """返回置信度最高的卡片；没检测到返回 None。"""
        found = self.detect_all(image)
        return found[0] if found else None

    # ── 可视化 ──────────────────────────────────────────────────────

    def draw(
        self,
        image: np.ndarray,
        detections: list[Detection] | None = None,
        color: tuple[int, int, int] = (0, 255, 0),
        label_prefix: str = "",
    ) -> np.ndarray:
        """画出检测框与中心点。``detections=None`` 时会自己跑一次检测。"""
        if detections is None:
            detections = self.detect_all(image)

        boxes = [
            DetectedBox(
                x1=det.bbox[0],
                y1=det.bbox[1],
                x2=det.bbox[0] + det.bbox[2],
                y2=det.bbox[1] + det.bbox[3],
                score=det.score if det.score is not None else 1.0,
            )
            for det in detections
        ]
        return draw_boxes(
            image, boxes, color=color, label_prefix=label_prefix
        )

    def draw_roi(self, image: np.ndarray,
                 color: tuple[int, int, int] = (255, 200, 0)) -> np.ndarray:
        canvas = image.copy()
        if self.roi is not None:
            x, y, w, h = self.roi
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                canvas, "ROI", (x + 6, y + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA,
            )
        return canvas

    # ── 构造 ────────────────────────────────────────────────────────

    def describe(self) -> str:
        roi_text = (
            f"({self.roi[0]},{self.roi[1]},{self.roi[2]},{self.roi[3]})"
            if self.roi else "全图"
        )
        avg = self.average_inference_ms
        perf = f"{avg:.1f} ms/帧" if self.inference_count else "尚未推理"
        return "\n".join([
            f"引擎       : YOLO / RKNN（NPU 三核）",
            f"模型       : {self.model_path.name}",
            f"输入尺寸   : {self.input_size}x{self.input_size}",
            f"置信度/NMS : {self.conf_threshold} / {self.nms_threshold}",
            f"ROI        : {roi_text}",
            f"性能       : {perf}（已推理 {self.inference_count} 次）",
        ])


def default_model_path() -> Path:
    """定位仓库里自带的 best.rknn。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "Visual" / "lbm" / "model" / "best.rknn"
        if candidate.exists():
            return candidate
    # 兜底：相对本项目位置
    return here.parent.parent.parent / "lbm" / "model" / "best.rknn"


def build_card_detector(
    model_path: str | Path | None = None,
    conf_threshold: float = 0.5,
    nms_threshold: float = 0.45,
    roi: tuple[int, int, int, int] | None = None,
    max_blocks: int = 1,
) -> CardDetector:
    """便捷构造：模型路径留空时自动在仓库里找 best.rknn。"""
    path = Path(model_path) if model_path else default_model_path()
    return CardDetector(
        model_path=path,
        conf_threshold=conf_threshold,
        nms_threshold=nms_threshold,
        roi=roi,
        max_blocks=max_blocks,
    )
