"""色块检测单元测试 —— 全部基于合成图像，无需真实摄像头。

合成图的价值在于**已知真值**：我们知道方块画在哪个像素坐标上，
因此可以精确断言检测出的质心是否准确，而不是"看起来差不多"。
"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from src.detector import (
    PRESET_COLORS,
    ColorDetector,
    HsvRange,
    dominant_colored_pixel,
    preset_ranges,
    sample_region_color,
)
from tests.fakes import (
    draw_block,
    draw_circle_block,
    make_solid_background,
)


class TestPresets(unittest.TestCase):
    def test_all_presets_parse(self) -> None:
        for name in PRESET_COLORS:
            with self.subTest(name=name):
                ranges = preset_ranges(name)
                self.assertTrue(ranges)
                for hsv_range in ranges:
                    self.assertEqual(len(hsv_range.lower), 3)
                    self.assertEqual(len(hsv_range.upper), 3)

    def test_red_has_two_ranges_for_hue_wrap(self) -> None:
        """红色跨越 H=0/180 边界，必须拆成两段，否则会漏检一半。"""
        self.assertEqual(len(preset_ranges("red")), 2)

    def test_unknown_preset_raises(self) -> None:
        with self.assertRaises(KeyError):
            preset_ranges("chartreuse")

    def test_hsv_range_dict_roundtrip(self) -> None:
        original = HsvRange((1, 2, 3), (4, 5, 6))
        self.assertEqual(HsvRange.from_dict(original.to_dict()), original)


class TestDetectionAccuracy(unittest.TestCase):
    """检测位置精度。"""

    def setUp(self) -> None:
        self.detector = ColorDetector.from_preset(
            "red", min_area=200, morph_kernel=3, morph_iterations=1
        )

    def test_circle_centroid_is_exact(self) -> None:
        """圆的质心可解析求解，应极其接近绘制中心。"""
        background = make_solid_background()
        target = (321, 245)
        image = draw_circle_block(background, target, radius=40)
        detection = self.detector.detect(image)
        self.assertIsNotNone(detection)
        assert detection is not None

        dx = abs(detection.center[0] - target[0])
        dy = abs(detection.center[1] - target[1])
        self.assertLess(dx, 2.0, f"X 偏差过大：{dx}")
        self.assertLess(dy, 2.0, f"Y 偏差过大：{dy}")

    def test_square_centroid_is_accurate(self) -> None:
        background = make_solid_background()
        target = (400, 300)
        image = draw_block(background, target, size=80)
        detection = self.detector.detect(image)
        self.assertIsNotNone(detection)
        assert detection is not None

        self.assertAlmostEqual(detection.center[0], target[0], delta=3)
        self.assertAlmostEqual(detection.center[1], target[1], delta=3)

    def test_bbox_is_reasonable(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (200, 200), size=60)
        detection = self.detector.detect(image)
        assert detection is not None
        x, y, w, h = detection.bbox
        self.assertGreaterEqual(w, 50)
        self.assertLessEqual(w, 70)
        self.assertGreaterEqual(h, 50)
        self.assertLessEqual(h, 70)

    def test_area_is_positive(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (200, 200), size=60)
        detection = self.detector.detect(image)
        assert detection is not None
        self.assertGreater(detection.area, 2000)


class TestNegativeCases(unittest.TestCase):
    """不该误检的场景。"""

    def setUp(self) -> None:
        self.detector = ColorDetector.from_preset("red", min_area=200)

    def test_plain_background_no_detection(self) -> None:
        image = make_solid_background()
        self.assertIsNone(self.detector.detect(image))

    def test_different_color_not_detected(self) -> None:
        """绿色方块不应被"红色"检测器检出。"""
        background = make_solid_background()
        image = draw_block(background, (320, 240), size=60,
                           color_bgr=(0, 255, 0))
        self.assertIsNone(self.detector.detect(image))

    def test_tiny_block_filtered_by_min_area(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (320, 240), size=8)
        self.assertIsNone(self.detector.detect(image))

    def test_tiny_block_detected_with_low_min_area(self) -> None:
        """放宽 min_area 后同一个方块应能被检出 —— 证明是面积过滤而非分割失败。"""
        background = make_solid_background()
        image = draw_block(background, (320, 240), size=8)
        detector = ColorDetector.from_preset(
            "red", min_area=5, morph_kernel=1, morph_iterations=0
        )
        detection = detector.detect(image)
        self.assertIsNotNone(detection)

    def test_empty_image(self) -> None:
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        self.assertEqual(self.detector.detect_all(empty), [])


class TestPresetColors(unittest.TestCase):
    """各预设色都能检到自己对应的颜色。"""

    #: 预设名 → 用于绘制的 BGR
    COLOR_BGR = {
        "red": (0, 0, 255),
        "green": (0, 255, 0),
        "blue": (255, 0, 0),
        "yellow": (0, 255, 255),
        "orange": (0, 165, 255),
        "purple": (128, 0, 128),
        "cyan": (255, 255, 0),
    }

    def test_each_preset_detects_its_color(self) -> None:
        for name, bgr in self.COLOR_BGR.items():
            with self.subTest(color=name):
                detector = ColorDetector.from_preset(
                    name, min_area=200, morph_kernel=3, morph_iterations=1
                )
                background = make_solid_background()
                image = draw_block(background, (320, 240), size=80,
                                   color_bgr=bgr)
                detection = detector.detect(image)
                self.assertIsNotNone(
                    detection, f"预设 {name} 未能检出对应颜色 {bgr}"
                )

    def test_red_wraparound_both_sides(self) -> None:
        """纯红（H≈0）与略偏品红的红（H≈175）都应被红色预设检出。"""
        detector = ColorDetector.from_preset(
            "red", min_area=100, morph_kernel=1, morph_iterations=0
        )
        for hue in (0, 178):
            with self.subTest(hue=hue):
                hsv = np.zeros((100, 100, 3), dtype=np.uint8)
                hsv[:, :] = (hue, 255, 255)
                bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
                detection = detector.detect(bgr)
                self.assertIsNotNone(detection, f"H={hue} 未被检出")


class TestRoiRestriction(unittest.TestCase):
    """ROI 限制检测范围。"""

    def test_block_outside_roi_ignored(self) -> None:
        detector = ColorDetector.from_preset(
            "red", min_area=100, roi=(0, 0, 200, 200)
        )
        background = make_solid_background()
        image = draw_block(background, (500, 400), size=60)  # ROI 之外
        self.assertIsNone(detector.detect(image))

    def test_block_inside_roi_detected_with_global_coords(self) -> None:
        """ROI 内的坐标必须换算回整图坐标系，否则映射会整体偏移。"""
        detector = ColorDetector.from_preset(
            "red", min_area=100, roi=(300, 200, 200, 200),
            morph_kernel=3, morph_iterations=1,
        )
        background = make_solid_background()
        target = (400, 300)  # ROI 中心
        image = draw_block(background, target, size=60)
        detection = detector.detect(image)
        self.assertIsNotNone(detection)
        assert detection is not None
        self.assertAlmostEqual(detection.center[0], target[0], delta=3)
        self.assertAlmostEqual(detection.center[1], target[1], delta=3)


class TestMultiBlock(unittest.TestCase):
    def test_max_blocks_one_returns_largest(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (150, 150), size=40)
        image = draw_block(image, (450, 350), size=100)

        detector = ColorDetector.from_preset(
            "red", min_area=100, max_blocks=1,
            morph_kernel=3, morph_iterations=1,
        )
        detection = detector.detect(image)
        assert detection is not None
        # 应选面积更大的那个
        self.assertAlmostEqual(detection.center[0], 450, delta=5)

    def test_max_blocks_all_returns_both(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (150, 150), size=40)
        image = draw_block(image, (450, 350), size=100)

        detector = ColorDetector.from_preset(
            "red", min_area=100, max_blocks=5,
            morph_kernel=3, morph_iterations=1,
        )
        found = detector.detect_all(image)
        self.assertEqual(len(found), 2)
        # 应按面积降序
        self.assertGreater(found[0].area, found[1].area)


class TestColorSampling(unittest.TestCase):
    """取色标定辅助。"""

    def test_sample_red_block(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (320, 240), size=100,
                           color_bgr=(0, 0, 255))
        sampled = sample_region_color(image, 320, 240, radius=20)

        self.assertGreater(sampled.sample_count, 100)
        # 纯红的 H 接近 0 或 180
        hue = sampled.center_hsv[0]
        self.assertTrue(hue < 15 or hue > 165, f"意外的 Hue={hue}")
        # 采样出的区间必须能重新检出该色块
        detector = ColorDetector(sampled.ranges, min_area=100)
        self.assertIsNotNone(detector.detect(image))

    def test_sample_on_background_raises(self) -> None:
        """采样点落在低饱和背景上应明确报错，而不是给出一段垃圾区间。"""
        image = make_solid_background(color=(230, 230, 230))
        with self.assertRaises(ValueError):
            sample_region_color(image, 320, 240, radius=20)

    def test_sample_outside_image_raises(self) -> None:
        image = make_solid_background()
        with self.assertRaises(ValueError):
            sample_region_color(image, 5000, 5000, radius=10)

    def test_dominant_colored_pixel_finds_block(self) -> None:
        background = make_solid_background()
        target = (480, 120)
        image = draw_block(background, target, size=80, color_bgr=(0, 0, 255))
        found = dominant_colored_pixel(image)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertAlmostEqual(found[0], target[0], delta=5)
        self.assertAlmostEqual(found[1], target[1], delta=5)

    def test_dominant_colored_pixel_on_empty(self) -> None:
        image = make_solid_background()
        self.assertIsNone(dominant_colored_pixel(image))


class TestRendering(unittest.TestCase):
    """可视化不应崩溃，也不应修改原图。"""

    def test_draw_does_not_mutate_input(self) -> None:
        background = make_solid_background()
        image = draw_block(background, (320, 240), size=60)
        original = image.copy()

        detector = ColorDetector.from_preset("red", min_area=100)
        detection = detector.detect(image)
        assert detection is not None

        canvas = detector.draw(image, [detection])
        self.assertEqual(canvas.shape, image.shape)
        np.testing.assert_array_equal(image, original)

    def test_draw_roi(self) -> None:
        detector = ColorDetector.from_preset("red", roi=(10, 10, 100, 100))
        image = make_solid_background()
        canvas = detector.draw_roi(image)
        self.assertEqual(canvas.shape, image.shape)


class TestMaskShape(unittest.TestCase):
    def test_mask_is_binary_and_same_size(self) -> None:
        detector = ColorDetector.from_preset("red", min_area=100)
        image = make_solid_background()
        mask = detector.make_mask(image)
        self.assertEqual(mask.shape, image.shape[:2])
        self.assertEqual(mask.dtype, np.uint8)
        unique = np.unique(mask)
        self.assertTrue(set(unique.tolist()).issubset({0, 255}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
