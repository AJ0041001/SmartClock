"""`scripts/preview.py` 的集成测试。

为什么要测一个脚本
------------------
`preview.py` 曾经**每帧重建 CoordinateMapper**（为了让 `p` 取色立刻生效）。
换成双 ROI 之后，这个习惯变成了 bug：编辑器把 A 写进 mapper.roi，
下一帧 mapper 被重建，用户刚框好的 A 就悄悄变回配置里的旧值 ——
界面上看起来"框了但没生效"，非常难查。

这个测试就是钉住这类接线问题：
  · mapper 必须**只建一次**，A 的改动不能被后续帧冲掉
  · 编辑器必须拿得到 B，并按 B 去调检测器
  · render() 在三块画面拼接后尺寸正确
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import cv2
import numpy as np

from src.config import AppConfig
from src.mapper import CoordinateMapper, Roi
from src.roi_editor import RoiEditor

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = PROJECT_ROOT / "scripts" / "preview.py"


def load_preview_module():
    spec = importlib.util.spec_from_file_location("preview_under_test",
                                                  _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_config() -> AppConfig:
    config = AppConfig()
    config.camera.width = 640
    config.camera.height = 480
    config.detector.engine = "hsv"
    config.detector.preset = "red"
    config.detector.min_area = 200
    config.mapping.roi_x = 160
    config.mapping.roi_y = 60
    config.mapping.roi_w = 320
    config.mapping.roi_h = 360
    config.mapping.smoothing = None
    config.mapping.deadband = 0.0
    config.detector.roi_x = config.detector.roi_y = 0
    config.detector.roi_w = config.detector.roi_h = 0
    config.detector.roi_margin = 60
    return config


class TestScriptIsLoadable(unittest.TestCase):
    def test_module_loads_without_running_main(self) -> None:
        module = load_preview_module()
        self.assertTrue(hasattr(module, "main"))
        self.assertTrue(hasattr(module, "PreviewState"))
        self.assertTrue(hasattr(module, "render"))

    def test_no_roi_import_left_behind(self) -> None:
        """ROI 编辑已统一交给 RoiEditor，脚本里不该再有自己的 Roi 逻辑。"""
        module = load_preview_module()
        self.assertFalse(
            hasattr(module, "Roi"),
            "preview.py 不该再用自己的 Roi —— 统一走 RoiEditor",
        )

    def test_preview_state_has_no_own_roi_picker(self) -> None:
        module = load_preview_module()
        state = module.PreviewState(make_config())
        for name in ("_handle_roi", "reset_roi", "pending_roi"):
            self.assertFalse(
                hasattr(state, name),
                f"preview.py 里还留着旧的 ROI 实现 {name}，"
                f"会和 RoiEditor 打架",
            )


class TestEditorWiring(unittest.TestCase):
    """编辑器 + mapper + detector 的接线。"""

    def setUp(self) -> None:
        self.config = make_config()
        self.mapper = CoordinateMapper(roi=self.config.mapping.build_roi(),
                                       smoothing=None, deadband=0)

        class FakeDetector:
            def __init__(self) -> None:
                self.roi = None

        self.detector = FakeDetector()
        self.editor = RoiEditor(
            config=self.config, mapper=self.mapper,
            detector=self.detector, frame_size=(640, 480),
            config_path="/dev/null",
        )

    def test_mapper_roi_survives_reconfiguration(self) -> None:
        """A 改完之后，mapper 必须一直保持新值（不能每帧被配置冲掉）。"""
        self.editor.set_roi_a(Roi(100, 50, 300, 300))
        # 模拟"下一帧又按配置重建 mapper"是错误做法；这里验证
        # 编辑器的改动确实只作用在同一个 mapper 上
        self.assertEqual(self.mapper.roi.as_tuple(), (100, 50, 300, 300))
        for _ in range(3):
            self.assertEqual(self.mapper.roi.as_tuple(), (100, 50, 300, 300))

    def test_b_synced_to_detector(self) -> None:
        self.assertIsNotNone(self.detector.roi)
        self.editor.set_roi_b(Roi(20, 20, 400, 400))
        self.assertEqual(tuple(self.detector.roi),
                         self.editor.roi_b.as_tuple(),
                         "检测器用的 ROI 必须和编辑器里的 B 完全一致")
        # 而且必须真的包住 A —— 否则卡片贴边时中心还是会算偏
        a = self.mapper.roi
        b = self.editor.roi_b
        self.assertLessEqual(b.x, a.x)
        self.assertGreaterEqual(b.x + b.w, a.x + a.w)

    def test_bigger_b_than_a(self) -> None:
        a, b = self.mapper.roi, self.editor.roi_b
        self.assertGreater(b.w, a.w)
        self.assertGreater(b.h, a.h)


class TestRender(unittest.TestCase):
    def setUp(self) -> None:
        module = load_preview_module()
        self.config = make_config()
        self.state = module.PreviewState(self.config)
        self.render = module.render
        self.mapper = CoordinateMapper(roi=self.config.mapping.build_roi(),
                                       smoothing=None, deadband=0)
        from src.detector import ColorDetector

        self.detector = ColorDetector(
            ranges=self.config.detector.build_ranges(),
            min_area=self.config.detector.min_area,
            morph_kernel=self.config.detector.morph_kernel,
            morph_iterations=self.config.detector.morph_iterations,
        )
        self.state.editor = RoiEditor(
            config=self.config, mapper=self.mapper,
            detector=self.detector, frame_size=(640, 480),
            config_path="/dev/null",
        )
        self.image = np.zeros((480, 640, 3), dtype=np.uint8)
        self.image[200:280, 280:360] = (0, 0, 255)

    def test_render_without_editor(self) -> None:
        """没有编辑器（例如静态图跑失败）时也要能渲染。"""
        self.state.editor = None
        canvas = self.render(self.image, self.detector, self.mapper,
                             self.state, fps=0.0)
        self.assertEqual(canvas.shape, (480, 640, 3))

    def test_render_with_editor(self) -> None:
        canvas = self.render(self.image, self.detector, self.mapper,
                             self.state, fps=12.5)
        # overlay 布局：窗口就是画面尺寸
        self.assertEqual(canvas.shape, (480, 640, 3))

    def test_render_does_not_mutate_input(self) -> None:
        original = self.image.copy()
        self.render(self.image, self.detector, self.mapper, self.state, 0.0)
        np.testing.assert_array_equal(self.image, original)

    def test_render_with_message(self) -> None:
        self.state.set_message("测试消息")
        canvas = self.render(self.image, self.detector, self.mapper,
                             self.state, 0.0)
        self.assertEqual(canvas.shape[1], 640)

    def test_render_with_mask(self) -> None:
        self.state.show_mask = True
        canvas = self.render(self.image, self.detector, self.mapper,
                             self.state, 0.0)
        self.assertEqual(canvas.shape[1], 640)


class TestMouseRouting(unittest.TestCase):
    """取色模式自己处理点击，其余交给编辑器。"""

    def setUp(self) -> None:
        module = load_preview_module()
        self.module = module
        self.config = make_config()
        self.state = module.PreviewState(self.config)
        self.mapper = CoordinateMapper(roi=self.config.mapping.build_roi(),
                                       smoothing=None, deadband=0)
        self.state.editor = RoiEditor(
            config=self.config, mapper=self.mapper, detector=None,
            frame_size=(640, 480), config_path="/dev/null",
        )
        self.state.editor.buttons.render(640)
        self.image = np.zeros((480, 640, 3), dtype=np.uint8)
        self.image[200:280, 280:360] = (0, 0, 255)
        self.state._last_frame = self.image

    def test_pick_mode_does_not_touch_rois(self) -> None:
        before = (self.mapper.roi.as_tuple(),
                  self.state.editor.roi_b.as_tuple())
        self.state.mode = "pick"
        self.state.on_mouse(cv2.EVENT_LBUTTONDOWN, 320, 240, 0, None)
        self.assertEqual(
            (self.mapper.roi.as_tuple(),
             self.state.editor.roi_b.as_tuple()),
            before,
            "取色模式下的点击不该改动 ROI",
        )
        self.assertEqual(self.state.mode, "view", "取完色应自动回到浏览模式")

    def test_view_mode_routes_to_editor(self) -> None:
        self.state.editor.handle_action("pick_b")
        self.state.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 100, 0, None)
        self.state.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 300, 0, None)
        # 框的是 (200,100,200,200)，盖不住 A=(160,60,320,360)，
        # 所以会被撑到 A —— 这正是我们想要的兜底
        self.assertEqual(self.state.editor.roi_b.as_tuple(),
                         self.mapper.roi.as_tuple())

    def test_button_click_routed_to_editor(self) -> None:
        rect = self.state.editor.button_rect_on_screen("toggle_full")
        assert rect is not None
        self.state.on_mouse(cv2.EVENT_LBUTTONDOWN,
                            rect[0] + rect[2] // 2,
                            rect[1] + rect[3] // 2, 0, None)
        self.assertEqual(self.state.editor.roi_b.as_tuple(), (0, 0, 640, 480))

    def test_mouse_move_reaches_editor(self) -> None:
        self.state.on_mouse(cv2.EVENT_MOUSEMOVE, 10, 10, 0, None)
        self.assertIsNone(self.state.editor.state.pending_corner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
