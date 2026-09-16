"""双 ROI 编辑器测试。

这两个矩形是本项目**解决"边缘星星接不到"的关键**，逻辑必须锁死：

  · A（映射区域）：决定像素坐标 ↔ 游戏坐标的对应关系
  · B（检测区域）：决定往模型里送多大范围，必须比 A 大一圈

核心断言是 **B 严格包含 A**。如果哪天有人图省事把两者合并成一个，
这里的测试会立刻报警 —— 因为那正是"色块出界后中心算成可见部分中心"
这个 bug 的成因。
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

import cv2
import numpy as np

from src.config import AppConfig
from src.mapper import CoordinateMapper, Roi
from src.roi_editor import (
    B_STEP,
    CLIP_TOLERANCE,
    HINT_TEXT,
    MIN_ROI_SIDE,
    DualRoiState,
    EditTarget,
    RoiEditor,
)

FRAME_W, FRAME_H = 640, 480


def make_config(roi=(156, 20, 326, 428), margin: int = 40) -> AppConfig:
    """构造一份配置：A 显式给定，B 留空（走 A+margin 自动推导）。"""
    config = AppConfig()
    config.mapping.roi_x, config.mapping.roi_y = roi[0], roi[1]
    config.mapping.roi_w, config.mapping.roi_h = roi[2], roi[3]
    config.detector.roi_margin = margin
    config.detector.roi_x = config.detector.roi_y = 0
    config.detector.roi_w = config.detector.roi_h = 0
    return config


def make_editor(config: AppConfig | None = None,
                detector=None,
                frame_size=(FRAME_W, FRAME_H)) -> RoiEditor:
    config = config or make_config()
    mapper = CoordinateMapper(
        roi=config.mapping.build_roi(), smoothing=None, deadband=0
    )
    # config_path 指向 /dev/null：测试里按 Save 不该污染仓库的 config.yaml
    return RoiEditor(config, mapper, detector=detector, frame_size=frame_size,
                     config_path="/dev/null")


def make_resize_editor() -> RoiEditor:
    """给"调 B 大小"的测试用：A 小且居中，B 有余量可自由扩缩。

    如果沿用默认的 A（几乎占满画面），B 会被"必须包含 A"这条不变式
    撑到贴边，就没有扩缩空间了 —— 那是另一个测试要覆盖的事。
    """
    return make_editor(make_config(roi=(300, 220, 40, 40), margin=40))


def center_of(roi: Roi) -> tuple[float, float]:
    return (roi.x + roi.w / 2.0, roi.y + roi.h / 2.0)


# ──────────────────────────────────────────────────────────────────────
# 状态机
# ──────────────────────────────────────────────────────────────────────


class TestDualRoiState(unittest.TestCase):
    def setUp(self) -> None:
        self.state = DualRoiState()

    def test_initial(self) -> None:
        self.assertIs(self.state.target, EditTarget.NONE)
        self.assertFalse(self.state.selecting)
        self.assertFalse(self.state.dirty)
        self.assertIsNone(self.state.pending_corner)

    def test_start_each_target(self) -> None:
        for target in (EditTarget.A, EditTarget.B):
            with self.subTest(target=target):
                state = DualRoiState()
                state.start(target)
                self.assertIs(state.target, target)
                self.assertTrue(state.selecting)
                self.assertIsNone(state.pending_corner)
                self.assertTrue(state.message, "应给出操作提示")

    def test_click_ignored_when_not_selecting(self) -> None:
        self.assertIsNone(self.state.click(100, 100))
        self.assertIsNone(self.state.pending_corner)
        self.assertFalse(self.state.dirty)

    def test_two_clicks_produce_roi(self) -> None:
        self.state.start(EditTarget.A)
        self.assertIsNone(self.state.click(100, 80))
        self.assertEqual(self.state.pending_corner, (100, 80))

        result = self.state.click(300, 260)
        self.assertIsNotNone(result)
        target, roi = result
        self.assertIs(target, EditTarget.A)
        self.assertEqual((roi.x, roi.y, roi.w, roi.h), (100, 80, 200, 180))
        self.assertTrue(self.state.dirty)
        self.assertFalse(self.state.selecting)

    def test_target_recorded_for_b(self) -> None:
        self.state.start(EditTarget.B)
        self.state.click(10, 10)
        target, _ = self.state.click(400, 400)
        self.assertIs(target, EditTarget.B)

    def test_all_corner_orders_give_same_rect(self) -> None:
        """第二个角在任意方向都要得到正矩形。"""
        cases = [
            ((10, 10), (110, 110)),
            ((110, 110), (10, 10)),
            ((110, 10), (10, 110)),
            ((10, 110), (110, 10)),
        ]
        for first, second in cases:
            with self.subTest(first=first, second=second):
                state = DualRoiState()
                state.start(EditTarget.A)
                state.click(*first)
                _, roi = state.click(*second)
                self.assertEqual((roi.x, roi.y, roi.w, roi.h),
                                 (10, 10, 100, 100))

    def test_tiny_roi_rejected(self) -> None:
        self.state.start(EditTarget.A)
        self.state.click(100, 100)
        self.assertIsNone(self.state.click(105, 103))
        self.assertFalse(self.state.dirty)
        self.assertIn("太小", self.state.message)

    def test_exactly_minimum_side_accepted(self) -> None:
        self.state.start(EditTarget.A)
        self.state.click(100, 100)
        result = self.state.click(100 + MIN_ROI_SIDE, 100 + MIN_ROI_SIDE)
        self.assertIsNotNone(result, f"{MIN_ROI_SIDE}px 应该被接受")

    def test_one_pixel_short_rejected(self) -> None:
        self.state.start(EditTarget.A)
        self.state.click(100, 100)
        self.assertIsNone(self.state.click(100 + MIN_ROI_SIDE - 1,
                                           100 + MIN_ROI_SIDE - 1))

    def test_cancel(self) -> None:
        self.state.start(EditTarget.B)
        self.state.click(100, 100)
        self.state.cancel()
        self.assertFalse(self.state.selecting)
        self.assertIsNone(self.state.pending_corner)

    def test_restart_clears_pending_corner(self) -> None:
        """框选到一半改选另一个矩形，旧的第一个角必须作废。"""
        self.state.start(EditTarget.A)
        self.state.click(100, 100)
        self.state.start(EditTarget.B)
        self.assertIsNone(self.state.pending_corner)
        self.assertIsNone(self.state.click(200, 200))
        self.assertEqual(self.state.pending_corner, (200, 200))


# ──────────────────────────────────────────────────────────────────────
# 核心不变量：B 严格大于 A
# ──────────────────────────────────────────────────────────────────────


class TestDualRoiSeparation(unittest.TestCase):
    """本功能存在的意义，必须锁死。"""

    def test_b_is_larger_than_a_by_default(self) -> None:
        editor = make_editor()
        a, b = editor.mapper.roi, editor.roi_b
        self.assertGreater(
            b.w, a.w,
            f"检测区域 B({b.w}) 必须比映射区域 A({a.w}) 宽 —— "
            f"否则色块出界时中心会算成可见部分的中心",
        )
        self.assertGreater(b.h, a.h, "B 必须比 A 高")

    def test_b_contains_a(self) -> None:
        editor = make_editor()
        a, b = editor.mapper.roi, editor.roi_b
        self.assertLessEqual(b.x, a.x, "B 左边界不能比 A 更靠右")
        self.assertLessEqual(b.y, a.y, "B 上边界不能比 A 更靠下")
        self.assertGreaterEqual(b.x + b.w, a.x + a.w, "B 右边界要包住 A")
        self.assertGreaterEqual(b.y + b.h, a.y + a.h, "B 下边界要包住 A")

    def test_margin_respected_when_away_from_border(self) -> None:
        """A 不贴画面边缘时，B 应正好是 A 每边外扩 margin。"""
        config = make_config(roi=(200, 100, 200, 200), margin=40)
        editor = make_editor(config)
        a, b = editor.mapper.roi, editor.roi_b
        self.assertEqual((b.x, b.y), (a.x - 40, a.y - 40))
        self.assertEqual((b.w, b.h), (a.w + 80, a.h + 80))

    def test_margin_clamped_to_frame(self) -> None:
        """A 贴画面边缘时 B 被裁到画面内，绝不能越界。"""
        for roi in [(0, 0, 200, 200), (440, 280, 200, 200),
                    (0, 0, 640, 480), (600, 440, 40, 40)]:
            with self.subTest(roi=roi):
                config = make_config(roi=roi, margin=60)
                editor = make_editor(config)
                b = editor.roi_b
                self.assertGreaterEqual(b.x, 0)
                self.assertGreaterEqual(b.y, 0)
                self.assertLessEqual(b.x + b.w, FRAME_W)
                self.assertLessEqual(b.y + b.h, FRAME_H)
                self.assertGreater(b.w, 0)
                self.assertGreater(b.h, 0)

    def test_zero_margin_degenerates_to_a(self) -> None:
        """margin=0 时 B 与 A 同尺寸（退化为旧行为，用于对照）。"""
        config = make_config(roi=(200, 100, 200, 200), margin=0)
        editor = make_editor(config)
        a, b = editor.mapper.roi, editor.roi_b
        self.assertEqual((b.x, b.y, b.w, b.h), (a.x, a.y, a.w, a.h))

    def test_explicit_b_in_config_wins(self) -> None:
        """配置里显式写了 B 就不用自动推导。"""
        config = make_config(roi=(200, 100, 200, 200), margin=40)
        config.detector.set_roi((50, 40, 500, 400))
        editor = make_editor(config)
        self.assertEqual(
            (editor.roi_b.x, editor.roi_b.y, editor.roi_b.w, editor.roi_b.h),
            (50, 40, 500, 400),
        )


class TestCenterAccuracyAtEdge(unittest.TestCase):
    """端到端语义：色块中心顶到 A 的边界时，B 仍能装下整个色块。

    只要 B 装得下完整色块，模型看到的就不是"残缺的一半"，
    算出来的中心就是真实中心 —— 这正是"挡板能贴到最边上"的前提。
    """

    BLOCK_W, BLOCK_H = 90, 170

    def test_edge_block_fully_inside_b(self) -> None:
        config = make_config(roi=(200, 100, 200, 200), margin=50)
        editor = make_editor(config)
        a, b = editor.mapper.roi, editor.roi_b

        for center_x, name in ((a.x, "左边界"), (a.x + a.w, "右边界")):
            with self.subTest(boundary=name):
                left = center_x - self.BLOCK_W // 2
                right = center_x + self.BLOCK_W // 2
                self.assertGreaterEqual(
                    left, b.x,
                    f"{name}处色块左缘 {left} 超出 B 左界 {b.x}",
                )
                self.assertLessEqual(
                    right, b.x + b.w,
                    f"{name}处色块右缘 {right} 超出 B 右界 {b.x + b.w}",
                )

    def test_old_behavior_would_clip(self) -> None:
        """反证：B == A（旧行为）时贴边色块确实被裁掉，中心必然偏移。"""
        a = Roi(200, 100, 200, 200)
        center_x = a.x                              # 中心正好在 A 左边界
        left = center_x - self.BLOCK_W // 2
        self.assertLess(
            left, a.x,
            "色块左半边确实落在 A 之外 —— 这正是旧实现算错中心的原因",
        )
        # 被裁后可见部分的中心，比真实中心偏右
        visible_center = (a.x + (center_x + self.BLOCK_W // 2)) / 2
        self.assertGreater(visible_center, center_x)
        self.assertGreater(visible_center - center_x, 10,
                           "偏移量应该大到肉眼可见（挡板贴不到边）")


# ──────────────────────────────────────────────────────────────────────
# 按钮动作
# ──────────────────────────────────────────────────────────────────────


class TestButtonActions(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = make_editor()

    def test_all_expected_buttons_present(self) -> None:
        actions = {b.action for b in self.editor.buttons.buttons if b.action}
        expected = {"pick_a", "pick_b", "b_auto", "b_grow", "b_shrink",
                    "toggle_full", "save", "quit"}
        self.assertEqual(actions, expected)

    def test_grow_increases_size_by_two_steps(self) -> None:
        self.editor = make_resize_editor()
        before = (self.editor.roi_b.w, self.editor.roi_b.h)
        self.editor.handle_action("b_grow")
        self.assertEqual(
            (self.editor.roi_b.w, self.editor.roi_b.h),
            (before[0] + 2 * B_STEP, before[1] + 2 * B_STEP),
        )

    def test_shrink_decreases_size_by_two_steps(self) -> None:
        self.editor = make_resize_editor()
        before = (self.editor.roi_b.w, self.editor.roi_b.h)
        self.editor.handle_action("b_shrink")
        self.assertEqual(
            (self.editor.roi_b.w, self.editor.roi_b.h),
            (before[0] - 2 * B_STEP, before[1] - 2 * B_STEP),
        )

    def test_grow_then_shrink_returns_to_original(self) -> None:
        self.editor = make_resize_editor()
        original = self.editor.roi_b.as_tuple()
        self.editor.handle_action("b_grow")
        self.editor.handle_action("b_shrink")
        self.assertEqual(self.editor.roi_b.as_tuple(), original,
                         "扩缩应可逆，不能越调越偏")

    def test_grow_keeps_center(self) -> None:
        """扩缩以中心为基准 —— 否则每按一次都会往一个方向漂。"""
        self.editor = make_resize_editor()
        cx, cy = center_of(self.editor.roi_b)
        self.editor.handle_action("b_grow")
        ax, ay = center_of(self.editor.roi_b)
        self.assertAlmostEqual(ax, cx, delta=1)
        self.assertAlmostEqual(ay, cy, delta=1)

    def test_shrink_keeps_center(self) -> None:
        self.editor = make_resize_editor()
        cx, cy = center_of(self.editor.roi_b)
        self.editor.handle_action("b_shrink")
        ax, ay = center_of(self.editor.roi_b)
        self.assertAlmostEqual(ax, cx, delta=1)
        self.assertAlmostEqual(ay, cy, delta=1)

    def test_shrink_has_minimum(self) -> None:
        """反复缩小不能变成 0 或负数尺寸。"""
        self.editor = make_resize_editor()
        for _ in range(60):
            self.editor.handle_action("b_shrink")
        self.assertGreaterEqual(self.editor.roi_b.w, MIN_ROI_SIDE)
        self.assertGreaterEqual(self.editor.roi_b.h, MIN_ROI_SIDE)

    def test_shrink_never_enlarges_b(self) -> None:
        """缩小到下限后再按缩小，**绝不能反而变大**。

        早期实现的写法是 `max(2*MIN_ROI_SIDE, w + 2*step)`：
        当 B 已经比那个下限还小时，按"缩小"会把 B 撑大，
        而状态栏还在显示"B -10px" —— 用户完全看不懂发生了什么。
        """
        self.editor = make_resize_editor()
        for _ in range(60):
            self.editor.handle_action("b_shrink")
        smallest = (self.editor.roi_b.w, self.editor.roi_b.h)

        for _ in range(5):
            self.editor.handle_action("b_shrink")
            now = (self.editor.roi_b.w, self.editor.roi_b.h)
            self.assertLessEqual(
                now[0], smallest[0],
                f"按了缩小，宽度却从 {smallest} 变成 {now}",
            )
            self.assertLessEqual(now[1], smallest[1])
            smallest = now

    def test_shrink_at_minimum_reports_clearly(self) -> None:
        self.editor = make_resize_editor()
        for _ in range(60):
            self.editor.handle_action("b_shrink")
        self.editor.handle_action("b_shrink")
        self.assertIn("最小", self.editor.state.message)

    def test_grow_clamped_to_frame(self) -> None:
        """一直放大不能越出画面。"""
        for _ in range(60):
            self.editor.handle_action("b_grow")
        b = self.editor.roi_b
        self.assertLessEqual(b.x + b.w, FRAME_W)
        self.assertLessEqual(b.y + b.h, FRAME_H)

    def test_toggle_full_sets_full_frame(self) -> None:
        self.editor.handle_action("toggle_full")
        self.assertEqual(self.editor.roi_b.as_tuple(), (0, 0, FRAME_W, FRAME_H))

    def test_toggle_full_twice_restores_auto(self) -> None:
        original = self.editor.roi_b.as_tuple()
        self.editor.handle_action("toggle_full")
        self.editor.handle_action("toggle_full")
        self.assertEqual(self.editor.roi_b.as_tuple(), original)

    def test_b_auto_resets_to_a_plus_margin(self) -> None:
        self.editor.set_roi_b(Roi(10, 10, 100, 100))
        self.editor.handle_action("b_auto")
        a, b = self.editor.mapper.roi, self.editor.roi_b
        margin = self.editor.config.detector.roi_margin
        self.assertEqual(b.x, max(0, a.x - margin))
        self.assertEqual(b.y, max(0, a.y - margin))
        self.assertEqual(b.x + b.w, min(FRAME_W, a.x + a.w + margin))
        self.assertEqual(b.y + b.h, min(FRAME_H, a.y + a.h + margin))

    def test_b_auto_after_a_moved(self) -> None:
        """A 改了以后按 B=A+边距，B 要跟着走。"""
        self.editor.set_roi_a(Roi(120, 60, 240, 240))
        self.editor.handle_action("b_auto")
        a, b = self.editor.mapper.roi, self.editor.roi_b
        self.assertEqual(center_of(b), center_of(a),
                         "B 应跟着 A 居中")

    def test_quit_returns_signal(self) -> None:
        self.assertEqual(self.editor.handle_action("quit"), "quit")

    def test_other_actions_return_none(self) -> None:
        for action in ("pick_a", "b_grow", "b_shrink", "b_auto",
                       "toggle_full"):
            with self.subTest(action=action):
                self.assertIsNone(self.editor.handle_action(action))

    def test_pick_buttons_highlight_active(self) -> None:
        self.editor.handle_action("pick_a")
        self.assertTrue(self.editor.buttons.get("pick_a").active)
        self.editor.handle_action("pick_b")
        self.assertTrue(self.editor.buttons.get("pick_b").active)
        self.assertFalse(self.editor.buttons.get("pick_a").active,
                         "同时只能有一个在框选")

    def test_dirty_flag_set_by_edits(self) -> None:
        self.assertFalse(self.editor.state.dirty)
        self.editor.handle_action("b_grow")
        self.assertTrue(self.editor.state.dirty,
                        "改过参数就该标记未保存，提醒用户按 Save")


class TestSaveToConfig(unittest.TestCase):
    def test_save_action_writes_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            config = make_config()
            editor = make_editor(config)
            editor.config_path = str(path)

            editor.set_roi_a(Roi(100, 50, 300, 300))
            editor.set_roi_b(Roi(80, 30, 340, 340))
            editor.handle_action("save")

            self.assertTrue(path.exists(), "Save 应该真的写文件")
            self.assertFalse(editor.state.dirty, "保存后不该还标着未保存")

            reloaded = AppConfig.load(path)
            self.assertEqual(
                (reloaded.mapping.roi_x, reloaded.mapping.roi_y,
                 reloaded.mapping.roi_w, reloaded.mapping.roi_h),
                (100, 50, 300, 300),
            )
            self.assertEqual(
                (reloaded.detector.roi_x, reloaded.detector.roi_y,
                 reloaded.detector.roi_w, reloaded.detector.roi_h),
                (80, 30, 340, 340),
            )

    def test_save_failure_is_reported_not_raised(self) -> None:
        """磁盘写不进去时要在状态栏说明，不能把程序崩掉。"""
        config = make_config()
        editor = make_editor(config)
        editor.config_path = "/proc/definitely/not/writable/config.yaml"
        editor.handle_action("save")
        self.assertIn("保存失败", editor.state.message)

    def test_save_failure_keeps_dirty_flag(self) -> None:
        """保存失败后必须还标着"未保存"。

        否则状态栏会说"已保存"、``dirty`` 也变 False，用户以为存上了，
        下次启动发现参数全没变 —— 这种问题极难查。
        """
        config = make_config()
        editor = make_editor(config)
        editor.handle_action("b_grow")
        self.assertTrue(editor.state.dirty)

        editor.config_path = "/proc/definitely/not/writable/config.yaml"
        editor.handle_action("save")
        self.assertTrue(editor.state.dirty,
                        "保存失败却清掉了未保存标记")

    def test_successful_save_clears_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config()
            editor = make_editor(config)
            editor.config_path = str(Path(tmp) / "config.yaml")
            editor.handle_action("b_grow")
            self.assertTrue(editor.state.dirty)
            editor.handle_action("save")
            self.assertFalse(editor.state.dirty)

    def test_unsaved_hint_shown_in_status(self) -> None:
        """有改动没保存时状态栏要提醒 —— 防止调完直接退出、白调一场。"""
        config = make_config()
        editor = make_editor(config)
        image = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)

        editor.compose(image)
        self.assertEqual(editor.unsaved_hint, "")
        self.assertNotIn("未保存", editor.status.lines[-1])

        editor.handle_action("b_grow")
        editor.compose(image)
        self.assertIn("未保存", editor.unsaved_hint)
        self.assertIn("未保存", editor.status.lines[-1])

    def test_clipping_warning_beats_unsaved_hint(self) -> None:
        """告警优先级更高：它直接关系到追踪准不准。"""
        config = make_config()
        editor = make_editor(config)
        editor.handle_action("b_grow")
        b = editor.roi_b
        editor.note_detection((b.x, 180, b.x + 90, 280))
        editor.compose(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        self.assertIn(editor.warning, editor.status.lines[-1])


# ──────────────────────────────────────────────────────────────────────
# 鼠标 / 键盘
# ──────────────────────────────────────────────────────────────────────


class TestMouseDispatch(unittest.TestCase):
    """鼠标要能区分"点按钮"和"在画面里框选"。"""

    def setUp(self) -> None:
        self.editor = make_editor()
        self.editor.buttons.render(FRAME_W)

    def click_button(self, action: str, x_offset: int = 0) -> None:
        rect = self.editor.button_rect_on_screen(action)
        assert rect is not None
        x, y, w, h = rect
        self.editor.on_mouse(
            cv2.EVENT_LBUTTONDOWN,
            x + w // 2 + x_offset,
            y + h // 2,
            0, None,
        )

    def test_click_button_triggers_action(self) -> None:
        self.click_button("toggle_full")
        self.assertEqual(self.editor.roi_b.as_tuple(),
                         (0, 0, FRAME_W, FRAME_H))

    def test_every_button_is_clickable(self) -> None:
        """每个按钮都要能通过鼠标点到 —— 位置算错就会在这里暴露。"""
        for button in self.editor.buttons.buttons:
            if not button.action or button.action == "quit":
                continue
            with self.subTest(action=button.action):
                editor = make_editor()
                editor.buttons.render(FRAME_W)
                rect = editor.button_rect_on_screen(button.action)
                assert rect is not None
                x, y, w, h = rect
                editor.on_mouse(cv2.EVENT_LBUTTONDOWN, x + w // 2,
                                y + h // 2, 0, None)
                if button.action.startswith("pick"):
                    self.assertTrue(editor.state.selecting,
                                    f"{button.action} 没进入框选状态")
                else:
                    self.assertTrue(editor.state.message,
                                    f"{button.action} 点了没反应")

    def test_click_video_area_does_not_hit_buttons(self) -> None:
        before = self.editor.roi_b.as_tuple()
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 320, 240, 0, None)
        self.assertEqual(self.editor.roi_b.as_tuple(), before)

    def test_click_status_bar_ignored(self) -> None:
        before = self.editor.roi_b.as_tuple()
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 320,
                             FRAME_H + self.editor.BUTTON_HEIGHT + 10, 0, None)
        self.assertEqual(self.editor.roi_b.as_tuple(), before)

    def test_select_a_via_mouse(self) -> None:
        self.editor.handle_action("pick_a")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 300, 0, None)
        self.assertEqual(self.editor.mapper.roi.as_tuple(),
                         (200, 100, 200, 200))

    def test_select_b_via_mouse(self) -> None:
        """框出来的 B 如果盖不住 A，会被撑到两者的并集。"""
        self.editor.handle_action("pick_b")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 150, 50, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 500, 430, 0, None)
        # 框的是 (150,50,350,380)，但 A=(156,20,326,428) 更宽更高，
        # 并集 = (150,20,350,428)
        self.assertEqual(self.editor.roi_b.as_tuple(), (150, 20, 350, 428))

    def test_selecting_b_does_not_touch_a(self) -> None:
        before = self.editor.mapper.roi.as_tuple()
        self.editor.handle_action("pick_b")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 150, 50, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 500, 430, 0, None)
        self.assertEqual(self.editor.mapper.roi.as_tuple(), before,
                         "框选 B 时绝不能顺手改掉映射区域 A")

    def test_non_left_click_ignored(self) -> None:
        self.editor.handle_action("pick_b")
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 100, 100, 0, None)
        self.assertIsNone(self.editor.state.pending_corner)

    def test_click_after_selection_does_not_reuse_old_corner(self) -> None:
        self.editor.handle_action("pick_b")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 400, 0, None)
        settled = self.editor.roi_b.as_tuple()

        # 框选已结束，再点两下不该被当成新矩形的两个角
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 200, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 300, 300, 0, None)
        self.assertEqual(self.editor.roi_b.as_tuple(), settled,
                         "不在框选状态下的点击不该改动 B")


class TestKeyboard(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = make_editor()

    def test_key_a_and_b(self) -> None:
        self.editor.on_key(ord("a"))
        self.assertIs(self.editor.state.target, EditTarget.A)
        self.editor.on_key(ord("b"))
        self.assertIs(self.editor.state.target, EditTarget.B)

    def test_key_plus_and_equals_both_grow(self) -> None:
        for key in (ord("+"), ord("=")):
            with self.subTest(key=chr(key)):
                editor = make_resize_editor()
                before = editor.roi_b.w
                editor.on_key(key)
                self.assertEqual(editor.roi_b.w, before + 2 * B_STEP)

    def test_key_minus_shrinks(self) -> None:
        self.editor = make_resize_editor()
        before = self.editor.roi_b.w
        self.editor.on_key(ord("-"))
        self.assertEqual(self.editor.roi_b.w, before - 2 * B_STEP)

    def test_key_zero_resets_to_auto(self) -> None:
        self.editor.set_roi_b(Roi(10, 10, 100, 100))
        self.editor.on_key(ord("0"))
        self.assertNotEqual(self.editor.roi_b.w, 100)
        # 注意：A 贴画面边缘时 B 会被限幅，中心自然对不齐（几何上必然）。
        # 这里把 A 放到画面中间，才能验证"B 以 A 为中心"。
        self.editor.set_roi_a(Roi(160, 60, 320, 340))
        self.editor.on_key(ord("0"))
        a, b = self.editor.mapper.roi, self.editor.roi_b
        self.assertEqual(center_of(b), center_of(a))

    def test_key_f_full(self) -> None:
        self.editor.on_key(ord("f"))
        self.assertEqual(self.editor.roi_b.as_tuple(),
                         (0, 0, FRAME_W, FRAME_H))

    def test_key_esc_cancels_selection_not_quit(self) -> None:
        self.editor.on_key(ord("a"))
        signal = self.editor.on_key(27)
        self.assertEqual(signal, "cancel")
        self.assertFalse(self.editor.state.selecting)
        self.assertFalse(self.editor.buttons.get("pick_a").active)

    def test_key_esc_without_selection_quits(self) -> None:
        self.assertEqual(self.editor.on_key(27), "quit")

    def test_key_q_quits(self) -> None:
        self.assertEqual(self.editor.on_key(ord("q")), "quit")

    def test_no_key_returns_none(self) -> None:
        self.assertIsNone(self.editor.on_key(255))
        self.assertIsNone(self.editor.on_key(-1))

    def test_unknown_key_returns_none(self) -> None:
        self.assertIsNone(self.editor.on_key(ord("z")))

    def test_key_s_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.editor.config_path = str(Path(tmp) / "config.yaml")
            self.editor.on_key(ord("s"))
            self.assertIn("已保存", self.editor.state.message)


# ──────────────────────────────────────────────────────────────────────
# 与检测器 / 配置的联动
# ──────────────────────────────────────────────────────────────────────


class TestDetectorSync(unittest.TestCase):
    def test_b_pushed_to_detector_on_construction(self) -> None:
        """B 必须同步给检测器，否则界面改了检测还在用旧值。"""

        class FakeDetector:
            def __init__(self) -> None:
                self.roi = None

        config = make_config()
        mapper = CoordinateMapper(
            roi=config.mapping.build_roi(), smoothing=None, deadband=0
        )
        detector = FakeDetector()
        RoiEditor(config, mapper, detector=detector,
                  frame_size=(FRAME_W, FRAME_H), config_path="/dev/null")
        self.assertIsNotNone(detector.roi, "构造时就应该同步一次")

    def test_b_pushed_on_every_change(self) -> None:
        class FakeDetector:
            def __init__(self) -> None:
                self.roi = None

        config = make_config()
        mapper = CoordinateMapper(
            roi=config.mapping.build_roi(), smoothing=None, deadband=0
        )
        detector = FakeDetector()
        editor = RoiEditor(config, mapper, detector=detector,
                           frame_size=(FRAME_W, FRAME_H),
                           config_path="/dev/null")

        editor.set_roi_b(Roi(50, 50, 400, 400))
        self.assertEqual(detector.roi, editor.roi_b.as_tuple(),
                         "检测器用的 ROI 必须和编辑器里的 B 完全一致")

        editor.handle_action("toggle_full")
        self.assertEqual(detector.roi, (0, 0, FRAME_W, FRAME_H))

    def test_detector_without_roi_attribute_is_tolerated(self) -> None:
        """HSV 检测器没有 roi 属性也不该崩。"""

        class Bare:
            pass

        editor = make_editor(detector=Bare())
        editor.set_roi_b(Roi(50, 50, 400, 400))
        a = editor.mapper.roi
        b = editor.roi_b
        self.assertLessEqual(b.x, a.x)
        self.assertLessEqual(b.y, a.y)
        self.assertGreaterEqual(b.x + b.w, a.x + a.w)
        self.assertGreaterEqual(b.y + b.h, a.y + a.h)


class TestConfigSync(unittest.TestCase):
    def test_apply_to_config_writes_both(self) -> None:
        config = make_config()
        editor = make_editor(config)
        editor.set_roi_a(Roi(100, 50, 300, 300))
        editor.set_roi_b(Roi(80, 30, 340, 340))
        editor.apply_to_config()

        self.assertEqual(
            (config.mapping.roi_x, config.mapping.roi_y,
             config.mapping.roi_w, config.mapping.roi_h),
            (100, 50, 300, 300),
        )
        self.assertEqual(
            (config.detector.roi_x, config.detector.roi_y,
             config.detector.roi_w, config.detector.roi_h),
            (80, 30, 340, 340),
        )
        self.assertFalse(editor.state.dirty)

    def test_round_trip_through_config(self) -> None:
        """保存后再构造一个编辑器，应得到相同的 A 与 B。"""
        config = make_config()
        editor = make_editor(config)
        editor.set_roi_a(Roi(140, 30, 360, 420))
        # 这个 B 右边界(470) 盖不住 A 的右边界(500)，会被自动撑到并集
        editor.set_roi_b(Roi(90, 40, 380, 400))
        self.assertEqual(editor.roi_b.as_tuple(), (90, 30, 410, 420),
                         "B 应被撑到完整包住 A")
        editor.apply_to_config()

        editor2 = make_editor(config)
        self.assertEqual(editor2.mapper.roi.as_tuple(), (140, 30, 360, 420))
        self.assertEqual(editor2.roi_b.as_tuple(), (90, 30, 410, 420))


# ──────────────────────────────────────────────────────────────────────
# 渲染
# ──────────────────────────────────────────────────────────────────────


class TestRendering(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = make_editor()
        self.image = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)

    def test_draw_rois_does_not_mutate_input(self) -> None:
        original = self.image.copy()
        out = self.editor.draw_rois(self.image)
        self.assertEqual(out.shape, self.image.shape)
        np.testing.assert_array_equal(self.image, original)

    def test_draw_rois_changes_image(self) -> None:
        out = self.editor.draw_rois(self.image)
        self.assertFalse(np.array_equal(out, self.image))

    def test_overlay_hint_mutates_in_place_by_design(self) -> None:
        """overlay_hint 是**原地修改**的（每帧都要跑，省一次整幅拷贝）。

        这里如实断言这个行为，而不是假装它不改入参 —— 行为变了就得改测试，
        不能靠一个恒真的断言蒙混过去。
        """
        original = self.image.copy()
        out = self.editor.overlay_hint(self.image)
        self.assertIs(out, self.image, "返回值应该就是入参本身（方便链式调用）")
        self.assertFalse(np.array_equal(self.image, original),
                         "原地画了提示，画面内容应该变了")
        self.assertEqual(out.shape, original.shape)

    def test_compose_overlay_is_window_sized(self) -> None:
        """默认 overlay 布局：窗口尺寸 = 摄像头画面尺寸。

        这样在任何屏幕上都不会出现"按钮条被挤到屏幕外"的情况 ——
        那正是用户报的"我不知道 Pick A 在哪"。
        """
        out = self.editor.compose(self.image)
        self.assertEqual(out.shape, (FRAME_H, FRAME_W, 3))

    def test_compose_stacked_is_taller(self) -> None:
        self.editor.config.debug.ui_layout = "stacked"
        out = self.editor.compose(self.image)
        expected_h = (FRAME_H + self.editor.BUTTON_HEIGHT
                      + self.editor.STATUS_HEIGHT)
        self.assertEqual(out.shape, (expected_h, FRAME_W, 3))

    def test_compose_accepts_drawn_frame(self) -> None:
        frame = self.editor.overlay_hint(self.editor.draw_rois(self.image))
        self.assertEqual(self.editor.compose(frame).shape[1], FRAME_W)

    def test_compose_with_pending_corner(self) -> None:
        self.editor.handle_action("pick_b")
        self.editor.state.click(200, 150)
        out = self.editor.compose(self.editor.draw_rois(self.image))
        self.assertEqual(out.shape, (FRAME_H, FRAME_W, 3))

    def test_draw_rois_with_full_frame_b(self) -> None:
        """B 等于整幅画面时画虚线不能越界。"""
        self.editor.handle_action("toggle_full")
        out = self.editor.draw_rois(self.image)
        self.assertEqual(out.shape, self.image.shape)

    def test_draw_rois_with_tiny_roi(self) -> None:
        self.editor.set_roi_b(Roi(300, 200, MIN_ROI_SIDE, MIN_ROI_SIDE))
        out = self.editor.draw_rois(self.image)
        self.assertEqual(out.shape, self.image.shape)

    def test_status_bar_shows_both_rois(self) -> None:
        self.editor.config.debug.ui_layout = "stacked"
        self.editor.compose(self.image)
        text = " ".join(self.editor.status.lines)
        self.assertIn("映射区", text)
        self.assertIn("检测区", text)
        a, b = self.editor.mapper.roi, self.editor.roi_b
        self.assertIn(f"{a.x},{a.y},{a.w},{a.h}", text.replace(" ", ""))
        self.assertIn(f"{b.x},{b.y},{b.w},{b.h}", text.replace(" ", ""))

    def test_stacked_status_has_two_rows(self) -> None:
        """stacked 布局：数值与提示分两行，避免中文告警被裁掉。"""
        self.editor.config.debug.ui_layout = "stacked"
        self.editor.state.message = "一些提示"
        self.editor.compose(self.image)
        self.assertEqual(len(self.editor.status.lines), 2)
        self.assertEqual(self.editor.status.lines[1], "一些提示")

    def test_overlay_status_shows_message(self) -> None:
        """overlay 布局：提示也必须有地方显示（状态行压在画面底部）。"""
        self.editor.state.message = "一些提示"
        self.editor.compose(self.image)
        self.assertIn("一些提示", self.editor.status.lines[0])


class TestClippingWarning(unittest.TestCase):
    """检测框贴到 B 边界时必须在界面上报警。

    这是判断"B 到底够不够大"的现场依据：框贴边 = 色块很可能被裁过 =
    算出来的中心偏向可见部分的中心 = 挡板到不了最边上。
    """

    def setUp(self) -> None:
        # A 放画面中间，B 不贴画面边缘，方便控制检测框位置
        self.editor = make_editor(make_config(roi=(200, 100, 200, 200),
                                              margin=40))

    def test_no_detection_no_warning(self) -> None:
        self.editor.note_detection(None)
        self.assertFalse(self.editor.clipped)
        self.assertEqual(self.editor.warning, "")

    def test_box_well_inside_no_warning(self) -> None:
        self.editor.note_detection((260, 160, 340, 240))
        self.assertFalse(self.editor.clipped)
        self.assertEqual(self.editor.warning, "")

    def test_box_touching_left_edge_warns(self) -> None:
        b = self.editor.roi_b
        self.editor.note_detection((b.x, 180, b.x + 90, 280))
        self.assertTrue(self.editor.clipped, "框贴到 B 左边界应报警")
        self.assertTrue(self.editor.warning)

    def test_box_touching_right_edge_warns(self) -> None:
        b = self.editor.roi_b
        self.editor.note_detection((b.x + b.w - 90, 180, b.x + b.w, 280))
        self.assertTrue(self.editor.clipped)

    def test_box_touching_top_edge_warns(self) -> None:
        b = self.editor.roi_b
        self.editor.note_detection((280, b.y, 370, b.y + 90))
        self.assertTrue(self.editor.clipped)

    def test_box_touching_bottom_edge_warns(self) -> None:
        b = self.editor.roi_b
        self.editor.note_detection((280, b.y + b.h - 90, 370, b.y + b.h))
        self.assertTrue(self.editor.clipped)

    def test_enlarging_b_clears_warning(self) -> None:
        """按 B+ 把 B 放大后，同一个检测框就不该再报警。"""
        b = self.editor.roi_b
        bbox = (b.x, 180, b.x + 90, 280)
        self.editor.note_detection(bbox)
        self.assertTrue(self.editor.clipped)

        for _ in range(6):
            self.editor.handle_action("b_grow")
            self.editor.note_detection(bbox)
            if not self.editor.clipped:
                break
        self.assertFalse(self.editor.clipped,
                         "放大 B 之后不该还报警")

    def test_warning_shown_in_status_bar(self) -> None:
        b = self.editor.roi_b
        self.editor.note_detection((b.x, 180, b.x + 90, 280))
        self.editor.compose(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        self.assertIn("B", self.editor.status.lines[-1])
        self.assertTrue(self.editor.status.lines[-1])

    def test_warning_takes_priority_over_message(self) -> None:
        """告警比普通提示重要 —— 它直接关系到追踪准不准。"""
        self.editor.state.message = "普通提示"
        b = self.editor.roi_b
        self.editor.note_detection((b.x, 180, b.x + 90, 280))
        self.editor.compose(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        self.assertNotIn("普通提示", self.editor.status.lines[-1])

    def test_clipped_changes_render(self) -> None:
        """贴边时 B 的配色要变（绿色→红色），让人一眼看到。"""
        image = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)
        normal = self.editor.draw_rois(image)
        b = self.editor.roi_b
        self.editor.note_detection((b.x, 180, b.x + 90, 280))
        clipped = self.editor.draw_rois(image)
        self.assertFalse(np.array_equal(normal, clipped))

    def test_tolerance_boundary(self) -> None:
        """刚好落在容差内算贴边，超出容差不算。"""
        b = self.editor.roi_b
        just_inside = (b.x + CLIP_TOLERANCE, 180,
                       b.x + CLIP_TOLERANCE + 90, 280)
        clearly_inside = (b.x + CLIP_TOLERANCE + 5, 180,
                          b.x + CLIP_TOLERANCE + 95, 280)
        self.editor.note_detection(just_inside)
        self.assertTrue(self.editor.clipped)
        self.editor.note_detection(clearly_inside)
        self.assertFalse(self.editor.clipped)


class TestDefaultMargin(unittest.TestCase):
    """默认边距必须够大 —— 否则功能等于没做。"""

    #: 红卡在 640x480 画面里的典型宽度（实测约 90px）
    CARD_WIDTH = 90

    def test_default_margin_exceeds_half_card_width(self) -> None:
        config = AppConfig()
        self.assertGreater(
            config.detector.roi_margin, self.CARD_WIDTH / 2,
            f"默认 roi_margin={config.detector.roi_margin} 不足以容纳"
            f"贴边色块（需要 > {self.CARD_WIDTH / 2}）—— "
            f"色块中心顶到 A 边界时仍会被裁掉一角，中心算偏",
        )

    def test_config_yaml_margin_explicit_and_sufficient(self) -> None:
        """仓库里的 config.yaml 也要满足这条规则（改小了会被测出来）。"""
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        if not path.exists():
            self.skipTest("config.yaml 不存在")
        config = AppConfig.load(path)
        self.assertGreater(config.detector.roi_margin, self.CARD_WIDTH / 2)


class TestLabelsAreRenderable(unittest.TestCase):
    """标签必须**画得出来**。

    中文靠 Pillow 渲染（板子上有 Noto CJK）。但万一没字体，纯中文标签
    会被丢成空白 —— 按钮看起来是空的，比乱码更糟。所以每个标签都要么
    能在当前环境画出来，要么留有 ASCII 备胎。
    """

    def test_every_label_is_renderable_or_has_fallback(self) -> None:
        from src.text_cjk import has_cjk_font

        editor = make_editor()
        for button in editor.buttons.buttons:
            if not button.label:
                continue
            with self.subTest(label=button.label):
                if button.label.isascii() or has_cjk_font():
                    continue
                self.assertTrue(
                    button.label_en,
                    f"标签 {button.label!r} 在无中文字体时会变空白，"
                    f"必须给一个 label_en 备胎",
                )

    def test_pure_chinese_labels_keep_some_ascii(self) -> None:
        """纯中文标签要么给了 label_en，要么去掉中文后还剩点东西。"""
        from src.text_cjk import strip_non_ascii

        editor = make_editor()
        for button in editor.buttons.buttons:
            if not button.label or button.label.isascii():
                continue
            if button.label_en:
                continue
            with self.subTest(label=button.label):
                self.assertTrue(
                    strip_non_ascii(button.label).strip(),
                    f"标签 {button.label!r} 去掉中文后什么都不剩",
                )

    def test_hint_text_is_ascii(self) -> None:
        """底部键盘提示保持纯 ASCII —— 没字体时它还得能看懂。"""
        self.assertTrue(HINT_TEXT.isascii())
        editor = make_editor()
        out = editor.overlay_hint(np.zeros((FRAME_H, FRAME_W, 3), np.uint8))
        self.assertEqual(out.shape, (FRAME_H, FRAME_W, 3))


class TestButtonBarFitsFrame(unittest.TestCase):
    """按钮条必须整体落在画面宽度内 —— 否则最右边的 Save/Quit 点不到。"""

    def test_all_buttons_visible_at_640(self) -> None:
        editor = make_editor()
        editor.buttons.render(FRAME_W)
        for button in editor.buttons.buttons:
            if not button.action:
                continue
            x, _, w, _ = button.rect
            with self.subTest(action=button.action):
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(
                    x + w, FRAME_W,
                    f"{button.label} 超出画面右边界，用户点不到",
                )

    def test_buttons_do_not_overlap(self) -> None:
        editor = make_editor()
        editor.buttons.render(FRAME_W)
        rects = [b.rect for b in editor.buttons.buttons if b.action]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                ax, _, aw, _ = rects[i]
                bx, _, bw, _ = rects[j]
                self.assertFalse(ax < bx + bw and bx < ax + aw,
                                 f"按钮重叠：{rects[i]} vs {rects[j]}")

    def test_narrow_frame_still_fits(self) -> None:
        editor = make_editor()
        editor.buttons.render(320)
        for button in editor.buttons.buttons:
            if not button.action:
                continue
            x, _, w, _ = button.rect
            self.assertLessEqual(x + w, 320)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ──────────────────────────────────────────────────────────────────────
# 键盘为主的框选流程（用户要的就是这个）
# ──────────────────────────────────────────────────────────────────────


class TestTwoClickPicking(unittest.TestCase):
    """按一下键 → 在画面里点两下（左上角、右下角）→ 矩形锁定。

    这是现场唯一必须好用的交互：按钮可能因为屏幕小而看不见，
    键盘 + 画面点击不会。
    """

    def setUp(self) -> None:
        self.editor = make_editor()

    def test_r_key_starts_picking_a(self) -> None:
        self.editor.on_key(ord("r"))
        self.assertIs(self.editor.state.target, EditTarget.A)
        self.assertTrue(self.editor.state.selecting)

    def test_t_key_starts_picking_b(self) -> None:
        self.editor.on_key(ord("t"))
        self.assertIs(self.editor.state.target, EditTarget.B)

    def test_a_and_b_are_aliases(self) -> None:
        self.editor.on_key(ord("a"))
        self.assertIs(self.editor.state.target, EditTarget.A)
        self.editor.on_key(ord("b"))
        self.assertIs(self.editor.state.target, EditTarget.B)

    def test_two_clicks_lock_a(self) -> None:
        """左上角 + 右下角两下点击，A 立刻生效。"""
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 120, 40, 0, None)
        self.assertTrue(self.editor.state.selecting, "只点一下不该结束框选")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 520, 440, 0, None)

        self.assertEqual(self.editor.mapper.roi.as_tuple(), (120, 40, 400, 400))
        self.assertFalse(self.editor.state.selecting, "框完应自动退出框选")

    def test_two_clicks_lock_b(self) -> None:
        self.editor.on_key(ord("t"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 60, 20, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 580, 460, 0, None)
        b = self.editor.roi_b
        self.assertLessEqual(b.x, 60)
        self.assertGreaterEqual(b.x + b.w, 580)

    def test_second_corner_may_be_up_left(self) -> None:
        """反着点（右下角先点）也要得到正矩形。"""
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 500, 400, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 80, 0, None)
        self.assertEqual(self.editor.mapper.roi.as_tuple(), (100, 80, 400, 320))

    def test_locked_rect_is_written_to_detector_for_b(self) -> None:
        class FakeDetector:
            def __init__(self) -> None:
                self.roi = None

        detector = FakeDetector()
        editor = make_editor(detector=detector)
        editor.on_key(ord("t"))
        editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 40, 10, 0, None)
        editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 600, 470, 0, None)
        self.assertEqual(tuple(detector.roi), editor.roi_b.as_tuple())

    def test_escape_cancels_without_touching_roi(self) -> None:
        before = self.editor.mapper.roi.as_tuple()
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 200, 0, None)
        self.assertEqual(self.editor.on_key(27), "cancel")
        self.assertEqual(self.editor.mapper.roi.as_tuple(), before,
                         "取消框选不该改动 A")
        self.assertFalse(self.editor.state.selecting)

    def test_escape_then_r_starts_fresh(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 200, 0, None)
        self.editor.on_key(27)
        self.editor.on_key(ord("r"))
        self.assertIsNone(self.editor.state.pending_corner,
                          "重新框选时旧的第一个角必须作废")

    def test_single_click_leaves_roi_untouched(self) -> None:
        before = self.editor.mapper.roi.as_tuple()
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 300, 300, 0, None)
        self.assertEqual(self.editor.mapper.roi.as_tuple(), before)

    def test_tiny_drag_is_rejected(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 300, 300, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 305, 303, 0, None)
        self.assertIn("太小", self.editor.state.message)

    def test_picking_message_tells_the_next_step(self) -> None:
        self.editor.on_key(ord("r"))
        self.assertIn("第一个角", self.editor.state.message)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 120, 40, 0, None)
        self.assertIn("第二个角", self.editor.state.message)

    def test_after_locking_a_hints_to_press_zero(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 300, 0, None)
        self.assertIn("0", self.editor.state.message,
                      "锁定 A 之后应提示按 0 让 B=A+边距")


class TestRubberBandPreview(unittest.TestCase):
    """框选到一半时跟着鼠标画预览矩形，落点看得见。"""

    def setUp(self) -> None:
        self.editor = make_editor()
        self.image = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)

    def test_hover_tracked_only_after_first_corner(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 300, 200, 0, None)
        self.assertIsNone(self.editor.state.hover,
                          "还没点第一个角，画什么橡皮筋")
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 300, 200, 0, None)
        self.assertEqual(self.editor.state.hover, (300, 200))

    def test_hover_cleared_after_locking(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 300, 200, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 300, 200, 0, None)
        self.assertIsNone(self.editor.state.hover)

    def test_hover_does_not_change_the_locked_rect(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 250, 250, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 350, 0, None)
        self.assertEqual(self.editor.mapper.roi.as_tuple(), (100, 100, 300, 250))

    def test_render_with_hover_does_not_crash(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 100, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_MOUSEMOVE, 500, 400, 0, None)
        out = self.editor.compose(self.editor.draw_rois(self.image))
        self.assertEqual(out.shape, (FRAME_H, FRAME_W, 3))

    def test_banner_drawn_only_while_selecting(self) -> None:
        plain = self.editor.draw_rois(self.image)
        self.editor.on_key(ord("r"))
        picking = self.editor.draw_rois(self.image)
        self.assertFalse(np.array_equal(plain, picking),
                         "框选时画面应有明显提示")
        self.editor.on_key(27)
        after = self.editor.draw_rois(self.image)
        np.testing.assert_array_equal(plain, after,
                                      "取消后提示应该消失")


class TestLayouts(unittest.TestCase):
    """两种布局下，按钮的"画在哪"和"点哪里"都必须一致。"""

    def test_button_rects_consistent_in_both_layouts(self) -> None:
        for layout in ("overlay", "stacked"):
            with self.subTest(layout=layout):
                config = make_config()
                config.debug.ui_layout = layout
                editor = make_editor(config)
                editor.buttons.render(FRAME_W)

                for button in editor.buttons.buttons:
                    if not button.action:
                        continue
                    rect = editor.button_rect_on_screen(button.action)
                    assert rect is not None
                    x, y, w, h = rect
                    # 点按钮中心必须命中该按钮
                    editor.state.cancel()
                    editor.on_mouse(cv2.EVENT_LBUTTONDOWN,
                                    x + w // 2, y + h // 2, 0, None)
                    if button.action.startswith("pick"):
                        self.assertTrue(
                            editor.state.selecting or editor.state.target,
                            f"{layout}: {button.action} 点不动",
                        )
                        editor.state.cancel()

    def test_overlay_buttons_inside_frame(self) -> None:
        editor = make_editor()
        editor.buttons.render(FRAME_W)
        for button in editor.buttons.buttons:
            if not button.action:
                continue
            x, y, w, h = editor.button_rect_on_screen(button.action)
            with self.subTest(action=button.action):
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y + h, FRAME_H,
                                     "按钮跑出画面了（屏幕小的时候就会点不到）")
                self.assertLessEqual(x + w, FRAME_W)

    def test_overlay_bar_really_drawn(self) -> None:
        """画面底部那条压条必须真的画上了东西。"""
        editor = make_editor()
        video = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)
        out = editor.compose(video.copy())
        strip = out[editor.button_strip_top:, :]
        self.assertGreater(int(np.abs(strip.astype(int) - 40).sum()), 0)

    def test_invalid_layout_falls_back_to_overlay(self) -> None:
        config = make_config()
        config.debug.ui_layout = "nonsense"
        editor = make_editor(config)
        self.assertEqual(editor.layout_mode, "overlay")


def make_guide_editor() -> RoiEditor:
    """显式打开教学窗的编辑器（默认是关的）。"""
    config = make_config()
    config.debug.show_guide = True
    return make_editor(config)


class TestStartupGuide(unittest.TestCase):
    """画面中央的教学窗：**默认不显示**，显式打开才出现。

    用户明确要求关掉它：底部那行键盘提示一直在，教学窗反而挡住卡片。
    代码保留是为了"卡住的时候能再看一眼"，所以两个方向都要有测试。
    """

    def setUp(self) -> None:
        self.editor = make_guide_editor()
        self.image = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)

    def test_off_by_default_draws_nothing(self) -> None:
        """默认配置下，教学窗**一个像素都不许画**。"""
        editor = make_editor()
        self.assertFalse(
            editor.guide_visible,
            "默认就该关掉教学窗 —— 用户明确要求不要它",
        )
        canvas = self.image.copy()
        before = canvas.copy()
        editor._draw_startup_guide(canvas)
        np.testing.assert_array_equal(
            canvas, before,
            "开关是关的，_draw_startup_guide 却改了画面",
        )

    def test_enabled_actually_draws_something(self) -> None:
        """开关打开时必须真的画出东西（否则这个功能就是死的）。"""
        canvas = self.image.copy()
        before = canvas.copy()
        self.editor._draw_startup_guide(canvas)
        self.assertFalse(np.array_equal(canvas, before))

    def test_visible_when_explicitly_enabled(self) -> None:
        self.assertTrue(self.editor.guide_visible)

    def test_drawn_on_screen_when_enabled(self) -> None:
        plain = self.editor.draw_rois(self.image)
        self.assertFalse(np.array_equal(plain, self.image))

    def test_disappears_after_picking_a(self) -> None:
        self.editor.on_key(ord("r"))
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 200, 100, 0, None)
        self.editor.on_mouse(cv2.EVENT_LBUTTONDOWN, 400, 300, 0, None)
        self.assertFalse(self.editor.guide_visible,
                         "框过 A 之后就不该再挡画面")

    def test_disappears_after_timeout(self) -> None:
        import src.roi_editor as module

        with mock.patch.object(module.time, "monotonic",
                               return_value=self.editor._guide_deadline + 1):
            self.assertFalse(self.editor.guide_visible)

    def test_hidden_while_selecting(self) -> None:
        self.editor.on_key(ord("r"))
        self.assertFalse(self.editor.guide_visible,
                         "框选时显示的是分步提示，不该两个框叠一起")

    def test_guide_box_inside_frame(self) -> None:
        """指引框不能画出画面外。"""
        for size in ((640, 480), (424, 240)):
            with self.subTest(size=size):
                editor = make_guide_editor()
                image = np.full((size[1], size[0], 3), 40, dtype=np.uint8)
                out = editor.draw_rois(image)
                self.assertEqual(out.shape, image.shape)

    def test_guide_contains_the_key_steps(self) -> None:
        """指引里必须写清"按 r → 点两下"，否则等于没写。"""
        import inspect
        import src.roi_editor as module

        source = inspect.getsource(module.RoiEditor._draw_startup_guide)
        for token in ("按 r", "点两下", "按 0", "按 s"):
            self.assertIn(token, source)
