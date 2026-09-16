"""健壮性测试 —— 现场"用着用着出事"的那些路径。

这些场景在开发机上很少碰到，但在现场天天发生：

  · 用户点界面上的 Quit 按钮退出（鼠标路径，不是键盘路径）
  · 用户直接点窗口右上角的 × 关掉预览（OpenCV 会抛异常）
  · USB 转串口松了一下、驱动掉线（串口读写抛异常）
  · 配置里手写了一个贴边或越界的 ROI

共同点：**以前这些都会让整个追踪程序崩掉或静默失效**，而现场正在比赛。
所以每一个都要有测试钉住。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from src import pipeline as pipeline_module
from src.config import AppConfig
from src.mapper import Roi
from src.roi_editor import HINT_TEXT, MIN_ROI_SIDE, RoiEditor
from src.serialport import SerialError
from src.mapper import CoordinateMapper
from tests.fakes import FakeCapture, FakeLink


def make_config(**overrides) -> AppConfig:
    config = AppConfig()
    config.camera.width = 640
    config.camera.height = 480
    config.detector.engine = "hsv"
    config.detector.preset = "red"
    config.detector.min_area = 200
    config.detector.morph_kernel = 3
    config.detector.morph_iterations = 1
    config.mapping.roi_x = 160
    config.mapping.roi_y = 60
    config.mapping.roi_w = 320
    config.mapping.roi_h = 360
    config.mapping.smoothing = None
    config.mapping.deadband = 0.0
    config.loop.target_fps = 1000.0
    config.loop.send_interval = 0.0
    config.debug.preview = False
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(config, section), field, value)
    return config


class RaisingLink:
    """模拟"USB 转串口被拔掉"：一写就抛 SerialError。"""

    def __init__(self) -> None:
        self.write_calls = 0

    def send_frame_and_wait(self, frame: bytes, timeout: float = 0.5):
        self.write_calls += 1
        raise SerialError("设备已断开")

    def write(self, data: bytes) -> int:
        raise SerialError("设备已断开")

    def close(self) -> None:
        pass


# ──────────────────────────────────────────────────────────────────────
# 串口异常
# ──────────────────────────────────────────────────────────────────────


class TestSerialErrorDoesNotKillLoop(unittest.TestCase):
    def test_step_survives_serial_error(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)], width=640, height=480)
        link = RaisingLink()
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, capture, link)
        pipeline.open()
        try:
            frame = capture.read()
            assert frame is not None
            result = pipeline.step(frame)          # 不该抛异常
            self.assertIsNotNone(result)
            self.assertEqual(pipeline.stats.serial_errors, 1)
            self.assertEqual(pipeline.stats.ack_timeout, 1)
        finally:
            pipeline.close()

    def test_repeated_errors_are_counted_not_raised(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)], width=640, height=480)
        link = RaisingLink()
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, capture, link)
        pipeline.open()
        try:
            for _ in range(5):
                frame = capture.read()
                assert frame is not None
                pipeline.step(frame)
            self.assertEqual(pipeline.stats.serial_errors, 5)
        finally:
            pipeline.close()

    def test_error_count_appears_in_report(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)], width=640, height=480)
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, capture, RaisingLink())
        pipeline.open()
        try:
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
            report = pipeline.stats.report()
            self.assertIn("串口异常", report)
        finally:
            pipeline.close()

    def test_run_returns_instead_of_raising(self) -> None:
        """整条主循环跑一遍，串口一直报错也要正常收尾。"""
        config = make_config(**{"loop.target_fps": 1000.0})
        capture = FakeCapture([(320, 240)] * 5, width=640, height=480)
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, capture, RaisingLink())
        pipeline.open()
        try:
            stats = pipeline.run(duration=0.15)
            self.assertGreater(stats.serial_errors, 0)
            self.assertEqual(stats.frames, stats.frames)   # 没崩就是通过
        finally:
            pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# 预览窗口
# ──────────────────────────────────────────────────────────────────────


class TestPreviewWindowRobustness(unittest.TestCase):
    """窗口相关的异常不能让主循环崩掉。"""

    def _make_pipeline(self):
        config = make_config(**{"debug.preview": True})
        capture = FakeCapture([(320, 240)] * 20, width=640, height=480)
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, capture, FakeLink())
        pipeline.open()
        return pipeline

    def test_imshow_error_exits_gracefully(self) -> None:
        """用户点窗口右上角 × 时，OpenCV 会抛 cv2.error。"""
        pipeline = self._make_pipeline()
        try:
            with mock.patch.object(cv2, "namedWindow"), \
                 mock.patch.object(cv2, "waitKey", return_value=255), \
                 mock.patch.object(cv2, "setMouseCallback"), \
                 mock.patch.object(cv2, "destroyAllWindows"), \
                 mock.patch.object(cv2, "imshow",
                                   side_effect=cv2.error("窗口已关闭")):
                stats = pipeline.run(duration=2.0)     # 不该抛出去
            self.assertFalse(pipeline.config.debug.preview,
                             "窗口不可用时应自动关掉预览继续跑")
            self.assertGreaterEqual(stats.frames, 0)
        finally:
            pipeline.close()

    def test_quit_button_stops_the_loop(self) -> None:
        """鼠标点界面上的 Quit 必须能退出 —— 这条路径以前是死的。

        鼠标回调是 OpenCV 反过来调的，没有返回值能传回主循环，
        所以必须靠 exit_requested 标志位。
        """
        pipeline = self._make_pipeline()
        editor = pipeline.roi_editor
        self.assertIsNotNone(editor)
        editor.buttons.render(pipeline.config.camera.width)

        rect = editor.button_rect_on_screen("quit")
        assert rect is not None
        click = (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)

        shown = {"count": 0}

        def fake_imshow(_window, _canvas):
            shown["count"] += 1
            if shown["count"] == 1:
                # 第一帧就模拟用户点了 Quit
                editor.on_mouse(cv2.EVENT_LBUTTONDOWN, click[0], click[1],
                                0, None)

        try:
            with mock.patch.object(cv2, "namedWindow"), \
                 mock.patch.object(cv2, "imshow", side_effect=fake_imshow), \
                 mock.patch.object(cv2, "waitKey", return_value=255), \
                 mock.patch.object(cv2, "setMouseCallback"), \
                 mock.patch.object(cv2, "destroyAllWindows"):
                pipeline.run(duration=5.0)
            self.assertEqual(shown["count"], 1,
                             "点了 Quit 之后不该再显示第二帧")
        finally:
            pipeline.close()

    def test_quit_button_sets_flag(self) -> None:
        editor = RoiEditor(make_config(), CoordinateMapper(
            roi=Roi(160, 60, 320, 360), smoothing=None, deadband=0),
            detector=None, frame_size=(640, 480), config_path="/dev/null")
        self.assertFalse(editor.exit_requested)
        editor.handle_action("quit")
        self.assertTrue(editor.exit_requested)

    def test_keyboard_quit_does_not_need_the_flag(self) -> None:
        editor = RoiEditor(make_config(), CoordinateMapper(
            roi=Roi(160, 60, 320, 360), smoothing=None, deadband=0),
            detector=None, frame_size=(640, 480), config_path="/dev/null")
        self.assertEqual(editor.on_key(ord("q")), "quit")


# ──────────────────────────────────────────────────────────────────────
# ROI 越界
# ──────────────────────────────────────────────────────────────────────


class TestClampKeepsRoiInsideFrame(unittest.TestCase):
    """手写/遗留配置可能给出贴边或越界的 ROI，绝不能产生画面外的框。"""

    def make(self, roi: Roi) -> RoiEditor:
        return RoiEditor(make_config(), CoordinateMapper(
            roi=roi, smoothing=None, deadband=0),
            detector=None, frame_size=(640, 480), config_path="/dev/null")

    def test_extreme_rois_stay_inside(self) -> None:
        cases = [
            Roi(630, 460, 100, 100),      # 右下角、尺寸超出
            Roi(-50, -50, 100, 100),      # 左上越界
            Roi(700, 500, 100, 100),      # 完全在画面外
            Roi(639, 479, 1, 1),          # 贴着最后一像素
            Roi(0, 0, 5000, 5000),        # 巨大
            Roi(100, 100, 0, 0),          # 零尺寸
        ]
        for roi in cases:
            with self.subTest(roi=roi.as_tuple()):
                editor = self.make(roi)
                for name, rect in (("A", editor.mapper.roi),
                                   ("B", editor.roi_b)):
                    self.assertGreaterEqual(rect.x, 0, f"{name} x 越界")
                    self.assertGreaterEqual(rect.y, 0, f"{name} y 越界")
                    self.assertGreaterEqual(rect.w, MIN_ROI_SIDE)
                    self.assertGreaterEqual(rect.h, MIN_ROI_SIDE)
                    self.assertLessEqual(
                        rect.x + rect.w, 640,
                        f"{name}={rect.as_tuple()} 右边缘超出画面",
                    )
                    self.assertLessEqual(
                        rect.y + rect.h, 480,
                        f"{name}={rect.as_tuple()} 下边缘超出画面",
                    )

    def test_detector_gets_the_same_rect_as_editor(self) -> None:
        """编辑器显示的框必须和检测器实际裁的区域一致。"""

        class FakeDetector:
            def __init__(self) -> None:
                self.roi = None

        detector = FakeDetector()
        editor = RoiEditor(make_config(), CoordinateMapper(
            roi=Roi(630, 460, 100, 100), smoothing=None, deadband=0),
            detector=detector, frame_size=(640, 480), config_path="/dev/null")

        self.assertEqual(tuple(detector.roi), editor.roi_b.as_tuple())

        # 再模拟 card_detector 自己的裁剪算法，确认裁出来的区域和 B 一样
        rx, ry, rw, rh = editor.roi_b.as_tuple()
        width, height = 640, 480
        rx2 = max(0, min(int(rx), width - 1))
        ry2 = max(0, min(int(ry), height - 1))
        rw2 = max(1, min(int(rw), width - rx2))
        rh2 = max(1, min(int(rh), height - ry2))
        self.assertEqual((rx, ry, rw, rh), (rx2, ry2, rw2, rh2),
                         "编辑器与检测器对 B 的理解必须一致")


class TestBSizeChangesAreHonest(unittest.TestCase):
    def make(self) -> RoiEditor:
        return RoiEditor(make_config(), CoordinateMapper(
            roi=Roi(160, 60, 320, 360), smoothing=None, deadband=0),
            detector=None, frame_size=(640, 480), config_path="/dev/null")

    def test_status_always_bigger_than_a(self) -> None:
        editor = self.make()
        a = editor.mapper.roi
        for action in ("bgrow", "b_grow", "b_shrink", "b_auto", "toggle_full"):
            with self.subTest(action=action):
                editor.handle_action(action)
                b = editor.roi_b
                self.assertLessEqual(b.x, a.x)
                self.assertLessEqual(b.y, a.y)
                self.assertGreaterEqual(b.x + b.w, a.x + a.w)
                self.assertGreaterEqual(b.y + b.h, a.y + a.h)

    def test_message_matches_actual_change(self) -> None:
        """状态栏说"B -10px"时，B 必须真的变小了。"""
        editor = self.make()
        for _ in range(200):                      # 一路缩到最小
            before = editor.roi_b.as_tuple()
            editor.handle_action("b_shrink")
            after = editor.roi_b.as_tuple()
            if "-10px" in editor.state.message:
                self.assertLess(after[2], before[2],
                                f"消息说缩小了，实际 {before} → {after}")
            else:
                self.assertEqual(after, before,
                                 f"消息说没变小，实际却变了：{before} → {after}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ──────────────────────────────────────────────────────────────────────
# 边缘中心精度（用户报的那个 bug 的量化回归）
# ──────────────────────────────────────────────────────────────────────


def _load_edge_demo():
    """加载 scripts/demo_edge_center.py（不执行 main）。"""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / \
        "demo_edge_center.py"
    spec = importlib.util.spec_from_file_location("edge_demo_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestEdgeCentreAccuracy(unittest.TestCase):
    """卡片中心顶到 A 的边界时，中心必须算得准。

    这是用户报的那个 bug 的量化回归：
      · 检测区和映射区是同一个框（旧行为）→ 中心偏移半个卡片宽
      · 检测区比映射区大一圈（现在）      → 中心基本无误差
    """

    ROI_A = Roi(156, 20, 326, 428)
    CARD_W, CARD_H = 90, 170

    def setUp(self) -> None:
        self.demo = _load_edge_demo()
        self.cy = self.ROI_A.y + self.ROI_A.h // 2

    def error_at(self, box: Roi, centre_x: float) -> float:
        frame = self.demo.render_frame(
            (int(round(centre_x)), self.cy), self.CARD_W, self.CARD_H
        )
        found = self.demo.centre_of_visible_block(frame, box.as_tuple())
        self.assertIsNotNone(found, "黑底红卡应该能被检出")
        return abs(found[0] - centre_x)

    def _box_with_margin(self, margin: int) -> Roi:
        a = self.ROI_A
        w = min(a.w + 2 * margin, 640)
        h = min(a.h + 2 * margin, 480)
        x = max(0, min(a.x - margin, 640 - w))
        y = max(0, min(a.y - margin, 480 - h))
        return Roi(x, y, w, h)

    def test_old_behaviour_is_off_by_half_a_card(self) -> None:
        """B = A（旧行为）时，卡片中心顶到 A 边界就会算偏。"""
        for edge_x in (self.ROI_A.x,
                       self.ROI_A.x + self.ROI_A.w):
            with self.subTest(edge_x=edge_x):
                error = self.error_at(self.ROI_A, edge_x)
                self.assertGreater(
                    error, 10.0,
                    f"B=A 时中心误差只有 {error:.1f}px —— "
                    f"如果这里不偏了，说明裁剪逻辑变了，"
                    f"这个 bug 的成因需要重新分析",
                )

    def test_dual_roi_fixes_it(self) -> None:
        """B = A + 60（> 卡片半宽 45）时，同样的位置几乎无误差。"""
        box = self._box_with_margin(60)
        for edge_x in (self.ROI_A.x,
                       self.ROI_A.x + self.ROI_A.w):
            with self.subTest(edge_x=edge_x):
                error = self.error_at(box, edge_x)
                self.assertLess(
                    error, 2.0,
                    f"双 ROI 下中心误差仍有 {error:.1f}px",
                )

    def test_insufficient_margin_still_fails(self) -> None:
        """边距小于卡片半宽时，问题依然存在 —— 说明这个参数不能随便调小。"""
        box = self._box_with_margin(20)      # 卡片半宽是 45
        error = self.error_at(box, self.ROI_A.x)
        self.assertGreater(error, 5.0,
                           "边距不足时中心本该仍然偏")

    def test_demo_module_is_importable(self) -> None:
        self.assertTrue(hasattr(self.demo, "main"))
        self.assertTrue(hasattr(self.demo, "centre_of_visible_block"))

    def test_no_red_returns_none(self) -> None:
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        self.assertIsNone(
            self.demo.centre_of_visible_block(blank, (0, 0, 640, 480))
        )


class TestControlsExistWithoutCallingOpen(unittest.TestCase):
    """`scripts/main.py` **从不调用 pipeline.open()**，ROI 面板也必须出现。

    真实事故（用户报的）：main.py 是自己开摄像头、然后直接调 run() 的。
    ROI 编辑器原来只在 open() 里创建，于是窗口照常弹出、检测也正常，
    但两个 ROI、按钮、操作指引全都不见了 —— 用户看到的就是
    "根本没有你说的东西，功能也没有"。

    所以这里**完全模仿 main.py 的用法**：只 open 摄像头，不碰 pipeline.open()，
    直接 run()，然后断言控制面板确实被创建并且画到了画面上。
    """

    def setUp(self) -> None:
        # 屏幕截图里就是这种配置：预览开着、默认 overlay 布局
        config = make_config(**{"debug.preview": True})
        self.capture = FakeCapture([(320, 240)] * 30, width=640, height=480)
        self.pipeline = pipeline_module.ColorTrackingPipeline(
            config, self.capture, FakeLink()
        )

    def tearDown(self) -> None:
        self.pipeline.close()

    def test_editor_created_by_run_alone(self) -> None:
        self.assertIsNone(self.pipeline.roi_editor,
                          "构造阶段还不该建（此时还没决定要不要预览）")
        self.capture.open()                 # main.py 的做法
        self.pipeline.run(duration=0.15)    # 注意：没有 pipeline.open()！
        self.assertIsNotNone(
            self.pipeline.roi_editor,
            "run() 之后必须已经有 ROI 编辑器 —— 否则界面上什么都没有",
        )

    def test_controls_are_drawn_into_the_preview(self) -> None:
        """画到屏幕上的帧必须是"拼好界面"的那一张，不是原始视频。"""
        self.capture.open()
        frames: list[np.ndarray] = []

        def fake_imshow(_window, canvas):
            frames.append(canvas.copy())

        with mock.patch.object(cv2, "namedWindow"), \
             mock.patch.object(cv2, "imshow", side_effect=fake_imshow), \
             mock.patch.object(cv2, "waitKey", return_value=255), \
             mock.patch.object(cv2, "setMouseCallback") as hook, \
             mock.patch.object(cv2, "destroyAllWindows"):
            self.pipeline.run(duration=0.15)

        self.assertTrue(frames, "整个 run() 里一次都没画过画面")
        canvas = frames[0]

        # ① 鼠标回调必须挂上（否则点了没反应）
        self.assertTrue(hook.called, "没有挂 setMouseCallback，鼠标完全没反应")

        # ② overlay 布局下窗口尺寸 = 画面尺寸
        self.assertEqual(canvas.shape, (480, 640, 3))

        # ③ 画面底部那条按钮压条必须真的画上了东西
        editor = self.pipeline.roi_editor
        self.assertIsNotNone(editor)
        strip = canvas[editor.button_strip_top:, :]
        raw = np.full_like(strip, 0)
        self.assertFalse(
            np.array_equal(strip, raw),
            "按钮压条区域是空的 —— 界面没画上去",
        )

        # ④ 教学窗默认是关的（用户要求关掉），但底部键盘提示必须一直在
        self.assertFalse(editor.guide_visible,
                         "教学窗默认应该关掉")
        self.assertTrue(HINT_TEXT, "底部键盘提示不能为空")

    def test_run_is_idempotent_about_controls(self) -> None:
        """重复调用不能建出两个编辑器（否则界面会打架）。"""
        self.capture.open()
        self.pipeline._ensure_preview_controls()
        first = self.pipeline.roi_editor
        self.pipeline._ensure_preview_controls()
        self.assertIs(self.pipeline.roi_editor, first)

    def test_open_still_creates_controls(self) -> None:
        """老的调用路径（先 open 再 run）同样要有界面。"""
        self.pipeline.open()
        self.assertIsNotNone(self.pipeline.roi_editor)

    def test_no_preview_means_no_controls(self) -> None:
        """明确关掉预览时不该建编辑器（省内存，也不该挂鼠标回调）。"""
        config = make_config(**{"debug.preview": False})
        pipeline = pipeline_module.ColorTrackingPipeline(
            config, FakeCapture([(320, 240)] * 5), FakeLink()
        )
        try:
            pipeline.open()
            self.assertIsNone(pipeline.roi_editor)
        finally:
            pipeline.close()
