"""CardDetector 测试 —— 用假 RKNN 模拟 NPU，无硬件也能验证集成逻辑。

关键验证点
----------
1. **中心坐标算得对** —— 这是本项目的核心需求
2. **ROI 坐标要换算回整图** —— 否则映射到游戏坐标会整体偏移
3. **接口与 ColorDetector 一致** —— 保证流水线能无缝切换检测器
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from src.card_detector import CardDetector, CardDetectorError
from src.detector import ColorDetector, Detection


# ──────────────────────────────────────────────────────────────────────
# 假 RKNN 运行时
# ──────────────────────────────────────────────────────────────────────


class FakeRKNNLite:
    """模拟 RKNNLite。

    不会碰 NPU，而是返回预先编排好的输出张量。
    这样就能精确断言"给定模型输出，检测器算出的中心对不对"。
    """

    #: 类级别的脚本：下一次 inference 返回什么
    scripted_output: np.ndarray | None = None
    instances: list["FakeRKNNLite"] = []

    NPU_CORE_0_1_2 = 7
    NPU_CORE_AUTO = 0

    def __init__(self, verbose: bool = True, lib_path=None) -> None:
        self.verbose = verbose
        self.loaded_path: Path | None = None
        self.initialized = False
        self.released = False
        self.core_mask: int | None = None
        self.inference_inputs: list[np.ndarray] = []
        FakeRKNNLite.instances.append(self)

    def load_rknn(self, path) -> bool:
        self.loaded_path = Path(path)
        return True

    def init_runtime(self, core_mask: int = 0, flag: int = 0) -> bool:
        self.initialized = True
        self.core_mask = core_mask
        return True

    def inference(self, inputs, data_format: str = "nhwc",
                  inputs_pass_through=None, **_):
        self.inference_inputs.append(np.asarray(inputs[0]))
        if FakeRKNNLite.scripted_output is None:
            return [np.zeros((1, 5, 8400), dtype=np.float32)]
        return [FakeRKNNLite.scripted_output]

    def release(self) -> None:
        self.released = True


def script_boxes(entries: list[tuple[tuple[float, float, float, float],
                                     float]]) -> np.ndarray:
    """编排一组检测结果（cxcywh 格式，与模型原始输出一致）。"""
    out = np.zeros((1, 5, 8400), dtype=np.float32)
    for index, (box, score) in enumerate(entries):
        out[0, :4, index] = box
        out[0, 4, index] = score
    return out


class CardDetectorTestBase(unittest.TestCase):
    """统一打桩：替换 RKNNLite 与库查找。"""

    def setUp(self) -> None:
        FakeRKNNLite.scripted_output = None
        FakeRKNNLite.instances = []

        self._patches = [
            mock.patch("src.card_detector.RKNNLite", FakeRKNNLite),
            mock.patch("src.card_detector.find_librknnrt",
                       return_value="/fake/librknnrt.so"),
        ]
        for patch in self._patches:
            patch.start()

        # 造一个假的模型文件（内容无所谓，假运行时不会解析）
        self.tmp_model = Path("/tmp/fake_best.rknn")
        if not self.tmp_model.exists():
            self.tmp_model.write_bytes(b"FAKE_RKNN_MODEL" * 100)

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()

    def make_detector(self, **kwargs) -> CardDetector:
        kwargs.setdefault("warmup", False)
        kwargs.setdefault("input_size", 640)
        return CardDetector(self.tmp_model, **kwargs)


# ──────────────────────────────────────────────────────────────────────
# 核心：中心坐标
# ──────────────────────────────────────────────────────────────────────


class TestCenterCoordinate(CardDetectorTestBase):
    """本项目的核心需求：检测到色块 → 输出二维中心坐标。"""

    def test_center_of_known_box(self) -> None:
        # 模型输出 cxcywh：(320, 240) 中心，200x100 尺寸
        # letterbox 无缩放无偏移（原图就是 640x640）
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 200.0, 100.0), 0.9)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.center[0], 320.0, delta=2)
        self.assertAlmostEqual(result.center[1], 240.0, delta=2)
        self.assertAlmostEqual(result.score, 0.9, places=2)

    def test_center_with_letterbox_offset(self) -> None:
        """原图 640x480 会被 letterbox 加上下边距，中心必须还原。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 320.0, 200.0, 100.0), 0.9)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

        assert result is not None
        # letterbox: scale=1, top=80 → 中心 Y 应减去 80
        self.assertAlmostEqual(result.center[0], 320.0, delta=2)
        self.assertAlmostEqual(result.center[1], 240.0, delta=2)

    def test_center_scaled_image(self) -> None:
        """原图 1280x960 → scale=0.5 且带 letterbox 边距，坐标要正确还原。

        几何推演：
            原图 960(h) x 1280(w)
            max=1280 → scale = 640/1280 = 0.5
            缩放后 480(h) x 640(w)
            top = (640-480)//2 = 80     ← 这个偏移必须减掉
            模型输出中心 y=240
            → 缩放图内 y = (240-80) = 160
            → 原图内   y = 160 / 0.5 = 320
        """
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 100.0), 0.9)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((960, 1280, 3), dtype=np.uint8))

        assert result is not None
        self.assertAlmostEqual(result.center[0], 640.0, delta=3)   # 320/0.5
        self.assertAlmostEqual(result.center[1], 320.0, delta=3)   # (240-80)/0.5

    def test_bbox_consistent_with_center(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((300.0, 200.0, 100.0, 60.0), 0.85)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

        assert result is not None
        x, y, w, h = result.bbox
        self.assertAlmostEqual(result.center[0], x + w / 2, delta=1)
        self.assertAlmostEqual(result.center[1], y + h / 2, delta=1)
        self.assertAlmostEqual(result.bbox_center[0], result.center[0], delta=1)

    def test_center_is_float(self) -> None:
        """奇数尺寸时中心是半像素，不能取整丢精度。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((100.0, 100.0, 21.0, 21.0), 0.9)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        assert result is not None
        self.assertIsInstance(result.center[0], float)


# ──────────────────────────────────────────────────────────────────────
# ROI
# ──────────────────────────────────────────────────────────────────────


class TestRoiHandling(CardDetectorTestBase):
    """ROI 只用于缩小推理范围，输出坐标必须换算回整图。"""

    def test_roi_offset_restored(self) -> None:
        """ROI 内的坐标要先还原 letterbox，再加回 ROI 偏移。

        几何推演（原图 480x640，ROI = 200,100,400,400）：
            ROI 实际裁剪高度 = min(400, 480-100) = 380
            scale = 640/400 = 1.6
            缩放后 608(h) x 640(w)
            top = (640-608)//2 = 16        ← 同样必须减掉
            模型输出中心 (100,100)
            → ROI 内 = (100/1.6, (100-16)/1.6) = (62.5, 52.5)
            → 整图   = (262.5, 152.5)
        """
        FakeRKNNLite.scripted_output = script_boxes(
            [((100.0, 100.0, 80.0, 80.0), 0.9)]
        )
        with self.make_detector(roi=(200, 100, 400, 400)) as detector:
            result = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

        assert result is not None
        scale = 640 / 400
        top = (640 - int(round(380 * scale))) // 2
        self.assertAlmostEqual(result.center[0], 200 + 100 / scale, delta=3)
        self.assertAlmostEqual(
            result.center[1], 100 + (100 - top) / scale, delta=3
        )
        # 明确钉住数值，避免公式写错时两边一起错
        self.assertAlmostEqual(result.center[0], 262.5, delta=1)
        self.assertAlmostEqual(result.center[1], 152.5, delta=1)

    def test_roi_smaller_than_image(self) -> None:
        """ROI 之外的目标不该被检出（因为根本没送进模型）。"""
        FakeRKNNLite.scripted_output = script_boxes([])
        with self.make_detector(roi=(0, 0, 320, 240)) as detector:
            result = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertIsNone(result)

    def test_roi_inference_uses_cropped_size(self) -> None:
        """送进模型的输入必须是 ROI 裁剪后的大小，而不是整图。"""
        FakeRKNNLite.scripted_output = script_boxes([])
        with self.make_detector(roi=(200, 100, 400, 400)) as detector:
            detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

        sent = FakeRKNNLite.instances[-1].inference_inputs[-1]
        # 无论 ROI 多大，letterbox 后都应是 640x640x3
        self.assertEqual(sent.shape, (1, 640, 640, 3))


# ──────────────────────────────────────────────────────────────────────
# 输出结构
# ──────────────────────────────────────────────────────────────────────


class TestDetectionStructure(CardDetectorTestBase):
    def test_returns_detection_objects(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 100.0), 0.9)]
        )
        with self.make_detector() as detector:
            results = detector.detect_all(
                np.zeros((640, 640, 3), dtype=np.uint8)
            )
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Detection)

    def test_score_populated(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 100.0), 0.77)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        assert result is not None
        self.assertAlmostEqual(result.score, 0.77, places=2)

    def test_max_blocks_limits_results(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes([
            ((150.0, 240.0, 80.0, 80.0), 0.95),
            ((500.0, 240.0, 80.0, 80.0), 0.90),
        ])
        with self.make_detector(max_blocks=1) as detector:
            results = detector.detect_all(
                np.zeros((640, 640, 3), dtype=np.uint8)
            )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].score, 0.95, places=2)

    def test_max_blocks_two(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes([
            ((150.0, 240.0, 80.0, 80.0), 0.95),
            ((500.0, 240.0, 80.0, 80.0), 0.90),
        ])
        with self.make_detector(max_blocks=2) as detector:
            results = detector.detect_all(
                np.zeros((640, 640, 3), dtype=np.uint8)
            )
        self.assertEqual(len(results), 2)

    def test_no_detection_returns_none(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes([])
        with self.make_detector() as detector:
            self.assertIsNone(
                detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
            )

    def test_contour_synthesized_from_bbox(self) -> None:
        """YOLO 没有像素级轮廓，应合成一个矩形轮廓供可视化用。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 60.0), 0.9)]
        )
        with self.make_detector() as detector:
            result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        assert result is not None
        self.assertIsNotNone(result.contour)
        self.assertEqual(result.contour.reshape(-1, 2).shape[0], 4)


# ──────────────────────────────────────────────────────────────────────
# 生命周期与错误处理
# ──────────────────────────────────────────────────────────────────────


class TestLifecycle(CardDetectorTestBase):
    def test_lazy_open(self) -> None:
        """不调用 open() 直接 detect() 也应该工作。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 100.0), 0.9)]
        )
        detector = self.make_detector()
        self.assertFalse(detector.is_open)
        result = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        self.assertTrue(detector.is_open)
        self.assertIsNotNone(result)
        detector.close()

    def test_context_manager_releases(self) -> None:
        with self.make_detector() as detector:
            self.assertTrue(detector.is_open)
            rknn = FakeRKNNLite.instances[-1]
        self.assertFalse(detector.is_open)
        self.assertTrue(rknn.released)

    def test_core_mask_passed(self) -> None:
        with self.make_detector() as detector:
            detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        self.assertEqual(FakeRKNNLite.instances[-1].core_mask, 7)

    def test_missing_model_raises(self) -> None:
        detector = CardDetector("/nonexistent/model.rknn", warmup=False)
        with self.assertRaises(CardDetectorError) as ctx:
            detector.open()
        self.assertIn("模型文件不存在", str(ctx.exception))

    def test_missing_library_raises(self) -> None:
        with mock.patch("src.card_detector.find_librknnrt",
                        return_value=None):
            detector = CardDetector(self.tmp_model, warmup=False)
            with self.assertRaises(CardDetectorError) as ctx:
                detector.open()
            self.assertIn("librknnrt.so", str(ctx.exception))

    def test_empty_image_returns_empty(self) -> None:
        with self.make_detector() as detector:
            self.assertEqual(
                detector.detect_all(np.zeros((0, 0, 3), dtype=np.uint8)), []
            )

    def test_statistics_accumulate(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 240.0, 100.0, 100.0), 0.9)]
        )
        with self.make_detector() as detector:
            for _ in range(3):
                detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
            self.assertEqual(detector.inference_count, 3)
            self.assertGreater(detector.average_inference_ms, 0.0)


# ──────────────────────────────────────────────────────────────────────
# 接口兼容性 —— 保证流水线能无缝切换
# ──────────────────────────────────────────────────────────────────────


class TestInterfaceCompatibility(unittest.TestCase):
    """两种检测器必须能被流水线无差别调用。"""

    REQUIRED_METHODS = ("detect", "detect_all", "draw", "draw_roi")

    def test_card_detector_has_required_methods(self) -> None:
        for name in self.REQUIRED_METHODS:
            with self.subTest(method=name):
                self.assertTrue(
                    callable(getattr(CardDetector, name, None)),
                    f"CardDetector 缺少 {name}() —— 流水线无法直接切换",
                )

    def test_color_detector_has_required_methods(self) -> None:
        for name in self.REQUIRED_METHODS:
            with self.subTest(method=name):
                self.assertTrue(
                    callable(getattr(ColorDetector, name, None)),
                    f"ColorDetector 缺少 {name}()",
                )

    def test_signatures_match(self) -> None:
        import inspect

        for name in ("detect", "detect_all"):
            card_sig = inspect.signature(getattr(CardDetector, name))
            color_sig = inspect.signature(getattr(ColorDetector, name))
            self.assertEqual(
                list(card_sig.parameters),
                list(color_sig.parameters),
                f"{name}() 的参数不一致，流水线切换时会出问题",
            )


class TestBboxConventionRegression(CardDetectorTestBase):
    """回归：Detection.bbox 的 (x,y,w,h) 约定被误当成 xyxy。

    这是真实数据集验证时暴露的 bug：
      · `Detection.bbox` 定义是 **(x, y, w, h)**
      · 验证脚本直接把它喂给期望 (x1,y1,x2,y2) 的 iou()
      · 结果 x2 < x1，框非法，**32/32 张图 IoU 恒为 0.000**
      · 而模型其实完全正确（真实 IoU 0.96）

    危害在于：会让人误判"模型有问题"，从而去怀疑模型、数据、转换，
    而真正的 bug 在评估代码里。

    修复：新增 `Detection.bbox_xyxy` 属性，并在评估代码里显式使用。
    """

    def test_bbox_is_xywh_not_xyxy(self) -> None:
        """钉住 bbox 的语义，防止将来被"顺手"改掉。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((333.0, 353.5, 49.8, 67.2), 0.91)]
        )
        with self.make_detector() as detector:
            det = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

        assert det is not None
        # bbox = (x, y, w, h)
        x, y, w, h = det.bbox
        self.assertAlmostEqual(w, 49.8, delta=2)
        self.assertAlmostEqual(h, 67.2, delta=2)
        # w/h 必须为正 —— 如果被当成 xyxy 存，这里会是负数
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_bbox_xyxy_property(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((333.0, 353.5, 49.8, 67.2), 0.91)]
        )
        with self.make_detector() as detector:
            det = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

        assert det is not None
        x1, y1, x2, y2 = det.bbox_xyxy
        self.assertLess(x1, x2, "xyxy 必须满足 x1 < x2")
        self.assertLess(y1, y2, "xyxy 必须满足 y1 < y2")
        # 与 bbox 语义一致
        x, y, w, h = det.bbox
        self.assertEqual((x1, y1), (x, y))
        self.assertEqual((x2, y2), (x + w, y + h))

    def test_iou_with_bbox_xyxy_is_high(self) -> None:
        """用 bbox_xyxy 与真值算 IoU，必须得到高分。

        真值来自真实数据集 img_003：(307,240)-(358,308)
        模型输出 cxcywh (333.0, 353.5, 49.8, 67.2)
        → letterbox 还原后应与其高度重合。
        """
        from src.dataset import GroundTruth, iou

        FakeRKNNLite.scripted_output = script_boxes(
            [((333.0, 353.5, 49.8, 67.2), 0.91)]
        )
        with self.make_detector() as detector:
            det = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        assert det is not None

        gt = GroundTruth(class_id=0, x1=307, y1=240, x2=358, y2=308)
        value = iou(det.bbox_xyxy, gt.bbox)
        self.assertGreater(
            value, 0.9,
            f"用 bbox_xyxy 应得到高 IoU，实际 {value:.3f}",
        )

    def test_using_raw_bbox_for_iou_gives_zero(self) -> None:
        """反证：若误用 bbox（w,h 当 x2,y2），IoU 必然为 0。

        这正是之前的症状，写下来是为了让后来者一眼看懂这个 bug。
        """
        from src.dataset import GroundTruth, iou

        FakeRKNNLite.scripted_output = script_boxes(
            [((333.0, 353.5, 49.8, 67.2), 0.91)]
        )
        with self.make_detector() as detector:
            det = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        assert det is not None

        gt = GroundTruth(class_id=0, x1=307, y1=240, x2=358, y2=308)
        wrong = iou(det.bbox, gt.bbox)      # ← 故意用错
        self.assertEqual(wrong, 0.0, "误用 bbox 会得到 IoU=0，这就是旧 bug")

    def test_match_predictions_with_correct_convention(self) -> None:
        """完整走一遍匹配流程，确认能匹配上。"""
        from src.dataset import GroundTruth, match_predictions

        FakeRKNNLite.scripted_output = script_boxes(
            [((333.0, 353.5, 49.8, 67.2), 0.91)]
        )
        with self.make_detector() as detector:
            dets = detector.detect_all(
                np.zeros((480, 640, 3), dtype=np.uint8)
            )

        predictions = [(d.bbox_xyxy, d.score or 0.0) for d in dets]
        gts = [GroundTruth(class_id=0, x1=307, y1=240, x2=358, y2=308)]
        matched = match_predictions(predictions, gts, 0.5)

        self.assertEqual(len(matched), 1, "应匹配成功")
        self.assertGreater(matched[0], 0.9)


# ──────────────────────────────────────────────────────────────────────
# 与流水线的集成 —— 端到端验证"检测 → 中心坐标 → 串口帧"
# ──────────────────────────────────────────────────────────────────────


class TestPipelineIntegration(CardDetectorTestBase):
    """把 YOLO 检测器接进流水线，验证整条链路。

    这正是本项目的核心需求链路：
        检测到色块 → 算出二维中心 → 映射成游戏坐标 → 组 10 字节帧
    """

    def _make_config(self):
        from src.config import AppConfig

        config = AppConfig()
        config.camera.width = 640
        config.camera.height = 480
        config.detector.engine = "yolo"
        config.mapping.roi_x = 0
        config.mapping.roi_y = 0
        config.mapping.roi_w = 640
        config.mapping.roi_h = 480
        config.mapping.smoothing = None
        config.mapping.deadband = 0.0
        config.loop.target_fps = 1000.0
        config.loop.send_interval = 0.0
        config.debug.preview = False
        config.debug.save_frames = False
        return config

    def test_pipeline_builds_card_detector(self) -> None:
        """engine=yolo 时流水线应构建出 CardDetector。"""
        from src.pipeline import build_detector

        detector = build_detector(self._make_config())
        self.assertIsInstance(detector, CardDetector)

    def test_end_to_end_center_to_serial_frame(self) -> None:
        """合成一张 640x640 检测结果，验证最终发出的游戏坐标。"""
        from src.pipeline import ColorTrackingPipeline
        from src.protocol import parse_frame
        from tests.fakes import FakeCapture, FakeLink

        # 模型输出：中心 (320, 320) —— 画面正中（letterbox 后）
        # 原图 640x480 → scale=1, top=80 → 原图内 y = 320-80 = 240
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 320.0, 120.0, 120.0), 0.92)]
        )

        config = self._make_config()
        capture = FakeCapture([(320, 240)], width=640, height=480)
        link = FakeLink()

        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        frame = capture.read()
        assert frame is not None
        result = pipeline.step(frame)
        pipeline.close()

        self.assertTrue(result.detected, "应检测到目标")
        assert result.mapping is not None
        assert result.frame_bytes is not None

        parsed = parse_frame(result.frame_bytes)
        self.assertTrue(parsed.crc_ok, "CRC 必须通过")
        # 画面正中 → 游戏坐标中点附近（X 35..389 的中点是 212）
        self.assertAlmostEqual(parsed.x, 212, delta=3)
        self.assertAlmostEqual(parsed.y, 292, delta=3)

    def test_pipeline_sends_idle_when_no_card(self) -> None:
        """检测不到卡片时应发 TYPE=02 交还按键控制。"""
        from src.pipeline import ColorTrackingPipeline
        from src.protocol import ControlType, parse_frame
        from tests.fakes import FakeCapture, FakeLink

        FakeRKNNLite.scripted_output = script_boxes([])   # 什么都检不到

        config = self._make_config()
        config.loop.lost_frames = 2
        capture = FakeCapture([None, None, None], width=640, height=480)
        link = FakeLink()

        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        for _ in range(3):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        self.assertIsNotNone(link.last_frame)
        parsed = parse_frame(link.last_frame)
        self.assertEqual(parsed.control_type, ControlType.KEYS_ONLY)

    def test_detected_center_matches_expected(self) -> None:
        """直接检查检测器给出的中心坐标，不经流水线。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((300.0, 200.0, 100.0, 80.0), 0.9)]
        )
        detector = self.make_detector()
        with detector:
            result = detector.detect(
                np.zeros((480, 640, 3), dtype=np.uint8)
            )
        assert result is not None
        # 原图 640x480：scale=1, top=80
        # 中心 x = 300, y = 200-80 = 120
        self.assertAlmostEqual(result.center[0], 300.0, delta=2)
        self.assertAlmostEqual(result.center[1], 120.0, delta=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
