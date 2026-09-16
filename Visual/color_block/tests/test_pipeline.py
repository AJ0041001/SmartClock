"""端到端流水线测试 —— 合成图像进，真实协议帧出。

这是本项目最重要的测试：它把"检测 → 映射 → 组帧 → 发送"整条链路
串起来验证。由于用的是完全可控的合成图像，每一步的期望值都可以
精确计算，因此能真正断言"喂进一个红色方块，STM32 会收到什么字节"。
"""

from __future__ import annotations

import unittest

from src.config import AppConfig
from src.pipeline import ColorTrackingPipeline, Stats
from src.protocol import (
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
    ControlType,
    Response,
    hexdump,
    parse_frame,
)
from tests.fakes import FakeCapture, FakeLink


def make_config(**overrides) -> AppConfig:
    """构造一份"确定性"测试配置：关闭平滑与死区，限速拉满。

    刻意用 **HSV 引擎**：它纯 CPU、结果确定、不依赖 NPU。
    本文件测的是**流水线逻辑**（帧率控制、丢失重捕、串口组帧），
    与用哪种检测器无关。YOLO 引擎由 tests/test_card_detector.py
    用假运行时单独覆盖。
    """
    config = AppConfig()
    config.camera.width = 640
    config.camera.height = 480
    config.detector.engine = "hsv"        # ← 不碰 NPU，保证测试可离线运行
    config.detector.preset = "red"
    config.detector.min_area = 200
    config.detector.morph_kernel = 3
    config.detector.morph_iterations = 1
    config.mapping.roi_x = 0
    config.mapping.roi_y = 0
    config.mapping.roi_w = 640
    config.mapping.roi_h = 480
    config.mapping.smoothing = None   # 关闭平滑，便于精确断言
    config.mapping.deadband = 0.0     # 关闭死区
    config.loop.target_fps = 1000.0
    config.loop.send_interval = 0.0   # 不限速，每帧都发
    config.loop.lost_frames = 3
    config.loop.reacquire_delay = 0.0
    config.debug.preview = False
    config.debug.save_frames = False

    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(config, section), field, value)
    return config


# ── 期望值独立推算 ────────────────────────────────────────────────────
# 刻意在测试里**重新实现一遍**线性插值公式，而不是复用 src.mapper 的实现。
# 这样万一 mapper 里的公式被改错，测试依然能抓到，而不是"自己验自己"。
#
# 注意：色块是有尺寸的。画在 (cx, cy) 的方块只要完整可见，其图像矩质心
# 就等于 (cx, cy)。所以期望值必须由**绘制中心**推算，不能想当然地把
# "画面左边缘"等同于"方块放在 x=30"。要得到真正的 CENTER_X_MIN，
# 质心必须落在 x=0，而那时方块已被裁掉一半，质心会偏移。


def expected_game_x(cam_x: float) -> int:
    nx = max(0.0, min(1.0, cam_x / 640.0))
    return int(round(CENTER_X_MIN + nx * (CENTER_X_MAX - CENTER_X_MIN)))


def expected_game_y(cam_y: float) -> int:
    ny = max(0.0, min(1.0, cam_y / 480.0))
    return int(round(CENTER_Y_MIN + ny * (CENTER_Y_MAX - CENTER_Y_MIN)))


class TestEndToEndTracking(unittest.TestCase):
    """整链路：像素坐标 → 游戏坐标 → 串口字节。"""

    def _run_single(self, block_center, config=None):
        """让流水线处理一张含色块的图，返回 (StepResult, FakeLink)。"""
        config = config or make_config()
        capture = FakeCapture([block_center])
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        frame = capture.read()
        assert frame is not None
        result = pipeline.step(frame)
        pipeline.close()
        return result, link

    def test_center_block_maps_to_middle(self) -> None:
        result, link = self._run_single((320, 240))
        self.assertTrue(result.detected)
        self.assertTrue(result.sent)

        parsed = parse_frame(link.last_frame)
        self.assertEqual(parsed.control_type, ControlType.SERIAL_AND_KEYS)
        self.assertTrue(parsed.crc_ok)
        # 画面正中 → 游戏坐标中点附近
        self.assertAlmostEqual(parsed.x, (CENTER_X_MIN + CENTER_X_MAX) // 2,
                               delta=2)
        self.assertAlmostEqual(parsed.y, (CENTER_Y_MIN + CENTER_Y_MAX) // 2,
                               delta=2)

    def test_left_side_block_maps_linearly(self) -> None:
        """色块靠近左边 → 游戏 X 接近下界，且与线性插值一致。"""
        cam_x = 30
        _, link = self._run_single((cam_x, 240))
        parsed = parse_frame(link.last_frame)
        self.assertAlmostEqual(parsed.x, expected_game_x(cam_x), delta=2)
        # 靠近左边界（不是恰好等于，因为方块完整可见时质心到不了 x=0）
        self.assertLess(parsed.x, CENTER_X_MIN + 40)

    def test_right_side_block_maps_linearly(self) -> None:
        cam_x = 610
        _, link = self._run_single((cam_x, 240))
        parsed = parse_frame(link.last_frame)
        self.assertAlmostEqual(parsed.x, expected_game_x(cam_x), delta=2)
        self.assertGreater(parsed.x, CENTER_X_MAX - 40)

    def test_top_block_maps_linearly(self) -> None:
        cam_y = 30
        _, link = self._run_single((320, cam_y))
        parsed = parse_frame(link.last_frame)
        self.assertAlmostEqual(parsed.y, expected_game_y(cam_y), delta=2)
        self.assertLess(parsed.y, CENTER_Y_MIN + 60)

    def test_bottom_block_maps_linearly(self) -> None:
        cam_y = 450
        _, link = self._run_single((320, cam_y))
        parsed = parse_frame(link.last_frame)
        self.assertAlmostEqual(parsed.y, expected_game_y(cam_y), delta=2)
        self.assertGreater(parsed.y, CENTER_Y_MAX - 60)

    def test_near_edge_centroid_approaches_minimum(self) -> None:
        """质心贴近画面左上角时，游戏坐标应非常接近下界。

        说明：要让结果**精确等于** CENTER_X_MIN，质心必须落在 x=0，
        而任何有宽度的方块都做不到（左半边会被裁掉，质心右移）。
        因此这里断言"逼近下界"，端点可达性由映射器单元测试覆盖。
        """
        config = make_config(**{
            "detector.min_area": 8,
            "detector.morph_kernel": 1,
            "detector.morph_iterations": 0,
        })
        capture = FakeCapture([(3, 3)], block_size=6)
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        frame = capture.read()
        assert frame is not None
        pipeline.step(frame)
        pipeline.close()

        parsed = parse_frame(link.last_frame)
        # 用"占量程的比例"而非绝对像素做断言：X/Y 两轴量程不同
        # （X 354，Y 572），固定像素容差会偏向其中一轴。
        self.assertLess((parsed.x - CENTER_X_MIN) / (CENTER_X_MAX - CENTER_X_MIN), 0.01)
        self.assertLess((parsed.y - CENTER_Y_MIN) / (CENTER_Y_MAX - CENTER_Y_MIN), 0.01)

    def test_frame_bytes_are_exactly_ten(self) -> None:
        _, link = self._run_single((320, 240))
        self.assertEqual(len(link.last_frame), 10)

    def test_tracking_is_monotonic_left_to_right(self) -> None:
        """从左到右扫过画面，游戏 X 应单调不减 —— 捕捉任何符号错误。"""
        previous = None
        for cam_x in (50, 150, 250, 350, 450, 550, 600):
            _, link = self._run_single((cam_x, 240))
            parsed = parse_frame(link.last_frame)
            if previous is not None:
                self.assertGreaterEqual(parsed.x, previous)
            previous = parsed.x


class TestLostAndReacquire(unittest.TestCase):
    """丢失与重新捕获的处理。"""

    def test_lost_sends_idle_type_02(self) -> None:
        config = make_config(**{"loop.lost_frames": 3})
        # 第 1 帧有块，之后连续 3 帧没有
        capture = FakeCapture([(320, 240), None, None, None], repeat_last=True)
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()

        for _ in range(4):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        # 最后一帧应是 TYPE=02 的交还帧
        parsed = parse_frame(link.last_frame)
        self.assertEqual(parsed.control_type, ControlType.KEYS_ONLY)
        self.assertGreaterEqual(pipeline.stats.idle_sent, 1)

    def test_no_send_while_searching_before_threshold(self) -> None:
        """还没到丢失阈值时不应发交还帧。"""
        config = make_config(**{"loop.lost_frames": 5})
        capture = FakeCapture([(320, 240), None, None, None, None],
                              repeat_last=True)
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()

        for _ in range(2):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        self.assertEqual(pipeline.stats.idle_sent, 0)

    def test_reacquire_resumes_tracking_frames(self) -> None:
        """丢失后重新检出，必须能再次发出 TYPE=01 数据帧。

        这里专门回归一个曾经的 bug：重新捕获的计时基准如果写成
        "最近检出时刻"，差值恒为 0，会永远卡在等待收敛的分支里。
        """
        config = make_config(**{
            "loop.lost_frames": 2,
            "loop.reacquire_delay": 0.0,
        })
        positions = [(320, 240), None, None, (500, 240)]
        capture = FakeCapture(positions, repeat_last=True)
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()

        for _ in range(4):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        parsed = parse_frame(link.last_frame)
        self.assertEqual(
            parsed.control_type, ControlType.SERIAL_AND_KEYS,
            "重新捕获后应恢复 TYPE=01 数据帧",
        )


class TestStatsAndAck(unittest.TestCase):
    """统计与应答处理。"""

    def test_ack_ok_counted(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)] * 5)
        link = FakeLink(response=Response.OK)
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        for _ in range(5):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        self.assertEqual(pipeline.stats.sent, 5)
        self.assertEqual(pipeline.stats.ack_ok, 5)
        self.assertEqual(pipeline.stats.ack_error, 0)
        self.assertAlmostEqual(pipeline.stats.ack_rate, 1.0)

    def test_ack_error_counted(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)] * 3)
        link = FakeLink(response=Response.ERROR)
        pipeline = ColorTrackingPipeline(config, capture, link)
        pipeline.open()
        for _ in range(3):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        self.assertEqual(pipeline.stats.ack_error, 3)
        self.assertEqual(pipeline.stats.ack_ok, 0)

    def test_dry_run_sends_nothing(self) -> None:
        """dry-run 模式下不应向串口写任何字节。"""
        config = make_config()
        capture = FakeCapture([(320, 240)] * 3)
        link = FakeLink()
        pipeline = ColorTrackingPipeline(config, capture, link, dry_run=True)
        pipeline.open()
        for _ in range(3):
            frame = capture.read()
            assert frame is not None
            pipeline.step(frame)
        pipeline.close()

        self.assertEqual(link.write_calls, 0)
        self.assertEqual(pipeline.stats.sent, 0)

    def test_stats_report_renders(self) -> None:
        stats = Stats(frames=100, detections=80, sent=80, ack_ok=78,
                      ack_error=2, started_at=0.0, finished_at=10.0)
        report = stats.report()
        self.assertIn("运行统计", report)
        # 100 帧 / 10 秒 = 10 fps
        self.assertIn("10.0 fps", report)
        self.assertIn("80.0%", report)


class TestRender(unittest.TestCase):
    """预览渲染。"""

    def test_render_produces_image(self) -> None:
        config = make_config()
        capture = FakeCapture([(320, 240)])
        pipeline = ColorTrackingPipeline(config, capture, FakeLink())
        pipeline.open()
        frame = capture.read()
        assert frame is not None
        result = pipeline.step(frame)
        canvas = pipeline.render(frame, result)
        pipeline.close()

        self.assertEqual(canvas.shape, frame.image.shape)

    def test_render_without_detection(self) -> None:
        config = make_config()
        capture = FakeCapture([None])
        pipeline = ColorTrackingPipeline(config, capture, FakeLink())
        pipeline.open()
        frame = capture.read()
        assert frame is not None
        result = pipeline.step(frame)
        canvas = pipeline.render(frame, result)
        pipeline.close()

        self.assertEqual(canvas.shape, frame.image.shape)


class TestConfigIntegration(unittest.TestCase):
    """配置与流水线的联动。"""

    def test_fixed_y_respected(self) -> None:
        config = make_config(**{"mapping.fixed_y": 292})
        _, link = _run_with_config(config, (320, 100))
        parsed = parse_frame(link.last_frame)
        self.assertEqual(parsed.y, 292)

    def test_invert_x_respected(self) -> None:
        normal_cfg = make_config()
        flipped_cfg = make_config(**{"mapping.invert_x": True})

        _, link_normal = _run_with_config(normal_cfg, (100, 240))
        _, link_flipped = _run_with_config(flipped_cfg, (100, 240))

        x_normal = parse_frame(link_normal.last_frame).x
        x_flipped = parse_frame(link_flipped.last_frame).x
        self.assertAlmostEqual(x_normal + x_flipped,
                               CENTER_X_MIN + CENTER_X_MAX, delta=2)

    def test_roi_respected(self) -> None:
        """ROI 设为右半屏时，色块坐标应按 ROI 重新归一化。

        方块完整落在 ROI 内，因此图像矩质心等于绘制中心，可以直接用
        ROI 版线性插值推算期望值。
        """
        roi_x, roi_w = 320, 320
        cam_x = 360
        config = make_config(**{
            "mapping.roi_x": roi_x,
            "mapping.roi_w": roi_w,
        })
        _, link = _run_with_config(config, (cam_x, 240))

        nx = (cam_x - roi_x) / roi_w
        expected = int(round(CENTER_X_MIN + nx * (CENTER_X_MAX - CENTER_X_MIN)))
        parsed = parse_frame(link.last_frame)
        self.assertAlmostEqual(parsed.x, expected, delta=2)
        # 同一色块在"整幅画面"ROI 下映射结果应明显不同 —— 证明确实生效了
        _, link_full = _run_with_config(make_config(), (cam_x, 240))
        self.assertNotEqual(parse_frame(link_full.last_frame).x, parsed.x)

    def test_validate_catches_bad_config(self) -> None:
        config = make_config()
        config.mapping.fixed_y = 9999
        problems = config.validate()
        self.assertTrue(any("fixed_y" in p for p in problems))

    def test_validate_clean_config(self) -> None:
        config = make_config()
        self.assertEqual(config.validate(), [])

    def test_config_yaml_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path

        config = make_config(**{"detector.min_area": 1234})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            config.save(path)
            loaded = AppConfig.load(path)
        self.assertEqual(loaded.detector.min_area, 1234)
        self.assertEqual(loaded.camera.width, config.camera.width)


def _run_with_config(config: AppConfig, block_center):
    capture = FakeCapture([block_center])
    link = FakeLink()
    pipeline = ColorTrackingPipeline(config, capture, link)
    pipeline.open()
    frame = capture.read()
    assert frame is not None
    result = pipeline.step(frame)
    pipeline.close()
    return result, link


if __name__ == "__main__":
    unittest.main(verbosity=2)
