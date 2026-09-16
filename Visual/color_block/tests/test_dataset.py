"""数据集模块测试 —— 标签解析与评价指标。

真值解析错了，整个评估就没有意义。这些测试用已知的标签内容
锁住解析行为，特别是**多边形 → 外接矩形**这个转换（本项目数据集
用的就是多边形标注）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from src.dataset import (
    GroundTruth,
    Sample,
    iou,
    load_split,
    match_predictions,
    parse_ground_truths,
    parse_label_file,
)


class TestParseLabelFile(unittest.TestCase):
    """标签文件解析 —— 两种格式都要支持。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, content: str) -> Path:
        path = self.dir / "label.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_bbox_format(self) -> None:
        """标准检测框格式：class cx cy w h（归一化）。"""
        path = self._write("0 0.5 0.5 0.2 0.4\n")
        boxes = parse_label_file(path)
        self.assertEqual(len(boxes), 1)
        cid, x1, y1, x2, y2 = boxes[0]
        self.assertEqual(cid, 0)
        self.assertAlmostEqual(x1, 0.4)     # 0.5 - 0.2/2
        self.assertAlmostEqual(y1, 0.3)     # 0.5 - 0.4/2
        self.assertAlmostEqual(x2, 0.6)
        self.assertAlmostEqual(y2, 0.7)

    def test_polygon_format(self) -> None:
        """多边形格式：class x1 y1 x2 y2 ... → 外接矩形。"""
        # 一个三角形：(0.2,0.2) (0.8,0.3) (0.5,0.9)
        path = self._write("0 0.2 0.2 0.8 0.3 0.5 0.9\n")
        boxes = parse_label_file(path)
        self.assertEqual(len(boxes), 1)
        cid, x1, y1, x2, y2 = boxes[0]
        self.assertAlmostEqual(x1, 0.2)
        self.assertAlmostEqual(y1, 0.2)
        self.assertAlmostEqual(x2, 0.8)
        self.assertAlmostEqual(y2, 0.9)

    def test_real_dataset_polygon(self) -> None:
        """用项目里真实的标签内容验证（从 redcard.yolo26 摘录）。"""
        line = ("0 0.5390625 0.2625 0.5390625 0.275 0.5875 0.29791666666 "
                "0.5984375 0.29583333 0.6046875 0.28125 0.646875 0.1125 "
                "0.6171875 0.10625 0.6015625 0.06666 0.5875 0.06666 "
                "0.58125 0.077083\n")
        path = self._write(line)
        boxes = parse_label_file(path)
        self.assertEqual(len(boxes), 1)
        _cid, x1, y1, x2, y2 = boxes[0]
        self.assertAlmostEqual(x1, 0.5390625, places=5)
        self.assertAlmostEqual(y1, 0.06666, places=4)
        self.assertAlmostEqual(x2, 0.646875, places=5)
        self.assertAlmostEqual(y2, 0.297916, places=4)

    def test_multiple_objects(self) -> None:
        path = self._write("0 0.5 0.5 0.2 0.2\n0 0.2 0.2 0.1 0.1\n")
        self.assertEqual(len(parse_label_file(path)), 2)

    def test_empty_file(self) -> None:
        path = self._write("")
        self.assertEqual(parse_label_file(path), [])

    def test_missing_file(self) -> None:
        self.assertEqual(parse_label_file(self.dir / "nope.txt"), [])

    def test_malformed_lines_skipped(self) -> None:
        """格式不对的行应跳过，而不是让整个解析崩掉。"""
        path = self._write(
            "0 0.5 0.5 0.2 0.2\n"      # 正常
            "garbage\n"                 # 无法解析
            "0 1 2\n"                   # 字段太少
            "0 0.3 0.3 0.1 0.1\n"      # 正常
        )
        self.assertEqual(len(parse_label_file(path)), 2)

    def test_class_id_preserved(self) -> None:
        path = self._write("3 0.5 0.5 0.2 0.2\n")
        self.assertEqual(parse_label_file(path)[0][0], 3)


class TestParseGroundTruths(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pixel_conversion(self) -> None:
        path = self.dir / "l.txt"
        path.write_text("0 0.5 0.5 0.5 0.5\n", encoding="utf-8")
        boxes = parse_ground_truths(path, 640, 480)
        self.assertEqual(len(boxes), 1)
        box = boxes[0]
        self.assertAlmostEqual(box.x1, 160)   # 0.25 × 640
        self.assertAlmostEqual(box.y1, 120)   # 0.25 × 480
        self.assertAlmostEqual(box.x2, 480)
        self.assertAlmostEqual(box.y2, 360)

    def test_center_and_size(self) -> None:
        gt = GroundTruth(class_id=0, x1=100, y1=200, x2=300, y2=400)
        self.assertEqual(gt.center, (200.0, 300.0))
        self.assertEqual(gt.width, 200)
        self.assertEqual(gt.height, 200)
        self.assertEqual(gt.area, 40000)


class TestIou(unittest.TestCase):
    def test_identical_boxes(self) -> None:
        self.assertAlmostEqual(iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)

    def test_disjoint_boxes(self) -> None:
        self.assertAlmostEqual(iou((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)

    def test_half_overlap(self) -> None:
        # 交集 5x10=50，并集 100+100-50=150 → 1/3
        self.assertAlmostEqual(iou((0, 0, 10, 10), (5, 0, 15, 10)), 1 / 3)

    def test_contained_box(self) -> None:
        """小框完全落在大框内时：

        大框 10x10 = 100，小框 (2,2)-(8,8) 即 6x6 = 36
        交集 = 36（小框全部在内）
        并集 = 100 + 36 - 36 = 100
        IoU  = 36/100 = 0.36
        """
        self.assertAlmostEqual(iou((0, 0, 10, 10), (2, 2, 8, 8)), 0.36)

    def test_degenerate_box(self) -> None:
        """零面积框不该导致除零。"""
        self.assertEqual(iou((0, 0, 0, 0), (0, 0, 10, 10)), 0.0)

    def test_symmetric(self) -> None:
        a = (10, 20, 100, 200)
        b = (50, 60, 150, 250)
        self.assertAlmostEqual(iou(a, b), iou(b, a))


class TestMatchPredictions(unittest.TestCase):
    """贪心匹配逻辑。"""

    def _gt(self, box) -> GroundTruth:
        return GroundTruth(class_id=0, x1=box[0], y1=box[1],
                           x2=box[2], y2=box[3])

    def test_perfect_match(self) -> None:
        preds = [((10, 10, 50, 50), 0.9)]
        gts = [self._gt((10, 10, 50, 50))]
        matched = match_predictions(preds, gts, 0.5)
        self.assertEqual(len(matched), 1)
        self.assertAlmostEqual(matched[0], 1.0)

    def test_below_threshold_not_matched(self) -> None:
        preds = [((100, 100, 150, 150), 0.9)]
        gts = [self._gt((10, 10, 50, 50))]
        self.assertEqual(match_predictions(preds, gts, 0.5), [])

    def test_extra_prediction_is_false_positive(self) -> None:
        preds = [
            ((10, 10, 50, 50), 0.9),      # 命中
            ((200, 200, 250, 250), 0.8),  # 误检
        ]
        gts = [self._gt((10, 10, 50, 50))]
        matched = match_predictions(preds, gts, 0.5)
        self.assertEqual(len(matched), 1)

    def test_missing_prediction_is_false_negative(self) -> None:
        preds = [((10, 10, 50, 50), 0.9)]
        gts = [self._gt((10, 10, 50, 50)), self._gt((200, 200, 260, 260))]
        matched = match_predictions(preds, gts, 0.5)
        self.assertEqual(len(matched), 1)   # 第二个没被匹配

    def test_each_gt_matched_once(self) -> None:
        """两个预测都落在同一个真值上时，只能算一次命中。"""
        preds = [
            ((10, 10, 50, 50), 0.9),
            ((11, 11, 51, 51), 0.8),
        ]
        gts = [self._gt((10, 10, 50, 50))]
        matched = match_predictions(preds, gts, 0.5)
        self.assertEqual(len(matched), 1, "一个真值不该被匹配两次")

    def test_higher_confidence_wins(self) -> None:
        """贪心匹配按置信度降序，高分预测应优先占用真值。"""
        preds = [
            ((10, 10, 50, 50), 0.95),   # 与 GT 完美重合
            ((12, 12, 52, 52), 0.60),
        ]
        gts = [self._gt((10, 10, 50, 50))]
        matched = match_predictions(preds, gts, 0.5)
        self.assertEqual(len(matched), 1)
        self.assertGreater(matched[0], 0.9)

    def test_empty_inputs(self) -> None:
        self.assertEqual(match_predictions([], [], 0.5), [])
        self.assertEqual(
            match_predictions([((0, 0, 10, 10), 0.9)], [], 0.5), []
        )


class TestLoadSplit(unittest.TestCase):
    """从磁盘加载数据集划分。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for split in ("train", "valid", "test"):
            (self.root / split / "images").mkdir(parents=True)
            (self.root / split / "labels").mkdir(parents=True)

        # 造 3 张测试图：两张有标注，一张没有
        for name, with_label in (("a", True), ("b", True), ("c", False)):
            image = np.full((480, 640, 3), 30, dtype=np.uint8)
            cv2.rectangle(image, (240, 160), (400, 320), (0, 0, 200), -1)
            cv2.imwrite(str(self.root / "test" / "images" / f"{name}.jpg"),
                        image)
            label = self.root / "test" / "labels" / f"{name}.txt"
            if with_label:
                label.write_text("0 0.5 0.5 0.25 0.25\n", encoding="utf-8")
            else:
                label.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_only_annotated(self) -> None:
        samples = load_split(self.root, "test")
        self.assertEqual(len(samples), 2)

    def test_can_include_unannotated(self) -> None:
        samples = load_split(self.root, "test", require_annotation=False)
        self.assertEqual(len(samples), 3)

    def test_limit(self) -> None:
        samples = load_split(self.root, "test", limit=1)
        self.assertEqual(len(samples), 1)

    def test_image_dimensions_recorded(self) -> None:
        samples = load_split(self.root, "test")
        self.assertEqual(samples[0].width, 640)
        self.assertEqual(samples[0].height, 480)

    def test_empty_split(self) -> None:
        self.assertEqual(load_split(self.root, "train"), [])

    def test_invalid_split_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_split(self.root, "nonexistent")

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_split(Path("/nonexistent/dataset"), "test")


class TestRealDataset(unittest.TestCase):
    """真实数据集的冒烟测试（存在才跑）。"""

    DATASET = Path(__file__).resolve().parent.parent / "redcard.yolo26"

    def setUp(self) -> None:
        if not self.DATASET.is_dir():
            self.skipTest("redcard.yolo26 数据集不存在")

    def test_test_split_loads(self) -> None:
        samples = load_split(self.DATASET, "test")
        self.assertGreater(len(samples), 0)
        self.assertTrue(all(s.has_annotation for s in samples))

    def test_all_splits_have_expected_counts(self) -> None:
        expected = {"train": 229, "valid": 63, "test": 32}
        for split, count in expected.items():
            with self.subTest(split=split):
                samples = load_split(self.DATASET, split)
                self.assertEqual(len(samples), count)

    def test_ground_truth_boxes_are_sane(self) -> None:
        """真值框必须在图像范围内，且尺寸合理。"""
        samples = load_split(self.DATASET, "test")
        for sample in samples:
            for gt in sample.boxes:
                with self.subTest(image=sample.stem):
                    self.assertGreaterEqual(gt.x1, -1)
                    self.assertGreaterEqual(gt.y1, -1)
                    self.assertLessEqual(gt.x2, sample.width + 1)
                    self.assertLessEqual(gt.y2, sample.height + 1)
                    self.assertGreater(gt.width, 10)
                    self.assertGreater(gt.height, 10)

    def test_images_are_640x480(self) -> None:
        samples = load_split(self.DATASET, "test")
        for sample in samples:
            with self.subTest(image=sample.stem):
                self.assertEqual((sample.width, sample.height), (640, 480))


if __name__ == "__main__":
    unittest.main(verbosity=2)
