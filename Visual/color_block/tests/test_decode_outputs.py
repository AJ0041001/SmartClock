"""YOLO 预处理/解码测试 —— 锁定 cxcywh 这个隐蔽的坑。

背景
----
ultralytics 导出 ONNX 的网络原始输出是 **cxcywh**（中心x, 中心y, 宽, 高），
不是 xyxy。仓库 README 写成 xyxy 是错的，据此写的推理脚本会画出完全
错位的框，而且**程序不报错**，只是结果不对 —— 这种 bug 最难发现。

这些测试直接针对规范实现 :mod:`src.yolo`（验证工具与正式检测器共用同一份），
用已知真值的合成张量锁住行为。
"""

from __future__ import annotations

import unittest

import numpy as np

from src.yolo import (
    DetectedBox,
    LetterboxInfo,
    decode_predictions,
    draw_boxes,
    letterbox,
    resolve_box_format,
)


# ── 便捷适配：把规范 API 包成测试里好用的形状 ──────────────────────


def lb(image: np.ndarray, size: int = 640):
    """返回 (canvas, scale, top, left)，便于断言几何参数。"""
    canvas, info = letterbox(image, size)
    return canvas, info.scale, info.top, info.left


def decode(output, conf, nms, scale, top, left, orig_shape,
           box_format="auto"):
    """返回 [(bbox, score), ...]，便于断言。"""
    boxes = decode_predictions(
        output,
        conf_threshold=conf,
        nms_threshold=nms,
        info=LetterboxInfo(scale=scale, top=top, left=left, input_size=640),
        orig_shape=orig_shape,
        box_format=box_format,
    )
    return [((b.x1, b.y1, b.x2, b.y2), b.score) for b in boxes]


def make_output(candidates) -> np.ndarray:
    """构造 (1, 5, 8400) 的模拟输出。"""
    out = np.zeros((1, 5, 8400), dtype=np.float32)
    for index, (box, score) in enumerate(candidates):
        out[0, :4, index] = box
        out[0, 4, index] = score
    return out


class TestLetterbox(unittest.TestCase):
    """letterbox 预处理（等比缩放 + 灰边，不拉伸变形）。"""

    def test_wide_image(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        canvas, scale, top, left = lb(image, 640)
        self.assertEqual(canvas.shape, (640, 640, 3))
        self.assertAlmostEqual(scale, 1.0)
        self.assertEqual(left, 0)
        self.assertEqual(top, 80)          # (640-480)/2

    def test_tall_image(self) -> None:
        image = np.zeros((800, 400, 3), dtype=np.uint8)
        canvas, scale, top, left = lb(image, 640)
        self.assertEqual(canvas.shape, (640, 640, 3))
        self.assertAlmostEqual(scale, 0.8)
        self.assertGreater(left, 0)

    def test_square_image_no_padding(self) -> None:
        image = np.zeros((640, 640, 3), dtype=np.uint8)
        canvas, scale, top, left = lb(image, 640)
        self.assertEqual((top, left), (0, 0))

    def test_pad_value_is_114(self) -> None:
        """填充值必须是 114，与训练时一致，否则边缘会有分布偏移。"""
        image = np.zeros((320, 640, 3), dtype=np.uint8)
        canvas, _, top, _ = lb(image, 640)
        self.assertEqual(int(canvas[0, 0, 0]), 114)
        self.assertEqual(int(canvas[-1, -1, 0]), 114)

    def test_aspect_ratio_preserved(self) -> None:
        """长宽比不能变 —— 变形会让框的宽高比失真。"""
        image = np.zeros((300, 600, 3), dtype=np.uint8)
        canvas, scale, _, _ = lb(image, 640)
        # 600x300 缩放后应是 640x320
        self.assertAlmostEqual(scale, 640 / 600)
        non_pad = int(round(300 * scale))
        self.assertAlmostEqual(non_pad, 320, delta=1)


class TestResolveBoxFormat(unittest.TestCase):
    """框格式判定。

    ⚠️ 这里曾经用过「x2>=x1 且 y2>=y1 → xyxy」的启发式判别，
    在真实数据上造成了 32/32 全错的后果。现在 auto 一律返回 xywh。
    下面第一个用例就是那个 bug 的回归测试。
    """

    def test_auto_never_guesses_xyxy(self) -> None:
        """回归测试：目标位于画面左侧时，auto 不能被误判成 xyxy。

        原始案例（img_003）：
            真值卡片在画面右下，模型输出的 cxcywh 约为 (48, 147, 411, 319)
            即 w(411) > cx(48) 且 h(319) > cy(147)。

        旧的判别逻辑看到「x2>=x1 且 y2>=y1」成立，就误判为 xyxy，
        于是把 (cx,cy,w,h) 当成 (x1,y1,x2,y2) 用，框彻底错位。

        正确行为：auto 返回 xywh。
        """
        # 这是"目标偏左上方"的典型取值
        left_side_card = np.array([[48.0, 147.0, 411.0, 319.0]] * 5)
        self.assertEqual(
            resolve_box_format(left_side_card, "auto"), "xywh",
            "auto 绝不能凭 x2>=x1 猜成 xyxy —— 那会让偏左的检测全部错位",
        )

    def test_auto_on_centered_card(self) -> None:
        boxes = np.array([[320.0, 240.0, 100.0, 80.0]] * 5)
        self.assertEqual(resolve_box_format(boxes, "auto"), "xywh")

    def test_auto_on_xyxy_looking_values(self) -> None:
        """即使数值"看起来像" xyxy，auto 也不该改判。

        因为输出形状 (1, 4+nc, N) 已经确定是无 NMS 的 ultralytics 输出，
        格式必然是 cxcywh —— 没有猜测的余地。
        """
        looks_like_xyxy = np.array([[100.0, 100.0, 300.0, 300.0]] * 5)
        self.assertEqual(resolve_box_format(looks_like_xyxy, "auto"), "xywh")

    def test_explicit_overrides(self) -> None:
        boxes = np.array([[320.0, 240.0, 100.0, 80.0]])
        self.assertEqual(resolve_box_format(boxes, "xyxy"), "xyxy")
        self.assertEqual(resolve_box_format(boxes, "xywh"), "xywh")

    def test_empty_defaults_to_xywh(self) -> None:
        self.assertEqual(resolve_box_format(np.zeros((0, 4)), "auto"), "xywh")


class TestLeftSideCardRegression(unittest.TestCase):
    """端到端回归：偏左的卡片必须算出正确的框。

    这是真实数据集上暴露的问题，用完整解码路径再验证一次。
    """

    def test_left_side_card_box_position(self) -> None:
        # cx=48, cy=147, w=411, h=319（letterbox 坐标）
        out = make_output([((48.0, 147.0, 411.0, 319.0), 0.9)])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (640, 640))

        self.assertEqual(len(dets), 1)
        (x1, y1, x2, y2), _ = dets[0]

        # 按 cxcywh 正确解读：box = (48-205.5, 147-159.5)-(48+205.5, 147+159.5)
        # = (-157.5, -12.5)-(253.5, 306.5)，裁剪到画面内
        self.assertLess(x1, 50, "x1 应在画面左边缘附近")
        self.assertLessEqual(y1, 5, "y1 应被裁剪到画面顶部")
        self.assertAlmostEqual(x2, 253, delta=3)
        self.assertAlmostEqual(y2, 306, delta=3)

        # 反证：若误判为 xyxy，框会变成 (48,147)-(411,319)，宽高完全不同
        self.assertNotAlmostEqual(x2 - x1, 363, delta=20,
                                  msg="宽 363 说明又被误判成 xyxy 了")

    def test_both_sides_consistent(self) -> None:
        """左侧和右侧的卡片都必须被正确解读 —— 不能"换个位置就坏"。"""
        cases = [
            ((48.0, 147.0, 411.0, 319.0), "左侧卡片"),
            ((580.0, 300.0, 80.0, 120.0), "右侧卡片"),
            ((320.0, 320.0, 160.0, 160.0), "中央卡片"),
        ]
        for raw, label in cases:
            with self.subTest(case=label):
                out = make_output([(raw, 0.9)])
                dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (640, 640))
                self.assertEqual(len(dets), 1, f"{label} 应被检出")

                cx, cy, w, h = raw
                (x1, y1, x2, y2), _ = dets[0]
                # 解码后的中心必须与输入的 cx,cy 一致（考虑裁剪前）
                self.assertAlmostEqual(
                    (max(0, x1) + min(639, x2)) / 2,
                    min(639, max(0, cx)), delta=max(5, w / 2),
                    msg=f"{label} 的中心对不上",
                )


class TestCxcywhDecoding(unittest.TestCase):
    """核心：cxcywh → xyxy 的换算必须正确。

    真值来源：本模型在合成图上输出的 (322.8, 318.5, 165.6, 159.2)，
    对应画面里中心 (320,320)、尺寸 160x160 的方块。
    """

    def test_cxcywh_to_xyxy(self) -> None:
        out = make_output([((322.8, 318.5, 165.6, 159.2), 0.88)])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (640, 640))

        self.assertEqual(len(dets), 1)
        (x1, y1, x2, y2), score = dets[0]
        self.assertAlmostEqual((x1 + x2) / 2, 322.8, delta=2)
        self.assertAlmostEqual((y1 + y2) / 2, 318.5, delta=2)
        self.assertAlmostEqual(x2 - x1, 165.6, delta=2)
        self.assertAlmostEqual(y2 - y1, 159.2, delta=2)
        self.assertAlmostEqual(score, 0.88, places=2)

    def test_letterbox_restored(self) -> None:
        """带 letterbox 边距时坐标要正确还原回原图。"""
        out = make_output([((322.8, 318.5, 165.6, 159.2), 0.88)])
        # 模拟 640x480 图缩放到 640x640：scale=1, top=80, left=0
        dets = decode(out, 0.5, 0.45, 1.0, 80, 0, (480, 640))
        self.assertEqual(len(dets), 1)
        (x1, y1, x2, y2), _ = dets[0]
        self.assertAlmostEqual((y1 + y2) / 2, 238.5, delta=2)
        self.assertAlmostEqual((x1 + x2) / 2, 322.8, delta=2)

    def test_scaled_restored(self) -> None:
        """带缩放时坐标也要还原（scale != 1）。"""
        out = make_output([((320.0, 320.0, 160.0, 160.0), 0.9)])
        dets = decode(out, 0.5, 0.45, 0.5, 0, 0, (960, 1280))
        (x1, y1, x2, y2), _ = dets[0]
        self.assertAlmostEqual((x1 + x2) / 2, 640, delta=3)
        self.assertAlmostEqual((y1 + y2) / 2, 640, delta=3)
        self.assertAlmostEqual(x2 - x1, 320, delta=3)

    def test_xyxy_interpretation_would_be_wrong(self) -> None:
        """反证：同一组数据按 xyxy 解读会得到非法坐标而被丢弃。"""
        out = make_output([((322.8, 318.5, 165.6, 159.2), 0.88)])
        auto = decode(out, 0.5, 0.45, 1.0, 0, 0, (640, 640))
        forced = decode(out, 0.5, 0.45, 1.0, 0, 0, (640, 640),
                        box_format="xyxy")
        self.assertEqual(len(auto), 1, "auto 应正确识别为 cxcywh")
        self.assertEqual(len(forced), 0, "强制 xyxy 应因坐标非法而丢弃")


class TestDetectedBox(unittest.TestCase):
    """DetectedBox 的中心与面积计算 —— 中心就是要回传的值。"""

    def test_center(self) -> None:
        box = DetectedBox(100, 200, 300, 400, 0.9)
        self.assertEqual(box.center, (200.0, 300.0))

    def test_dimensions(self) -> None:
        box = DetectedBox(100, 200, 300, 400, 0.9)
        self.assertEqual(box.width, 200)
        self.assertEqual(box.height, 200)
        self.assertEqual(box.area, 40000.0)

    def test_odd_size_center(self) -> None:
        """奇数尺寸时中心会落在半像素上，不能取整丢失精度。"""
        box = DetectedBox(10, 10, 21, 31, 0.9)
        self.assertEqual(box.center, (15.5, 20.5))


class TestNonFiniteHandling(unittest.TestCase):
    """非有限值的处理：低分噪声应被剔除，不影响真实检测。"""

    def test_inf_in_low_confidence_dropped(self) -> None:
        out = make_output([
            ((320.0, 240.0, 100.0, 80.0), 0.9),
            ((1e5, 1e5, np.inf, 1.0), 0.01),
        ])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640))
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0][1], 0.9, places=2)

    def test_nan_in_high_confidence_dropped(self) -> None:
        out = make_output([
            ((np.nan, np.nan, np.nan, np.nan), 0.99),
            ((320.0, 240.0, 100.0, 80.0), 0.8),
        ])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640))
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0][1], 0.8, places=2)

    def test_all_nonfinite_returns_empty(self) -> None:
        out = make_output([((np.inf, np.inf, np.inf, np.inf), 0.99)])
        self.assertEqual(decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640)), [])


class TestConfidenceFiltering(unittest.TestCase):
    def test_below_confidence_filtered(self) -> None:
        out = make_output([((320.0, 240.0, 100.0, 80.0), 0.2)])
        self.assertEqual(decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640)), [])

    def test_empty_output(self) -> None:
        out = make_output([])
        self.assertEqual(decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640)), [])

    def test_none_like_empty_array(self) -> None:
        self.assertEqual(
            decode(np.zeros((0,), dtype=np.float32), 0.5, 0.45, 1.0, 0, 0,
                   (480, 640)),
            [],
        )


class TestNms(unittest.TestCase):
    def test_overlapping_suppressed(self) -> None:
        out = make_output([
            ((320.0, 240.0, 100.0, 80.0), 0.9),
            ((322.0, 242.0, 100.0, 80.0), 0.7),
        ])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640))
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0][1], 0.9, places=2)

    def test_separate_kept_and_sorted(self) -> None:
        out = make_output([
            ((150.0, 240.0, 80.0, 80.0), 0.6),
            ((500.0, 240.0, 80.0, 80.0), 0.9),
        ])
        dets = decode(out, 0.5, 0.45, 1.0, 0, 0, (480, 640))
        self.assertEqual(len(dets), 2)
        self.assertGreater(dets[0][1], dets[1][1], "应按置信度降序")


class TestDrawing(unittest.TestCase):
    def test_draw_does_not_mutate_input(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        original = image.copy()
        canvas = draw_boxes(image, [DetectedBox(100, 100, 300, 300, 0.9)])
        self.assertEqual(canvas.shape, image.shape)
        np.testing.assert_array_equal(image, original)

    def test_draw_empty_list(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        canvas = draw_boxes(image, [])
        self.assertEqual(canvas.shape, image.shape)


if __name__ == "__main__":
    unittest.main(verbosity=2)
