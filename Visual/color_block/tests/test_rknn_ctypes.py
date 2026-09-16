"""RKNN ctypes 绑定测试 —— 不需要 NPU 硬件就能跑的部分。

NPU 相关的功能只能在板子上验证，但下面这些是纯软件正确性，
必须在每次改动后回归：

  · 结构体布局是否仍与 C 编译器一致（指针错位会直接段错误）
  · 库查找逻辑
  · 错误码映射
  · 参数校验（在触碰硬件之前就该拦住的错误）
"""

from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from unittest import mock

from src.rknn_ctypes import (
    RKNN_ERR_CODES,
    RKNN_MAX_DIMS,
    RKNN_MAX_NAME_LEN,
    RKNN_NPU_CORE_0_1_2,
    RKNN_SUCC,
    RKNNLite,
    RknnError,
    RknnInput,
    RknnInputOutputNum,
    RknnOutput,
    RknnSdkVersion,
    RknnTensorAttr,
    find_librknnrt,
    selftest_diagnostics,
)


class TestStructLayout(unittest.TestCase):
    """结构体尺寸与字段偏移必须与 C ABI 一致。

    这些期望值是在 aarch64（LP64）上按 C 对齐规则手工推算的：
      · 指针 8 字节对齐
      · int32 4 字节对齐
      · 结构体总大小按最大成员对齐

    一旦有人"顺手"调整了字段顺序或类型，这里会立刻红。
    """

    def test_pointer_size_is_8(self) -> None:
        """整个推算都建立在 64 位平台之上，先确认前提成立。"""
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)

    def test_rknn_input_size(self) -> None:
        # index(4) pad(4) buf(8) size(4) pass_through(1) pad(3) type(4) fmt(4)
        self.assertEqual(ctypes.sizeof(RknnInput), 32)

    def test_rknn_output_size(self) -> None:
        # want_float(1) is_prealloc(1) pad(2) index(4) buf(8) size(4) pad(4)
        self.assertEqual(ctypes.sizeof(RknnOutput), 24)

    def test_rknn_tensor_attr_size(self) -> None:
        # index4 n_dims4 dims64 name256 n_elems4 size4 fmt4 type4 qnt4
        # fl1 pad3 zp4 scale4 w_stride4 size_with_stride4 pass1 pad3 h_stride4
        self.assertEqual(ctypes.sizeof(RknnTensorAttr), 376)

    def test_input_output_num_size(self) -> None:
        self.assertEqual(ctypes.sizeof(RknnInputOutputNum), 8)

    def test_sdk_version_size(self) -> None:
        self.assertEqual(ctypes.sizeof(RknnSdkVersion), 512)

    def test_input_field_offsets(self) -> None:
        """逐个字段核对偏移 —— 只比对总大小会漏掉"两个字段互相抵消"的错误。"""
        self.assertEqual(RknnInput.index.offset, 0)
        self.assertEqual(RknnInput.buf.offset, 8)
        self.assertEqual(RknnInput.size.offset, 16)
        self.assertEqual(RknnInput.pass_through.offset, 20)
        self.assertEqual(RknnInput.type.offset, 24)
        self.assertEqual(RknnInput.fmt.offset, 28)

    def test_tensor_attr_key_offsets(self) -> None:
        self.assertEqual(RknnTensorAttr.index.offset, 0)
        self.assertEqual(RknnTensorAttr.n_dims.offset, 4)
        self.assertEqual(RknnTensorAttr.dims.offset, 8)
        self.assertEqual(RknnTensorAttr.name.offset, 8 + RKNN_MAX_DIMS * 4)
        self.assertEqual(
            RknnTensorAttr.n_elems.offset,
            8 + RKNN_MAX_DIMS * 4 + RKNN_MAX_NAME_LEN,
        )

    def test_constants(self) -> None:
        self.assertEqual(RKNN_MAX_DIMS, 16)
        self.assertEqual(RKNN_MAX_NAME_LEN, 256)


class TestLibraryLookup(unittest.TestCase):
    def test_explicit_env_path_wins(self) -> None:
        with mock.patch.dict("os.environ",
                             {"RKNN_RT_PATH": "/nonexistent/lib.so"}):
            # 路径不存在时应忽略，回退到其它候选
            result = find_librknnrt()
            self.assertNotEqual(result, "/nonexistent/lib.so")

    def test_finds_bundled_library(self) -> None:
        """项目里带着 librknnrt.so，应当能被兜底找到。"""
        result = find_librknnrt()
        self.assertIsNotNone(result, "未能找到 librknnrt.so")
        self.assertTrue(Path(result).exists(), f"找到的路径不存在：{result}")

    def test_diagnostics_render(self) -> None:
        text = selftest_diagnostics()
        self.assertIn("librknnrt.so", text)
        self.assertIn("sizeof(rknn_input)", text)


class TestErrorMapping(unittest.TestCase):
    def test_known_codes_have_names(self) -> None:
        # 注意 RKNN_SUCC(=0) 是成功码，不在错误码表里
        self.assertEqual(RKNN_SUCC, 0)
        self.assertEqual(RKNN_ERR_CODES[-1], "RKNN_ERR_FAIL")
        self.assertIn(-5, RKNN_ERR_CODES)
        self.assertNotIn(RKNN_SUCC, RKNN_ERR_CODES)

    def test_error_message_includes_code_name(self) -> None:
        err = RknnError("初始化失败", -5)
        self.assertIn("初始化失败", str(err))
        self.assertIn("RKNN_ERR_PARAM_INVALID", str(err))
        self.assertEqual(err.code, -5)

    def test_error_without_code(self) -> None:
        err = RknnError("只是消息")
        self.assertEqual(str(err), "只是消息")
        self.assertIsNone(err.code)


class TestPreflightValidation(unittest.TestCase):
    """在触碰硬件之前就该拦住的错误。

    这些用例通过 mock 掉库加载，因此不需要 NPU。
    """

    def _make_lite(self) -> RKNNLite:
        with mock.patch.object(RKNNLite, "_load_library",
                               return_value=mock.MagicMock()):
            with mock.patch.object(RKNNLite, "_bind_functions"):
                return RKNNLite(verbose=False)

    def test_inference_before_init_raises(self) -> None:
        """未初始化时应明确报"尚未初始化"，而不是在底层崩掉。"""
        lite = self._make_lite()
        with self.assertRaises(RknnError) as ctx:
            lite.inference(inputs=[])
        self.assertIn("尚未初始化", str(ctx.exception))

    def test_inference_empty_inputs_raises(self) -> None:
        """已初始化但输入为空时，应报"inputs 不能为空"。"""
        lite = self._make_lite()
        lite._initialized = True
        with self.assertRaises(RknnError) as ctx:
            lite.inference(inputs=[])
        self.assertIn("inputs 不能为空", str(ctx.exception))

    def test_inference_wrong_dtype_raises_before_hardware(self) -> None:
        """类型不对时，应在调用 rknn_inputs_set 之前就被拦下。

        query_io_num 走的是被 mock 的库，所以这里一并打桩。
        """
        import numpy as np

        lite = self._make_lite()
        lite._initialized = True
        with mock.patch.object(lite, "query_io_num", return_value=(1, 1)):
            with self.assertRaises(RknnError) as ctx:
                lite.inference(
                    inputs=[np.zeros((1, 8, 8, 3), dtype=np.float32)]
                )
        self.assertIn("uint8", str(ctx.exception))

    def test_init_runtime_before_load_raises(self) -> None:
        lite = self._make_lite()
        with self.assertRaises(RknnError) as ctx:
            lite.init_runtime()
        self.assertIn("尚未载入模型", str(ctx.exception))

    def test_query_before_init_raises(self) -> None:
        lite = self._make_lite()
        with self.assertRaises(RknnError) as ctx:
            lite.query_io_num()
        self.assertIn("尚未初始化", str(ctx.exception))

    def test_load_missing_model_raises(self) -> None:
        lite = self._make_lite()
        with self.assertRaises(RknnError) as ctx:
            lite.load_rknn("/nonexistent/model.rknn")
        self.assertIn("模型文件不存在", str(ctx.exception))

    def test_release_is_idempotent(self) -> None:
        lite = self._make_lite()
        lite.release()
        lite.release()   # 不应抛异常
        self.assertFalse(lite._initialized)


class TestPassThroughSemantics(unittest.TestCase):
    """直通模式的取值语义。

    这是个曾经踩过的坑：本模型输入是 float16 且归一化烘焙在内部，
    必须 pass_through=0。这里把"默认值"钉住，防止以后被误改。
    """

    def test_default_is_not_pass_through(self) -> None:
        import inspect

        signature = inspect.signature(RKNNLite.inference)
        self.assertIn("inputs_pass_through", signature.parameters)
        # 默认 None 表示走常规模式（等价于 0）
        self.assertIsNone(
            signature.parameters["inputs_pass_through"].default
        )

    def test_core_mask_value(self) -> None:
        """0_1_2 三核的位掩码必须是 0b111。"""
        self.assertEqual(RKNN_NPU_CORE_0_1_2, 0b111)


if __name__ == "__main__":
    unittest.main(verbosity=2)
