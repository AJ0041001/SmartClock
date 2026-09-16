"""坐标映射单元测试。

核心断言：ROI 的边缘必须精确映射到挡板中心的有效边界，
这是"物理可动范围 ↔ 屏幕可动范围"对应关系的基石。
"""

from __future__ import annotations

import unittest

from src.mapper import (
    CoordinateMapper,
    Deadband,
    EmaSmoother,
    Roi,
    percent_to_game_x,
    percent_to_game_y,
)
from src.protocol import (
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
)


class TestRoi(unittest.TestCase):
    def test_full_frame(self) -> None:
        roi = Roi.full_frame(640, 480)
        self.assertEqual(roi.as_tuple(), (0, 0, 640, 480))

    def test_clamp_to_frame(self) -> None:
        roi = Roi(600, 400, 200, 200).clamp_to(640, 480)
        self.assertEqual(roi.x, 600)
        self.assertEqual(roi.y, 400)
        self.assertEqual(roi.w, 40)
        self.assertEqual(roi.h, 80)

    def test_dict_roundtrip(self) -> None:
        roi = Roi(10, 20, 30, 40)
        self.assertEqual(Roi.from_dict(roi.to_dict()), roi)


class TestBasicMapping(unittest.TestCase):
    """整幅画面 ROI 下的线性映射。"""

    def setUp(self) -> None:
        self.mapper = CoordinateMapper(
            roi=Roi(0, 0, 640, 480),
            smoothing=None,
            deadband=0,
        )

    def test_left_edge_maps_to_min_x(self) -> None:
        result = self.mapper.map_point(0, 240)
        self.assertEqual(result.game_x, CENTER_X_MIN)

    def test_right_edge_maps_to_max_x(self) -> None:
        result = self.mapper.map_point(640, 240)
        self.assertEqual(result.game_x, CENTER_X_MAX)

    def test_top_edge_maps_to_min_y(self) -> None:
        result = self.mapper.map_point(320, 0)
        self.assertEqual(result.game_y, CENTER_Y_MIN)

    def test_bottom_edge_maps_to_max_y(self) -> None:
        result = self.mapper.map_point(320, 480)
        self.assertEqual(result.game_y, CENTER_Y_MAX)

    def test_center_maps_to_middle(self) -> None:
        result = self.mapper.map_point(320, 240)
        expected_x = CENTER_X_MIN + (CENTER_X_MAX - CENTER_X_MIN) / 2
        expected_y = CENTER_Y_MIN + (CENTER_Y_MAX - CENTER_Y_MIN) / 2
        self.assertAlmostEqual(result.game_x, round(expected_x), delta=1)
        self.assertAlmostEqual(result.game_y, round(expected_y), delta=1)

    def test_out_of_roi_is_clamped(self) -> None:
        result = self.mapper.map_point(-500, 9999)
        self.assertEqual(result.game_x, CENTER_X_MIN)
        self.assertEqual(result.game_y, CENTER_Y_MAX)

    def test_monotonic_increase(self) -> None:
        """X 单调递增 —— 防止出现镜像翻转的意外符号错误。"""
        previous = None
        for cam_x in range(0, 641, 32):
            current = self.mapper.map_point(cam_x, 240).game_x
            if previous is not None:
                self.assertGreaterEqual(current, previous)
            previous = current


class TestRoiMapping(unittest.TestCase):
    """带 ROI 偏移与缩放的映射。"""

    def test_roi_offset(self) -> None:
        """ROI 左上角对应最小坐标，而不是画面左上角。"""
        mapper = CoordinateMapper(
            roi=Roi(100, 50, 400, 300),
            smoothing=None,
            deadband=0,
        )
        self.assertEqual(mapper.map_point(100, 50).game_x, CENTER_X_MIN)
        self.assertEqual(mapper.map_point(100, 50).game_y, CENTER_Y_MIN)
        self.assertEqual(mapper.map_point(500, 350).game_x, CENTER_X_MAX)
        self.assertEqual(mapper.map_point(500, 350).game_y, CENTER_Y_MAX)

    def test_point_before_roi_clamped(self) -> None:
        mapper = CoordinateMapper(
            roi=Roi(100, 50, 400, 300), smoothing=None, deadband=0
        )
        result = mapper.map_point(0, 0)
        self.assertEqual(result.game_x, CENTER_X_MIN)
        self.assertEqual(result.game_y, CENTER_Y_MIN)


class TestTransforms(unittest.TestCase):
    """镜像、轴交换、固定 Y。"""

    def test_invert_x(self) -> None:
        normal = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), smoothing=None, deadband=0
        )
        flipped = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), invert_x=True, smoothing=None, deadband=0
        )
        self.assertEqual(normal.map_point(0, 240).game_x, CENTER_X_MIN)
        self.assertEqual(flipped.map_point(0, 240).game_x, CENTER_X_MAX)

    def test_invert_y(self) -> None:
        mapper = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), invert_y=True, smoothing=None, deadband=0
        )
        self.assertEqual(mapper.map_point(320, 0).game_y, CENTER_Y_MAX)
        self.assertEqual(mapper.map_point(320, 480).game_y, CENTER_Y_MIN)

    def test_swap_xy(self) -> None:
        mapper = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), swap_xy=True, smoothing=None, deadband=0
        )
        # 交换后，摄像头 Y 决定游戏 X
        result = mapper.map_point(0, 0)
        self.assertEqual(result.game_x, CENTER_X_MIN)
        result_right = mapper.map_point(0, 640)
        self.assertEqual(result_right.game_x, CENTER_X_MAX)

    def test_fixed_y_ignores_camera_y(self) -> None:
        mapper = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), fixed_y=292,
            smoothing=None, deadband=0,
        )
        self.assertEqual(mapper.map_point(320, 0).game_y, 292)
        self.assertEqual(mapper.map_point(320, 480).game_y, 292)
        # X 仍然随摄像头变化
        self.assertNotEqual(
            mapper.map_point(0, 240).game_x,
            mapper.map_point(640, 240).game_x,
        )


class TestSmoothing(unittest.TestCase):
    """EMA 平滑器。"""

    def test_first_sample_passthrough(self) -> None:
        smoother = EmaSmoother(0.5)
        self.assertEqual(smoother.update(10.0, 20.0), (10.0, 20.0))

    def test_converges_towards_target(self) -> None:
        smoother = EmaSmoother(0.5)
        smoother.update(0.0, 0.0)
        for _ in range(30):
            smoother.update(100.0, 100.0)
        x, y = smoother.value or (0.0, 0.0)
        self.assertAlmostEqual(x, 100.0, delta=0.01)
        self.assertAlmostEqual(y, 100.0, delta=0.01)

    def test_reduces_jitter(self) -> None:
        """抖动输入经平滑后，相邻输出差值应显著小于输入差值。"""
        smoother = EmaSmoother(0.2)
        outputs = []
        for value in (100.0, 140.0, 100.0, 140.0, 100.0, 140.0):
            x, _ = smoother.update(value, 0.0)
            outputs.append(x)
        max_input_jump = 40.0
        max_output_jump = max(
            abs(outputs[i] - outputs[i - 1]) for i in range(1, len(outputs))
        )
        self.assertLess(max_output_jump, max_input_jump / 2)

    def test_invalid_alpha(self) -> None:
        for bad in (0.0, -0.5, 1.5):
            with self.subTest(alpha=bad):
                with self.assertRaises(ValueError):
                    EmaSmoother(bad)

    def test_reset(self) -> None:
        smoother = EmaSmoother(0.5)
        smoother.update(10.0, 10.0)
        smoother.reset()
        self.assertIsNone(smoother.value)
        self.assertEqual(smoother.update(99.0, 99.0), (99.0, 99.0))


class TestDeadband(unittest.TestCase):
    """死区滤波。"""

    def test_small_change_ignored(self) -> None:
        deadband = Deadband(threshold=2.0)
        deadband.update(100.0, 100.0)
        self.assertEqual(deadband.update(100.5, 101.0), (100.0, 100.0))

    def test_large_change_passes(self) -> None:
        deadband = Deadband(threshold=2.0)
        deadband.update(100.0, 100.0)
        result = deadband.update(110.0, 100.0)
        self.assertEqual(result[0], 110.0)

    def test_eliminates_jitter(self) -> None:
        """±1 像素的噪声应被完全抹平，输出恒定。"""
        deadband = Deadband(threshold=2.0)
        first = deadband.update(200.0, 200.0)
        for value in (201.0, 199.0, 200.5, 199.5, 201.0):
            result = deadband.update(value, 200.0)
            self.assertEqual(result, first)


class TestFullPipelineMapper(unittest.TestCase):
    """带平滑与死区的完整映射器。"""

    def test_smoothed_output_stays_in_range(self) -> None:
        mapper = CoordinateMapper(
            roi=Roi(0, 0, 640, 480), smoothing=0.3, deadband=1.0
        )
        for cam_x in range(0, 641, 17):
            for cam_y in range(0, 481, 23):
                result = mapper.map_point(cam_x, cam_y)
                self.assertGreaterEqual(result.game_x, CENTER_X_MIN)
                self.assertLessEqual(result.game_x, CENTER_X_MAX)
                self.assertGreaterEqual(result.game_y, CENTER_Y_MIN)
                self.assertLessEqual(result.game_y, CENTER_Y_MAX)

    def test_bind_frame_resizes_roi(self) -> None:
        mapper = CoordinateMapper(roi=Roi(0, 0, 1920, 1080), smoothing=None,
                                  deadband=0)
        mapper.bind_frame(640, 480)
        self.assertEqual(mapper.roi.w, 640)
        self.assertEqual(mapper.roi.h, 480)


class TestPercentHelpers(unittest.TestCase):
    def test_percent_endpoints(self) -> None:
        self.assertEqual(percent_to_game_x(0), CENTER_X_MIN)
        self.assertEqual(percent_to_game_x(100), CENTER_X_MAX)
        self.assertEqual(percent_to_game_y(0), CENTER_Y_MIN)
        self.assertEqual(percent_to_game_y(100), CENTER_Y_MAX)

    def test_percent_clamped(self) -> None:
        self.assertEqual(percent_to_game_x(-50), CENTER_X_MIN)
        self.assertEqual(percent_to_game_x(500), CENTER_X_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
