"""预览界面与流水线的集成测试。

单测 `roi_editor` 和 `ui_buttons` 各自都没问题，但真正容易出错的是
**接线**：

  · 流水线有没有把检测框喂给编辑器（否则"B 太小"永远不报警）
  · 编辑器改完 B 有没有真的作用到检测器（否则界面调了半天没效果）
  · 三块画面（视频 / 按钮条 / 状态栏）拼起来尺寸对不对
  · 鼠标坐标从"整窗坐标"换算到"按钮条坐标"有没有算错

这个文件专门盯这些接线点。全部离线运行：用合成图像 + HSV 引擎，
不需要摄像头、不需要 NPU、不需要 STM32。
"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from src.config import AppConfig
from src.mapper import Roi
from src.pipeline import ColorTrackingPipeline
from src.roi_editor import RoiEditor
from tests.fakes import FakeCapture, FakeLink


def make_config(margin: int = 60) -> AppConfig:
    config = AppConfig()
    config.camera.width = 640
    config.camera.height = 480
    config.detector.engine = "hsv"
    config.detector.preset = "red"
    config.detector.min_area = 200
    config.detector.morph_kernel = 3
    config.detector.morph_iterations = 1
    # A 取画面内缩一圈，方便观察 B 的扩张
    config.mapping.roi_x = 160
    config.mapping.roi_y = 60
    config.mapping.roi_w = 320
    config.mapping.roi_h = 360
    config.mapping.smoothing = None
    config.mapping.deadband = 0.0
    config.detector.roi_x = 0
    config.detector.roi_y = 0
    config.detector.roi_w = 0
    config.detector.roi_h = 0
    config.detector.roi_margin = margin
    config.loop.target_fps = 1000.0
    config.loop.send_interval = 0.0
    config.debug.preview = False          # 无头环境下不真的开窗口
    return config


def make_preview_pipeline(config: AppConfig | None = None):
    """建一条流水线，并手工装上 ROI 编辑器（模拟 --preview 的情形）。"""
    config = config or make_config()
    capture = FakeCapture([(320, 240)], width=640, height=480, block_size=60)
    link = FakeLink()
    pipeline = ColorTrackingPipeline(config, capture, link)
    pipeline.open()
    pipeline.roi_editor = RoiEditor(
        config=config,
        mapper=pipeline.mapper,
        detector=pipeline.detector,
        frame_size=(640, 480),
        config_path="/dev/null",
    )
    return pipeline, capture, link


class TestDetectorRoiWiring(unittest.TestCase):
    """编辑器必须真的把 B 交给检测器，否则界面调参是白调的。"""

    def test_b_matches_between_editor_and_detector(self) -> None:
        pipeline, _, _ = make_preview_pipeline()
        try:
            self.assertIsNotNone(pipeline.detector.roi)
            self.assertEqual(
                tuple(pipeline.detector.roi),
                pipeline.roi_editor.roi_b.as_tuple(),
                "编辑器里的 B 与检测器实际使用的 ROI 必须一致",
            )
        finally:
            pipeline.close()

    def test_b_larger_than_a_in_pipeline(self) -> None:
        pipeline, _, _ = make_preview_pipeline()
        try:
            a = pipeline.roi_editor.mapper.roi
            b = pipeline.roi_editor.roi_b
            self.assertGreater(b.w, a.w)
            self.assertGreater(b.h, a.h)
        finally:
            pipeline.close()

    def test_changing_b_updates_detector(self) -> None:
        pipeline, _, _ = make_preview_pipeline()
        try:
            pipeline.roi_editor.set_roi_b(Roi(10, 10, 300, 300))
            self.assertEqual(tuple(pipeline.detector.roi),
                             pipeline.roi_editor.roi_b.as_tuple())
        finally:
            pipeline.close()

    def test_b_always_contains_a(self) -> None:
        """界面上不管怎么调，B 都必须包住 A —— 这是功能成立的前提。"""
        pipeline, _, _ = make_preview_pipeline()
        try:
            editor = pipeline.roi_editor
            a = editor.mapper.roi
            attempts = [
                Roi(10, 10, 300, 300),      # 太小
                Roi(a.x, a.y, 10, 10),      # 极小
                Roi(a.x + 50, a.y + 50, 80, 80),   # 完全在 A 内部
                Roi(0, 0, 640, 480),        # 整幅画面
            ]
            for attempt in attempts:
                with self.subTest(roi=attempt.as_tuple()):
                    editor.set_roi_b(attempt)
                    b = editor.roi_b
                    self.assertLessEqual(b.x, a.x, f"B={b} 不含 A={a}")
                    self.assertLessEqual(b.y, a.y)
                    self.assertGreaterEqual(b.x + b.w, a.x + a.w)
                    self.assertGreaterEqual(b.y + b.h, a.y + a.h)
        finally:
            pipeline.close()

    def test_moving_a_outwards_grows_b(self) -> None:
        """把 A 拉大之后，B 必须跟着撑大，不能留下"裁掉卡片"的缝。"""
        pipeline, _, _ = make_preview_pipeline()
        try:
            editor = pipeline.roi_editor
            editor.set_roi_a(Roi(20, 20, 600, 440))
            b = editor.roi_b
            self.assertLessEqual(b.x, 20)
            self.assertLessEqual(b.y, 20)
            self.assertGreaterEqual(b.x + b.w, 620)
            self.assertGreaterEqual(b.y + b.h, 460)
            self.assertEqual(tuple(pipeline.detector.roi), b.as_tuple())
        finally:
            pipeline.close()


class TestPreviewComposition(unittest.TestCase):
    """三块画面拼接 + 检测框回传。"""

    def setUp(self) -> None:
        self.pipeline, self.capture, _ = make_preview_pipeline()
        self.addCleanup(self.pipeline.close)

    def step_one(self):
        frame = self.capture.read()
        assert frame is not None
        return self.pipeline.step(frame)

    def test_frame_shape(self) -> None:
        result = self.step_one()
        canvas = self.pipeline.render(self.capture.read(), result)
        self.assertEqual(canvas.shape, (480, 640, 3))

    def test_full_preview_chain(self) -> None:
        """完全按主循环里的顺序走一遍。"""
        result = self.step_one()
        frame = self.capture.read()
        assert frame is not None

        canvas = self.pipeline.render(frame, result)
        editor = self.pipeline.roi_editor
        editor.note_detection(
            result.detection.bbox_xyxy if result.detection else None
        )
        canvas = editor.draw_rois(canvas)
        canvas = editor.overlay_hint(canvas)
        canvas = editor.compose(canvas)

        # 默认 overlay 布局：窗口尺寸 = 画面尺寸，按钮不可能跑到屏幕外
        self.assertEqual(canvas.shape, (480, 640, 3))
        self.assertEqual(canvas.dtype, np.uint8)

    def test_detection_bbox_reaches_editor(self) -> None:
        """色块在画面正中：检测框不该贴到 B 的边界。"""
        result = self.step_one()
        self.assertIsNotNone(result.detection, "HSV 应该能检出中间的红色方块")
        self.pipeline.roi_editor.note_detection(result.detection.bbox_xyxy)
        self.assertFalse(self.pipeline.roi_editor.clipped)

    def test_edge_block_triggers_warning_when_b_too_small(self) -> None:
        """色块贴 A 边界 + B 太小 → 必须报警。

        这正是用户遇到的问题：色块中心顶到映射区域边缘时，
        检测区域如果没比 A 大一圈，色块会被裁掉一部分。
        """
        editor = self.pipeline.roi_editor
        # 把 B 设成和 A 一样大（旧行为）
        editor.set_roi_b(editor.mapper.roi)

        a = editor.mapper.roi
        block_x = a.x                      # 色块中心顶到 A 的左边界
        height, width = 480, 640
        image = np.zeros((height, width, 3), dtype=np.uint8)
        half = 30
        image[a.y + a.h // 2 - half:a.y + a.h // 2 + half,
              max(0, block_x - half):block_x + half] = (0, 0, 255)

        detection = self.pipeline.detector.detect(image)
        self.assertIsNotNone(detection)
        editor.note_detection(detection.bbox_xyxy)
        self.assertTrue(
            editor.clipped,
            "色块贴边且 B == A 时必须报警，否则用户根本发现不了",
        )
        self.assertTrue(editor.warning)


class TestMouseCoordinates(unittest.TestCase):
    """鼠标回调收到的是整窗坐标，换算错了按钮就点不动。"""

    def setUp(self) -> None:
        self.pipeline, _, _ = make_preview_pipeline()
        self.addCleanup(self.pipeline.close)
        self.editor = self.pipeline.roi_editor
        self.editor.buttons.render(640)

    def test_click_button_by_absolute_coordinates(self) -> None:
        """按钮条画在画面内部，回调收到的是窗口坐标 —— 换算必须对。"""
        rect = self.editor.button_rect_on_screen("b_grow")
        assert rect is not None
        x, y, w, h = rect

        before = self.editor.roi_b.w
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, x + w // 2,
                             y + h // 2, 0, None)
        self.assertGreater(self.editor.roi_b.w, before)

    def test_click_just_above_button_strip_is_video(self) -> None:
        """压条上方的最后一像素仍属于画面。"""
        self.editor.handle_action("pick_b")
        top = self.editor.button_strip_top
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 320, top - 1, 0, None)
        self.assertEqual(self.editor.state.pending_corner, (320, top - 1))

    def test_click_on_button_does_not_become_a_corner(self) -> None:
        """不在框选时，点按钮就是点按钮，不该被当成框选的角。"""
        self.editor.handle_action("pick_b")
        self.editor.state.cancel()               # 退出框选状态
        rect = self.editor.button_rect_on_screen("b_grow")
        assert rect is not None
        x, y, w, h = rect
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, x + w // 2,
                             y + h // 2, 0, None)
        self.assertIsNone(self.editor.state.pending_corner)

    def test_buttons_do_not_steal_clicks_while_selecting(self) -> None:
        """框选过程中整幅画面都归框选用。

        否则想点画面最上/最下边缘的角时会被按钮吃掉，框不出来。
        """
        self.editor.handle_action("pick_a")
        rect = self.editor.button_rect_on_screen("b_grow")
        assert rect is not None
        x, y, w, h = rect
        before = self.editor.roi_b.w
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, x + w // 2,
                             y + h // 2, 0, None)
        self.assertEqual(self.editor.roi_b.w, before, "按钮抢走了框选的点击")
        self.assertEqual(self.editor.state.pending_corner,
                         (x + w // 2, y + h // 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
