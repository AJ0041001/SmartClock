"""验证脚本的端到端测试 —— 用假 RKNN 完整跑一遍 main()。

为什么要测一个"脚本"
--------------------
verify_model.py 里有大量分支（有照片/无照片、有 Inf/无 Inf、
不同框格式…），靠人工点测很容易漏。

真实事故：数值检查分支里写错了一个变量名（`conf` 应为 `args.conf`），
**前面的步骤全部正常通过**，直到跑进那个分支才抛 NameError。
用户看到的是"跑到一半崩了"，而且崩在一个和问题无关的地方。

用假 RKNN 完整跑一遍，这类错误在 CI 阶段就会暴露。
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

from src.rknn_ctypes import TensorInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = PROJECT_ROOT / "scripts" / "verify_model.py"
_REAL_MODEL = PROJECT_ROOT.parent / "lbm" / "model" / "best.rknn"


def load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_model_under_test",
                                                  _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRKNNLite:
    """满足 verify_model.py 全部调用点的假运行时。"""

    NPU_CORE_0_1_2 = 7
    NPU_CORE_AUTO = 0

    #: 脚本可通过它编排输出
    scripted_output: np.ndarray | None = None

    def __init__(self, verbose: bool = True, lib_path=None) -> None:
        self.released = False

    def load_rknn(self, path) -> bool:
        return True

    def init_runtime(self, core_mask: int = 0, flag: int = 0) -> bool:
        return True

    def query_sdk_version(self):
        return "2.3.2 (fake)", "0.9.8 (fake)"

    def query_io_num(self):
        return 1, 1

    def get_input_attrs(self):
        return [TensorInfo(
            index=0, name="images", dims=[1, 640, 640, 3],
            fmt="NHWC", dtype="float16", n_elems=1228800, size=1228800,
        )]

    def get_output_attrs(self):
        return [TensorInfo(
            index=0, name="output0", dims=[1, 5, 8400],
            fmt="UNDEFINED", dtype="float16", n_elems=42000, size=42000,
        )]

    def inference(self, inputs, data_format="nhwc",
                  inputs_pass_through=None, **_):
        if FakeRKNNLite.scripted_output is None:
            return [np.zeros((1, 5, 8400), dtype=np.float32)]
        return [FakeRKNNLite.scripted_output]

    def release(self) -> None:
        self.released = True


def script_boxes(entries) -> np.ndarray:
    out = np.zeros((1, 5, 8400), dtype=np.float32)
    for index, (box, score) in enumerate(entries):
        out[0, :4, index] = box
        out[0, 4, index] = score
    return out


@unittest.skipUnless(_REAL_MODEL.exists(), "仓库里没有 best.rknn")
class TestVerifyModelScript(unittest.TestCase):
    """完整跑通脚本的各条分支。"""

    def setUp(self) -> None:
        self.module = load_verify_module()
        FakeRKNNLite.scripted_output = None
        self._tmp = tempfile.TemporaryDirectory()
        self.save_dir = self._tmp.name

        self._patches = [
            mock.patch.object(self.module, "RKNNLite", FakeRKNNLite),
            mock.patch.object(self.module, "find_librknnrt",
                              return_value="/fake/librknnrt.so"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()

    def run_script(self, *extra_args: str) -> tuple[int, str]:
        """跑一次 main()，返回 (退出码, 标准输出)。"""
        argv = [
            "verify_model.py",
            "--model", str(_REAL_MODEL),
            "--save-dir", self.save_dir,
            *extra_args,
        ]
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with redirect_stdout(buffer):
                code = self.module.main()
        return code, buffer.getvalue()

    # ── 正常路径 ────────────────────────────────────────────────────

    def test_synthetic_run_succeeds(self) -> None:
        """默认（合成图）路径必须跑完且不崩。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((322.8, 318.5, 165.6, 159.2), 0.88)]
        )
        code, output = self.run_script("--dump-raw")

        self.assertIn("步骤 5", output)
        self.assertIn("推理耗时", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("NameError", output)
        self.assertEqual(code, 0, f"脚本应正常退出：\n{output}")

    def test_dump_raw_branch(self) -> None:
        """--dump-raw 会走额外的统计分支（就是出过 NameError 的那条）。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((322.8, 318.5, 165.6, 159.2), 0.88)]
        )
        code, output = self.run_script("--dump-raw")
        self.assertIn("原始输出统计", output)
        self.assertIn("置信度最高的 5 个候选", output)
        self.assertEqual(code, 0, output)

    def test_low_confidence_inf_only_warns(self) -> None:
        """低分候选里的 Inf 应该只给警告，不该判失败。"""
        FakeRKNNLite.scripted_output = script_boxes([
            ((320.0, 240.0, 100.0, 80.0), 0.9),
            ((1e5, 1e5, np.inf, 1.0), 0.01),      # 低分噪声
        ])
        code, output = self.run_script("--dump-raw")
        self.assertIn("非有限值", output)
        # 不该出现"大面积"或"高置信度候选里含非有限值"这两种失败
        self.assertNotIn("高置信度候选里含非有限值", output)
        self.assertEqual(code, 0, output)

    def test_all_nan_fails_loudly(self) -> None:
        """全 NaN（输入模式错误）必须判失败并给出可操作提示。"""
        FakeRKNNLite.scripted_output = np.full(
            (1, 5, 8400), np.nan, dtype=np.float32
        )
        code, output = self.run_script("--dump-raw")
        self.assertNotEqual(code, 0, "全 NaN 应判失败")
        self.assertIn("pass_through", output)

    def test_no_detection_path(self) -> None:
        """没有高分候选（都低于阈值）也要能正常走完。

        注意不能用全零张量模拟：真实推理输出不会全零，
        脚本把"全零"判为异常信号是合理的。这里用一个
        低于阈值的真实候选，更贴近实际。
        """
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 320.0, 100.0, 100.0), 0.11)]
        )
        code, output = self.run_script()
        self.assertNotIn("Traceback", output)
        self.assertIn("合成图未检出目标", output)
        self.assertEqual(code, 0, output)

    # ── 各命令行开关 ────────────────────────────────────────────────

    def test_env_only_skips_hardware(self) -> None:
        code, output = self.run_script("--env-only")
        self.assertIn("环境诊断", output)
        self.assertNotIn("步骤 3", output)
        self.assertEqual(code, 0, output)

    def test_conf_override(self) -> None:
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 320.0, 100.0, 100.0), 0.55)]
        )
        # 阈值 0.9 时不该检出
        code, output = self.run_script("--conf", "0.9")
        self.assertNotIn("Traceback", output)
        self.assertEqual(code, 0, output)

    def test_box_format_forced_xyxy(self) -> None:
        """强制错误格式时应能跑完（只是检不出东西），不能崩。"""
        FakeRKNNLite.scripted_output = script_boxes(
            [((322.8, 318.5, 165.6, 159.2), 0.88)]
        )
        code, output = self.run_script("--box-format", "xyxy")
        self.assertNotIn("Traceback", output)
        self.assertEqual(code, 0, output)

    def test_pass_through_one_reproduces_nan(self) -> None:
        """--pass-through 1 是保留的复现开关，应能正常执行。"""
        FakeRKNNLite.scripted_output = script_boxes([])
        code, output = self.run_script("--pass-through", "1")
        self.assertNotIn("Traceback", output)
        self.assertIn("pass_through=1", output)

    # ── 真实照片路径 ────────────────────────────────────────────────

    def test_with_image_file(self) -> None:
        """走 --image 分支，并验证会生成标注图。"""
        import cv2

        image_path = Path(self.save_dir) / "fake_card.jpg"
        image = np.full((480, 640, 3), 30, dtype=np.uint8)
        cv2.rectangle(image, (240, 160), (400, 320), (0, 0, 200), -1)
        cv2.imwrite(str(image_path), image)

        # 模型输出一个位于该方块中心的框（letterbox 后 y 要加 80）
        FakeRKNNLite.scripted_output = script_boxes(
            [((320.0, 320.0, 160.0, 160.0), 0.95)]
        )
        code, output = self.run_script("--image", str(image_path),
                                       "--dump-raw")
        self.assertNotIn("Traceback", output)
        self.assertIn("检测结果：1 个", output)
        self.assertEqual(code, 0, output)

    def test_missing_image_warns_not_crashes(self) -> None:
        code, output = self.run_script("--image", "/nonexistent/card.jpg")
        self.assertNotIn("Traceback", output)
        self.assertIn("不存在", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
