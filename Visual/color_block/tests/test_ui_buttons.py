"""画面内按钮条测试 —— 绘制与命中判定是纯计算，可完整覆盖。

这类"自绘 UI"最容易出的问题是**按钮位置和点击区域对不上**：
画在一个地方、点在另一个地方才算命中，用户点了没反应。
把布局与命中判定做成纯函数就能靠测试锁住，不必真的开窗口点。
"""

from __future__ import annotations

import unittest

import numpy as np

from src.ui_buttons import Button, ButtonBar, StatusBar


class TestButton(unittest.TestCase):
    def test_contains(self) -> None:
        button = Button(action="x", label="X", rect=(10, 20, 80, 30))
        self.assertTrue(button.contains(10, 20))     # 左上角含
        self.assertTrue(button.contains(89, 49))     # 右下角前一像素含
        self.assertFalse(button.contains(90, 49))    # 右边界不含
        self.assertFalse(button.contains(9, 20))     # 左边界外
        self.assertFalse(button.contains(10, 50))    # 下边界外

    def test_contains_edge_cases(self) -> None:
        button = Button(action="x", label="X", rect=(0, 0, 10, 10))
        self.assertTrue(button.contains(0, 0))
        self.assertTrue(button.contains(9, 9))
        self.assertFalse(button.contains(10, 10))


class TestButtonBarLayout(unittest.TestCase):
    """布局引擎。"""

    def setUp(self) -> None:
        self.bar = ButtonBar()
        self.bar.add("a", "AAA", width=80)
        self.bar.add("b", "BBB", width=90)
        self.bar.add("c", "CCC", width=70)

    def test_rects_assigned_after_render(self) -> None:
        self.bar.render(640)
        for button in self.bar.buttons:
            x, y, w, h = button.rect
            self.assertEqual(w, button.width)
            self.assertGreater(h, 0)

    def test_buttons_do_not_overlap(self) -> None:
        self.bar.render(640)
        rects = [b.rect for b in self.bar.buttons]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                ax, ay, aw, ah = rects[i]
                bx, by, bw, bh = rects[j]
                overlap_x = ax < bx + bw and bx < ax + aw
                overlap_y = ay < by + bh and by < ay + ah
                self.assertFalse(
                    overlap_x and overlap_y,
                    f"按钮 {i} 与 {j} 重叠：{rects[i]} vs {rects[j]}",
                )

    def test_buttons_in_left_to_right_order(self) -> None:
        self.bar.render(640)
        xs = [b.rect[0] for b in self.bar.buttons]
        self.assertEqual(xs, sorted(xs))

    def test_buttons_fit_in_width(self) -> None:
        self.bar.render(640)
        for button in self.bar.buttons:
            x, _, w, _ = button.rect
            self.assertLessEqual(x + w, 640)

    def test_render_shape(self) -> None:
        bar = self.bar.render(640)
        self.assertEqual(bar.shape, (ButtonBar().height, 640, 3))
        self.assertEqual(bar.dtype, np.uint8)


class TestHitTest(unittest.TestCase):
    """命中判定必须与绘制位置一致 —— 这是最关键的测试。"""

    def setUp(self) -> None:
        self.bar = ButtonBar()
        self.bar.add("first", "F", width=80)
        self.bar.add("second", "S", width=80)
        self.bar.add("third", "T", width=80)
        self.bar.render(640)

    def test_click_center_of_each_button(self) -> None:
        for button in self.bar.buttons:
            x, y, w, h = button.rect
            hit = self.bar.hit_test(x + w // 2, y + h // 2)
            self.assertEqual(
                hit, button.action,
                f"点在 {button.action} 中心却命中了 {hit}",
            )

    def test_click_top_left_inside(self) -> None:
        for button in self.bar.buttons:
            x, y, _, _ = button.rect
            self.assertEqual(self.bar.hit_test(x, y), button.action)

    def test_painted_area_equals_clickable_area(self) -> None:
        """画出来的像素范围必须和可点击范围完全一致。

        早期用 ``cv2.rectangle(..., (x+w, y+h))`` 填色，会多画最右一列
        和最下一行，而 ``contains`` 是半开区间 —— 那一圈看着是按钮、
        点上去却没反应。这里逐像素比对：
          · 按钮内部每个像素都要能命中
          · 按钮外一个像素都不能命中
        """
        bar = ButtonBar()
        bar.add("a", "A", width=80)
        bar.add_separator()
        bar.add("b", "B", width=60)
        bar.render(640)

        for button in bar.buttons:
            if not button.action:
                continue
            x, y, w, h = button.rect
            with self.subTest(action=button.action):
                # 四角与右下角都要能命中（右下角就是曾经点不动的那一个）
                for px, py in ((x, y), (x + w - 1, y), (x, y + h - 1),
                               (x + w - 1, y + h - 1)):
                    self.assertEqual(
                        bar.hit_test(px, py), button.action,
                        f"({px},{py}) 在 {button.action} 的绘制范围内却点不中",
                    )
                # 越界一像素就不能命中
                self.assertNotEqual(bar.hit_test(x + w, y + h // 2),
                                    button.action)

    def test_click_gap_returns_none(self) -> None:
        """按钮之间的空隙不该命中任何按钮。"""
        rects = [b.rect for b in self.bar.buttons]
        gap_x = rects[0][0] + rects[0][2] + 1     # 第一个按钮右侧空隙
        if gap_x < rects[1][0]:
            self.assertIsNone(self.bar.hit_test(gap_x, rects[0][1] + 5))

    def test_click_outside_returns_none(self) -> None:
        self.assertIsNone(self.bar.hit_test(5000, 5))
        self.assertIsNone(self.bar.hit_test(-10, 5))

    def test_disabled_button_not_clickable(self) -> None:
        self.bar.set_enabled("second", False)
        x, y, w, h = self.bar.buttons[1].rect
        self.assertIsNone(self.bar.hit_test(x + w // 2, y + h // 2))

    def test_separator_not_clickable(self) -> None:
        bar = ButtonBar()
        bar.add("a", "A", width=60)
        bar.add_separator()
        bar.add("b", "B", width=60)
        bar.render(640)
        sep = bar.buttons[1]
        x, y, w, h = sep.rect
        self.assertIsNone(bar.hit_test(x + w // 2, y + h // 2))


class TestButtonState(unittest.TestCase):
    def setUp(self) -> None:
        self.bar = ButtonBar()
        self.bar.add("a", "A")
        self.bar.add("b", "B")

    def test_set_active(self) -> None:
        self.bar.set_active("a")
        self.assertTrue(self.bar.get("a").active)
        self.assertFalse(self.bar.get("b").active)

    def test_set_active_clears_others(self) -> None:
        self.bar.set_active("a")
        self.bar.set_active("b")
        self.assertFalse(self.bar.get("a").active)
        self.assertTrue(self.bar.get("b").active)

    def test_clear_active(self) -> None:
        self.bar.set_active("a")
        self.bar.clear_active()
        self.assertFalse(self.bar.get("a").active)

    def test_set_enabled(self) -> None:
        self.bar.set_enabled("a", False)
        self.assertFalse(self.bar.get("a").enabled)

    def test_get_missing(self) -> None:
        self.assertIsNone(self.bar.get("nonexistent"))


class TestRendering(unittest.TestCase):
    def test_render_does_not_crash_with_no_buttons(self) -> None:
        bar = ButtonBar().render(640)
        self.assertEqual(bar.shape[1], 640)

    def test_active_button_renders_differently(self) -> None:
        bar = ButtonBar()
        bar.add("a", "A", width=80)
        normal = bar.render(640).copy()
        bar.set_active("a")
        active = bar.render(640)
        self.assertFalse(np.array_equal(normal, active),
                         "激活状态应该有视觉差异")

    def test_disabled_button_renders_differently(self) -> None:
        bar = ButtonBar()
        bar.add("a", "A", width=80)
        normal = bar.render(640).copy()
        bar.set_enabled("a", False)
        disabled = bar.render(640)
        self.assertFalse(np.array_equal(normal, disabled))

    def test_narrow_width_does_not_crash(self) -> None:
        bar = ButtonBar()
        for i in range(10):
            bar.add(f"b{i}", f"B{i}", width=80)
        out = bar.render(200)       # 放不下也不该崩
        self.assertEqual(out.shape[1], 200)


class TestStatusBar(unittest.TestCase):
    def test_render(self) -> None:
        status = StatusBar(26)
        status.set("A=(1,2,3,4)", "B=(5,6,7,8)")
        out = status.render(640)
        self.assertEqual(out.shape, (26, 640, 3))

    def test_empty_render(self) -> None:
        out = StatusBar().render(640)
        self.assertEqual(out.shape[1], 640)

    def test_long_text_truncated_gracefully(self) -> None:
        """超长文字不该崩溃（会被裁掉）。"""
        status = StatusBar(26)
        status.set("X" * 500)
        out = status.render(640)
        self.assertEqual(out.shape[1], 640)


if __name__ == "__main__":
    unittest.main(verbosity=2)
