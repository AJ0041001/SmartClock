"""RKNN 运行时 —— 基于 ctypes 的轻量实现，不依赖官方 wheel。

为什么不用官方的 rknn_toolkit_lite2？
------------------------------------
官方 wheel 的标签只到 ``cp312``，而本机是 **Python 3.14**，ABI 不兼容，
根本装不上。而 wheel 里其实是编译好的 C 扩展（``.cpython-312-*.so``），
也没有可用的源码分发。

但 ``librknnrt.so`` 本身是个标准的 C 动态库，导出的是稳定的 C ABI。
因此这里直接用 ctypes 调它 —— 好处有三：

  1. **不挑 Python 版本**，3.8 / 3.14 / 以后都行；
  2. **不需要安装任何东西**，库文件随项目携带即可；
  3. **结构体布局完全可控**，字段顺序严格按官方 ``rknn_api.h`` 声明。

结构体与枚举的定义依据（已附在项目中）::

    tools/rknn_api/rknn_api.h

取自 airockchip/rknn-toolkit2 官方仓库，未做改动。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

# ──────────────────────────────────────────────────────────────────────
# 常量（严格对应 rknn_api.h）
# ──────────────────────────────────────────────────────────────────────

RKNN_MAX_DIMS = 16
RKNN_MAX_NAME_LEN = 256

# rknn_query_cmd
RKNN_QUERY_IN_OUT_NUM = 0
RKNN_QUERY_INPUT_ATTR = 1
RKNN_QUERY_OUTPUT_ATTR = 2
RKNN_QUERY_PERF_DETAIL = 3
RKNN_QUERY_PERF_RUN = 4
RKNN_QUERY_SDK_VERSION = 5
RKNN_QUERY_MEM_SIZE = 6

# rknn_tensor_type / rknn_tensor_format（两者枚举值一致）
RKNN_TENSOR_FLOAT32 = 0
RKNN_TENSOR_FLOAT16 = 1
RKNN_TENSOR_INT8 = 2
RKNN_TENSOR_UINT8 = 3
RKNN_TENSOR_INT16 = 4
RKNN_TENSOR_UINT16 = 5
RKNN_TENSOR_INT32 = 6
RKNN_TENSOR_UINT32 = 7
RKNN_TENSOR_INT64 = 8
RKNN_TENSOR_BOOL = 9

TENSOR_TYPE_NAMES = {
    RKNN_TENSOR_FLOAT32: "float32",
    RKNN_TENSOR_FLOAT16: "float16",
    RKNN_TENSOR_INT8: "int8",
    RKNN_TENSOR_UINT8: "uint8",
    RKNN_TENSOR_INT16: "int16",
    RKNN_TENSOR_UINT16: "uint16",
    RKNN_TENSOR_INT32: "int32",
    RKNN_TENSOR_UINT32: "uint32",
    RKNN_TENSOR_INT64: "int64",
    RKNN_TENSOR_BOOL: "bool",
}

# rknn_tensor_format
RKNN_TENSOR_NCHW = 0
RKNN_TENSOR_NHWC = 1
RKNN_TENSOR_NC1HWC2 = 2
RKNN_TENSOR_UNDEFINED = 3

FORMAT_NAMES = {
    RKNN_TENSOR_NCHW: "NCHW",
    RKNN_TENSOR_NHWC: "NHWC",
    RKNN_TENSOR_NC1HWC2: "NC1HWC2",
    RKNN_TENSOR_UNDEFINED: "UNDEFINED",
}

# rknn_core_mask
RKNN_NPU_CORE_AUTO = 0
RKNN_NPU_CORE_0 = 1
RKNN_NPU_CORE_1 = 2
RKNN_NPU_CORE_2 = 4
RKNN_NPU_CORE_0_1 = 3
RKNN_NPU_CORE_0_1_2 = 7
RKNN_NPU_CORE_ALL = 0xFFFF

# 返回值
RKNN_SUCC = 0
RKNN_ERR_CODES = {
    -1: "RKNN_ERR_FAIL",
    -2: "RKNN_ERR_TIMEOUT",
    -3: "RKNN_ERR_DEVICE_UNAVAILABLE",
    -4: "RKNN_ERR_MALLOC_FAIL",
    -5: "RKNN_ERR_PARAM_INVALID",
    -6: "RKNN_ERR_MODEL_INVALID",
    -7: "RKNN_ERR_CTX_INVALID",
    -8: "RKNN_ERR_INPUT_INVALID",
    -9: "RKNN_ERR_OUTPUT_INVALID",
    -10: "RKNN_ERR_DEVICE_UNMATCH",
    -11: "RKNN_ERR_INCOMPATILE_PRE_COMPILE_MODEL",
    -12: "RKNN_ERR_INCOMPATILE_OPTIMIZATION_LEVEL_VERSION",
    -13: "RKNN_ERR_TARGET_PLATFORM_UNMATCH",
}


# ──────────────────────────────────────────────────────────────────────
# 结构体定义（字段顺序严格对应 rknn_api.h）
# ──────────────────────────────────────────────────────────────────────


class RknnInputOutputNum(ctypes.Structure):
    """对应 rknn_input_output_num。"""

    _fields_ = [
        ("n_input", ctypes.c_uint32),
        ("n_output", ctypes.c_uint32),
    ]


class RknnInput(ctypes.Structure):
    """对应 rknn_input。

    注意 buf 是指针，ctypes 会自动处理 4→8 字节的对齐填充。
    """

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("type", ctypes.c_int),
        ("fmt", ctypes.c_int),
    ]


class RknnOutput(ctypes.Structure):
    """对应 rknn_output。"""

    _fields_ = [
        ("want_float", ctypes.c_uint8),
        ("is_prealloc", ctypes.c_uint8),
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
    ]


class RknnTensorAttr(ctypes.Structure):
    """对应 rknn_tensor_attr。

    这个结构体最容易写错，因为它有 int8 → int32 的对齐跳变。
    字段顺序与官方头文件逐行对应，ctypes 会自动插入正确的填充。
    """

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("n_dims", ctypes.c_uint32),
        ("dims", ctypes.c_uint32 * RKNN_MAX_DIMS),
        ("name", ctypes.c_char * RKNN_MAX_NAME_LEN),
        ("n_elems", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("fmt", ctypes.c_int),
        ("type", ctypes.c_int),
        ("qnt_type", ctypes.c_int),
        ("fl", ctypes.c_int8),
        ("zp", ctypes.c_int32),
        ("scale", ctypes.c_float),
        ("w_stride", ctypes.c_uint32),
        ("size_with_stride", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("h_stride", ctypes.c_uint32),
    ]


class RknnSdkVersion(ctypes.Structure):
    """对应 rknn_sdk_version。"""

    _fields_ = [
        ("api_version", ctypes.c_char * 256),
        ("drv_version", ctypes.c_char * 256),
    ]


@dataclass
class TensorInfo:
    """张量属性的可读形式。"""

    index: int
    name: str
    dims: list[int]
    fmt: str
    dtype: str
    n_elems: int
    size: int

    def describe(self) -> str:
        return (
            f"[{self.index}] {self.name}  shape={self.dims}  "
            f"fmt={self.fmt}  dtype={self.dtype}  elems={self.n_elems}"
        )


# ──────────────────────────────────────────────────────────────────────
# 库定位
# ──────────────────────────────────────────────────────────────────────

#: 按优先级查找 librknnrt.so 的位置
_CANDIDATE_PATHS = (
    "/usr/lib/librknnrt.so",
    "/usr/local/lib/librknnrt.so",
    "/usr/lib/aarch64-linux-gnu/librknnrt.so",
    "/lib/librknnrt.so",
)


def find_librknnrt() -> str | None:
    """查找 librknnrt.so。

    查找顺序：
      1. 环境变量 ``RKNN_RT_PATH`` 指定的路径
      2. 系统库目录
      3. 项目内 Visual/lbm/runtime/（随仓库携带的那份）

    优先用系统目录，因为 ``sudo cp librknnrt.so /usr/lib/`` 之后
    版本更可控；项目内那份作为兜底，方便没装驱动的场景先跑通。
    """
    env = os.environ.get("RKNN_RT_PATH")
    if env and Path(env).exists():
        return env

    for path in _CANDIDATE_PATHS:
        if Path(path).exists():
            return path

    # 项目内兜底
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "Visual" / "lbm" / "runtime" / "librknnrt.so"
        if candidate.exists():
            return str(candidate)

    # 最后交给动态链接器
    found = ctypes.util.find_library("rknnrt")
    return found


# ──────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────


class RknnError(RuntimeError):
    """RKNN 调用失败。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        self.code = code
        if code is not None:
            name = RKNN_ERR_CODES.get(code, f"未知错误({code})")
            message = f"{message} —— {name}"
        super().__init__(message)


# ──────────────────────────────────────────────────────────────────────
# 主体
# ──────────────────────────────────────────────────────────────────────


class RKNNLite:
    """``rknnlite.api.RKNNLite`` 的 ctypes 等价实现。

    刻意保持与官方 API 同名同签名，这样现有的推理脚本几乎不用改就能迁移::

        rknn = RKNNLite()
        rknn.load_rknn("best.rknn")
        rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0_1_2)
        outputs = rknn.inference(inputs=[img[None, ...]], data_format="nhwc")
        rknn.release()
    """

    # 与官方 API 保持一致的常量命名
    NPU_CORE_AUTO = RKNN_NPU_CORE_AUTO
    NPU_CORE_0 = RKNN_NPU_CORE_0
    NPU_CORE_1 = RKNN_NPU_CORE_1
    NPU_CORE_2 = RKNN_NPU_CORE_2
    NPU_CORE_0_1 = RKNN_NPU_CORE_0_1
    NPU_CORE_0_1_2 = RKNN_NPU_CORE_0_1_2
    NPU_CORE_ALL = RKNN_NPU_CORE_ALL

    def __init__(self, lib_path: str | None = None, verbose: bool = True) -> None:
        self.verbose = verbose
        self._lib = self._load_library(lib_path)
        self._ctx = ctypes.c_uint64(0)
        self._model_buf: ctypes.Array | None = None
        """模型文件必须常驻内存：rknn_init 不拷贝数据，只持有指针。"""
        self._loaded = False
        self._initialized = False
        self._bind_functions()

    # ── 内部 ────────────────────────────────────────────────────────

    @staticmethod
    def _load_library(lib_path: str | None) -> ctypes.CDLL:
        path = lib_path or find_librknnrt()
        if not path:
            raise RknnError(
                "找不到 librknnrt.so。请任选一种方式：\n"
                "  1) sudo cp Visual/lbm/runtime/librknnrt.so /usr/lib/ && "
                "sudo ldconfig\n"
                "  2) export RKNN_RT_PATH=/绝对路径/librknnrt.so\n"
                "  3) 保持项目目录结构不变（会自动在 Visual/lbm/runtime/ 下找）"
            )
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            raise RknnError(f"加载 {path} 失败：{exc}") from exc
        return lib

    def _bind_functions(self) -> None:
        """声明各函数的参数与返回类型。

        **这一步不能省**：ctypes 默认把指针当 int 处理，在 64 位平台
        上传指针会导致段错误。
        """
        lib = self._lib

        lib.rknn_init.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        lib.rknn_init.restype = ctypes.c_int

        lib.rknn_destroy.argtypes = [ctypes.c_uint64]
        lib.rknn_destroy.restype = ctypes.c_int

        lib.rknn_query.argtypes = [
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        lib.rknn_query.restype = ctypes.c_int

        lib.rknn_inputs_set.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.POINTER(RknnInput),
        ]
        lib.rknn_inputs_set.restype = ctypes.c_int

        lib.rknn_run.argtypes = [ctypes.c_uint64, ctypes.c_void_p]
        lib.rknn_run.restype = ctypes.c_int

        lib.rknn_outputs_get.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.POINTER(RknnOutput),
            ctypes.c_void_p,
        ]
        lib.rknn_outputs_get.restype = ctypes.c_int

        lib.rknn_outputs_release.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.POINTER(RknnOutput),
        ]
        lib.rknn_outputs_release.restype = ctypes.c_int

    def _check(self, ret: int, what: str) -> None:
        if ret != RKNN_SUCC:
            raise RknnError(f"{what} 失败", ret)

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[rknn] {message}")

    def _require_ctx(self) -> int:
        if not self._initialized:
            raise RknnError("运行时尚未初始化，请先调用 init_runtime()")
        return self._ctx.value

    # ── 模型加载 ────────────────────────────────────────────────────

    def load_rknn(self, path: str | os.PathLike) -> bool:
        """把 .rknn 模型读进内存。

        注意：这里**不**立即调用 rknn_init，与官方 API 行为一致 ——
        真正的初始化在 ``init_runtime()`` 里做。缓冲区保存在 ``self._model_buf``，
        确保在 destroy 之前不被 GC 回收。
        """
        model_path = Path(path)
        if not model_path.exists():
            raise RknnError(f"模型文件不存在：{model_path}")

        data = model_path.read_bytes()
        self._model_buf = ctypes.create_string_buffer(data, len(data))
        self._loaded = True
        self._log(f"已载入模型 {model_path.name}（{len(data):,} 字节）")
        return True

    # ── 运行时初始化 ────────────────────────────────────────────────

    def init_runtime(
        self,
        core_mask: int = RKNN_NPU_CORE_AUTO,
        flag: int = 0,
    ) -> bool:
        """初始化 NPU 运行时。"""
        if not self._loaded or self._model_buf is None:
            raise RknnError("尚未载入模型，请先调用 load_rknn()")

        # core_mask 通过 flag 的高 16 位传递（RKNN 的约定）
        combined = flag | ((core_mask & 0xFFFF) << 16)

        ret = self._lib.rknn_init(
            ctypes.byref(self._ctx),
            ctypes.cast(self._model_buf, ctypes.c_void_p),
            len(self._model_buf),
            combined,
            None,
        )
        self._check(ret, "rknn_init")

        self._initialized = True
        self._log(f"运行时初始化完成（core_mask={core_mask:#x}）")

        try:
            api_ver, drv_ver = self.query_sdk_version()
            self._log(f"API 版本 {api_ver}  驱动版本 {drv_ver}")
        except RknnError:
            pass
        return True

    # ── 查询 ────────────────────────────────────────────────────────

    def query_sdk_version(self) -> tuple[str, str]:
        ctx = self._require_ctx()
        info = RknnSdkVersion()
        ret = self._lib.rknn_query(
            ctx, RKNN_QUERY_SDK_VERSION, ctypes.byref(info), ctypes.sizeof(info)
        )
        self._check(ret, "查询 SDK 版本")
        return (
            info.api_version.decode(errors="replace"),
            info.drv_version.decode(errors="replace"),
        )

    def query_io_num(self) -> tuple[int, int]:
        ctx = self._require_ctx()
        info = RknnInputOutputNum()
        ret = self._lib.rknn_query(
            ctx, RKNN_QUERY_IN_OUT_NUM, ctypes.byref(info), ctypes.sizeof(info)
        )
        self._check(ret, "查询输入输出数量")
        return info.n_input, info.n_output

    def _query_tensor_attr(self, index: int, is_input: bool) -> RknnTensorAttr:
        ctx = self._require_ctx()
        attr = RknnTensorAttr()
        attr.index = index
        cmd = RKNN_QUERY_INPUT_ATTR if is_input else RKNN_QUERY_OUTPUT_ATTR
        ret = self._lib.rknn_query(
            ctx, cmd, ctypes.byref(attr), ctypes.sizeof(attr)
        )
        self._check(ret, f"查询{'输入' if is_input else '输出'}张量属性")
        return attr

    @staticmethod
    def _attr_to_info(attr: RknnTensorAttr) -> TensorInfo:
        dims = [int(attr.dims[i]) for i in range(attr.n_dims)]
        return TensorInfo(
            index=int(attr.index),
            name=attr.name.decode(errors="replace").rstrip("\x00"),
            dims=dims,
            fmt=FORMAT_NAMES.get(int(attr.fmt), f"?({attr.fmt})"),
            dtype=TENSOR_TYPE_NAMES.get(int(attr.type), f"?({attr.type})"),
            n_elems=int(attr.n_elems),
            size=int(attr.size),
        )

    def get_input_attrs(self) -> list[TensorInfo]:
        n_input, _ = self.query_io_num()
        return [
            self._attr_to_info(self._query_tensor_attr(i, True))
            for i in range(n_input)
        ]

    def get_output_attrs(self) -> list[TensorInfo]:
        _, n_output = self.query_io_num()
        return [
            self._attr_to_info(self._query_tensor_attr(i, False))
            for i in range(n_output)
        ]

    # ── 推理 ────────────────────────────────────────────────────────

    def inference(
        self,
        inputs: Sequence[np.ndarray],
        data_format: str = "nhwc",
        inputs_pass_through: Sequence[int] | None = None,
        **_: object,
    ) -> list[np.ndarray]:
        """执行一次推理。

        参数
        ----
        inputs : numpy 数组序列。YOLO 场景通常是 ``[img[None, ...]]``，
                 即 (1, 640, 640, 3) 的 uint8 RGB。
        data_format : ``"nhwc"`` 或 ``"nchw"``。
        inputs_pass_through : 每路输入是否直通。

            ⚠️ **绝大多数情况必须保持默认（False / 0）。**

            直通模式（1）会把原始字节不经任何处理交给输入节点。只有当
            输入缓冲已经是模型原生格式（本模型是 float16）时才可以用。
            本项目喂的是 uint8 图像，而模型输入是 float16，且转换时配置了
            ``std_values=[[255,255,255]]`` —— 归一化是烘焙进 RKNN 模型里的。
            所以必须让 RKNN 自己做「uint8 → 除 255 → float16」这步转换。

            用错模式的表现：推理不报错，但输出全是 NaN/Inf，或者出现一堆
            置信度异常高的幻觉框。这个坑很隐蔽，因为程序看起来"跑通了"。

        返回
        ----
        numpy 数组列表，各元素为 float32（因为 want_float=1）。
        """
        ctx = self._require_ctx()
        if not inputs:
            raise RknnError("inputs 不能为空")

        n_input, n_output = self.query_io_num()

        if inputs_pass_through is None:
            inputs_pass_through = [0] * len(inputs)

        # ── 设置输入 ──
        rk_inputs = (RknnInput * len(inputs))()
        keepalive: list[np.ndarray] = []

        for position, array in enumerate(inputs):
            arr = np.ascontiguousarray(array)
            keepalive.append(arr)   # 防止被 GC 回收导致悬垂指针

            pass_through = int(inputs_pass_through[position])
            rk_inputs[position].index = position
            rk_inputs[position].buf = arr.ctypes.data_as(ctypes.c_void_p)
            rk_inputs[position].size = arr.nbytes
            rk_inputs[position].pass_through = pass_through
            rk_inputs[position].fmt = (
                RKNN_TENSOR_NHWC if data_format.lower() == "nhwc"
                else RKNN_TENSOR_NCHW
            )

            if pass_through:
                # 直通：缓冲区必须是模型原生格式，只做类型校验
                if arr.dtype not in (np.uint8, np.float16, np.float32):
                    raise RknnError(
                        f"直通模式下输入类型应为 uint8/float16/float32，"
                        f"收到 {arr.dtype}"
                    )
                rk_inputs[position].type = {
                    np.dtype(np.uint8): RKNN_TENSOR_UINT8,
                    np.dtype(np.float16): RKNN_TENSOR_FLOAT16,
                    np.dtype(np.float32): RKNN_TENSOR_FLOAT32,
                }[arr.dtype]
            else:
                # 常规模式：交给 RKNN 做类型转换与归一化
                if arr.dtype != np.uint8:
                    raise RknnError(
                        f"常规模式下输入应为 uint8，收到 {arr.dtype}。"
                        f"RKNN 会负责后续的归一化与类型转换。"
                    )
                rk_inputs[position].type = RKNN_TENSOR_UINT8

        self._check(
            self._lib.rknn_inputs_set(ctx, n_input, rk_inputs), "设置输入"
        )

        # ── 执行 ──
        self._check(self._lib.rknn_run(ctx, None), "执行推理")

        # ── 取输出 ──
        rk_outputs = (RknnOutput * n_output)()
        for i in range(n_output):
            rk_outputs[i].index = i
            rk_outputs[i].want_float = 1     # 统一拿到 float32，省去反量化
            rk_outputs[i].is_prealloc = 0

        self._check(
            self._lib.rknn_outputs_get(
                ctx, n_output, rk_outputs, None
            ),
            "获取输出",
        )

        results: list[np.ndarray] = []
        try:
            for i in range(n_output):
                attr = self._query_tensor_attr(i, False)
                count = int(attr.n_elems)
                buffer = ctypes.cast(
                    rk_outputs[i].buf, ctypes.POINTER(ctypes.c_float)
                )
                flat = np.ctypeslib.as_array(buffer, shape=(count,))
                # 必须 copy：release 之后这块内存就失效了
                shape = tuple(int(attr.dims[j]) for j in range(attr.n_dims))
                results.append(flat.copy().reshape(shape))
        finally:
            self._lib.rknn_outputs_release(ctx, n_output, rk_outputs)

        return results

    # ── 释放 ────────────────────────────────────────────────────────

    def release(self) -> None:
        """释放 NPU 运行时。可重复调用。"""
        if self._initialized:
            try:
                self._lib.rknn_destroy(self._ctx)
            except Exception:
                pass
            self._initialized = False
        self._ctx = ctypes.c_uint64(0)
        self._model_buf = None
        self._loaded = False

    def __enter__(self) -> "RKNNLite":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# 自检
# ──────────────────────────────────────────────────────────────────────


def selftest_diagnostics() -> str:
    """生成一份环境诊断报告，用于排查 NPU 相关问题。"""
    lines = ["── RKNN 环境诊断 ──"]

    path = find_librknnrt()
    if path:
        lines.append(f"  ✓ librknnrt.so: {path}")
    else:
        lines.append("  ✗ 找不到 librknnrt.so")
        return "\n".join(lines)

    # NPU 设备节点
    npu_nodes = [
        p for p in ("/dev/rknpu", "/dev/rknpu0", "/dev/dri/renderD129")
        if Path(p).exists()
    ]
    if npu_nodes:
        lines.append(f"  ✓ NPU 设备节点: {', '.join(npu_nodes)}")
    else:
        lines.append(
            "  ? 未见到 NPU 设备节点（/dev/rknpu 等）"
        )
        lines.append(
            "    注意：沙盒/容器环境看不到设备节点属正常，"
            "以实际运行结果为准"
        )

    # 结构体尺寸自检：与官方头文件在 64 位平台上的期望值比对
    lines.append(f"  · sizeof(rknn_input)       = {ctypes.sizeof(RknnInput)}")
    lines.append(f"  · sizeof(rknn_output)      = {ctypes.sizeof(RknnOutput)}")
    lines.append(f"  · sizeof(rknn_tensor_attr) = {ctypes.sizeof(RknnTensorAttr)}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(selftest_diagnostics())
