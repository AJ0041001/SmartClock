"""中文文字渲染测试。

为什么值得单独测
----------------
``cv2.putText`` 画不了中文，而且**不报错** —— 只是字不见了。
这类问题在窗口里表现为"状态栏那一行是空的"，很容易被当成逻辑 bug
查半天。所以这里把渲染路径锁住：

  · 有中文字体时必须真的画出东西（像素要有变化）
  · 没有中文字体时必须**降级**而不是画乱码
  · 文字被画到画面外、或一部分在画面外时不能崩
"""

from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from src import text_cjk
from src.text_cjk import (
    draw_text,
    find_cjk_font,
    has_cjk_font,
    render_bitmap,
    strip_non_ascii,
)

CJK_SAMPLE = "正在框选 A（映射区域）"
ASCII_SAMPLE = "B=(116,0,406,480)"


class TestFontDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        text_cjk.find_cjk_font.cache_clear()

    def tearDown(self) -> None:
        text_cjk.find_cjk_font.cache_clear()

    def test_font_path_exists_when_found(self) -> None:
        path = find_cjk_font()
        if path is None:
            self.skipTest("本机没有中文字体（降级分支另有测试覆盖）")
        import os

        self.assertTrue(os.path.isfile(path), f"字体路径不存在：{path}")

    def test_result_is_cached(self) -> None:
        self.assertIs(find_cjk_font(), find_cjk_font())


class TestStripNonAscii(unittest.TestCase):
    def test_pure_ascii_untouched(self) -> None:
        self.assertEqual(strip_non_ascii(ASCII_SAMPLE), ASCII_SAMPLE)

    def test_chinese_removed(self) -> None:
        self.assertEqual(strip_non_ascii("中文 abc 测试"), "abc")

    def test_all_chinese_becomes_empty(self) -> None:
        self.assertEqual(strip_non_ascii("全部是中文"), "")

    def test_whitespace_collapsed(self) -> None:
        self.assertEqual(strip_non_ascii("a    b"), "a b")


class TestRenderBitmap(unittest.TestCase):
    def setUp(self) -> None:
        render_bitmap.cache_clear()

    def tearDown(self) -> None:
        render_bitmap.cache_clear()

    def test_empty_text_returns_none(self) -> None:
        self.assertIsNone(render_bitmap("", 14, (255, 255, 255)))

    def test_ascii_renders(self) -> None:
        if not has_cjk_font():
            self.skipTest("没有中文字体")
        bitmap = render_bitmap(ASCII_SAMPLE, 14, (255, 255, 255))
        self.assertIsNotNone(bitmap, "ASCII 文字也应该能渲染出位图")
        bgr, alpha = bitmap
        self.assertEqual(bgr.shape[:2], alpha.shape[:2])
        self.assertEqual(bgr.shape[2], 3)

    def test_chinese_renders(self) -> None:
        if not has_cjk_font():
            self.skipTest("没有中文字体")
        bitmap = render_bitmap(CJK_SAMPLE, 15, (255, 255, 255))
        self.assertIsNotNone(bitmap, "有中文字体却渲染不出中文")
        bgr, alpha = bitmap
        self.assertGreater(bgr.shape[1], 20, "中文位图宽度太窄，可能没画出字")
        self.assertTrue((alpha > 0).any(), "alpha 全 0 说明什么都没画")

    def test_bitmap_is_cached(self) -> None:
        if not has_cjk_font():
            self.skipTest("没有中文字体")
        first = render_bitmap(ASCII_SAMPLE, 14, (255, 255, 255))
        second = render_bitmap(ASCII_SAMPLE, 14, (255, 255, 255))
        self.assertIs(first, second, "重复文字应命中缓存，不必每帧重渲染")


class TestDrawText(unittest.TestCase):
    def setUp(self) -> None:
        render_bitmap.cache_clear()
        self.image = np.full((60, 400, 3), 30, dtype=np.uint8)

    def tearDown(self) -> None:
        render_bitmap.cache_clear()

    def test_empty_text_leaves_image_alone(self) -> None:
        before = self.image.copy()
        draw_text(self.image, "", 5, 5)
        np.testing.assert_array_equal(self.image, before)

    def test_chinese_changes_pixels(self) -> None:
        if not has_cjk_font():
            self.skipTest("没有中文字体")
        before = self.image.copy()
        draw_text(self.image, CJK_SAMPLE, 5, 5, 15)
        self.assertFalse(
            np.array_equal(self.image, before),
            "画了中文但画面没变化 —— 字没画出来",
        )

    def test_ascii_changes_pixels(self) -> None:
        before = self.image.copy()
        draw_text(self.image, ASCII_SAMPLE, 5, 5, 15)
        self.assertFalse(np.array_equal(self.image, before))

    def test_returns_same_array(self) -> None:
        self.assertIs(draw_text(self.image, ASCII_SAMPLE, 5, 5), self.image)

    def test_shadow_draws_more(self) -> None:
        if not has_cjk_font():
            self.skipTest("没有中文字体")
        plain = np.full((40, 300, 3), 30, dtype=np.uint8)
        shadowed = plain.copy()
        draw_text(plain, "ABC", 5, 5, 15)
        draw_text(shadowed, "ABC", 5, 5, 15, shadow=True)
        self.assertGreater(
            int(np.abs(shadowed.astype(int) - 30).sum()),
            int(np.abs(plain.astype(int) - 30).sum()),
            "带描边应该多画一层",
        )

    def test_partially_offscreen_left(self) -> None:
        draw_text(self.image, CJK_SAMPLE, -60, 5, 15)      # 不该崩
        self.assertEqual(self.image.shape, (60, 400, 3))

    def test_partially_offscreen_top(self) -> None:
        draw_text(self.image, CJK_SAMPLE, 5, -8, 15)
        self.assertEqual(self.image.shape, (60, 400, 3))

    def test_fully_offscreen_right(self) -> None:
        before = self.image.copy()
        draw_text(self.image, CJK_SAMPLE, 5000, 5, 15)
        np.testing.assert_array_equal(self.image, before)

    def test_fully_offscreen_below(self) -> None:
        before = self.image.copy()
        draw_text(self.image, CJK_SAMPLE, 5, 5000, 15)
        np.testing.assert_array_equal(self.image, before)

    def test_clipped_at_right_edge_does_not_wrap(self) -> None:
        """文字超出右边界时应被裁掉，不能让 numpy 切片出错。"""
        draw_text(self.image, "X" * 200, 380, 5, 15)
        self.assertEqual(self.image.shape, (60, 400, 3))


class TestFallbackWithoutFont(unittest.TestCase):
    """没有中文字体时的降级路径 —— 必须不崩、不画乱码。"""

    def setUp(self) -> None:
        render_bitmap.cache_clear()
        self.image = np.full((40, 300, 3), 30, dtype=np.uint8)

    def tearDown(self) -> None:
        render_bitmap.cache_clear()

    def test_render_bitmap_returns_none(self) -> None:
        with mock.patch.object(text_cjk, "_load_font", lambda size: None):
            self.assertIsNone(render_bitmap("abc", 14, (255, 255, 255)))

    def test_ascii_still_drawn_by_opencv(self) -> None:
        before = self.image.copy()
        with mock.patch.object(text_cjk, "_load_font", lambda size: None):
            draw_text(self.image, ASCII_SAMPLE, 5, 5, 15)
        self.assertFalse(np.array_equal(self.image, before),
                         "退回到 cv2.putText 后 ASCII 仍应可见")

    def test_chinese_degrades_to_empty_not_garbage(self) -> None:
        """纯中文在没有字体时宁可什么都不画，也不画出问号。"""
        before = self.image.copy()
        with mock.patch.object(text_cjk, "_load_font", lambda size: None):
            draw_text(self.image, "已保存到 config.yaml", 5, 5, 15)
        self.assertFalse(
            np.array_equal(self.image, before),
            "中文里的 ASCII 部分（config.yaml）应该保留下来",
        )

    def test_pure_chinese_leaves_image_clean(self) -> None:
        before = self.image.copy()
        with mock.patch.object(text_cjk, "_load_font", lambda size: None):
            draw_text(self.image, "全部是中文", 5, 5, 15)
        np.testing.assert_array_equal(
            self.image, before,
            "画不出中文时不该留下任何涂鸦",
        )

    def test_describe_runs(self) -> None:
        self.assertIsInstance(text_cjk.describe(), str)
        self.assertTrue(text_cjk.describe())


if __name__ == "__main__":
    unittest.main(verbosity=2)
