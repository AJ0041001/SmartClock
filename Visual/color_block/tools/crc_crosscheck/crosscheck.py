#!/usr/bin/env python3
"""CRC-8 交叉验证 —— 编译固件原版 C 函数并与 Python 实现逐比特比对。

这是本项目最关键的一项验证：
    视觉端算出的 CRC 只要与单片机差一位，所有帧都会被判为 ERROR。

验证方式不是"看算法像不像"，而是把固件里的 C 函数原样编译成可执行文件，
喂入大量随机字节序列，比对两边输出。

用法：
    python3 crosscheck.py [向量数量，默认 100000]
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent           # color_block/
sys.path.insert(0, str(PROJECT_ROOT))

from src.protocol import (  # noqa: E402
    CRC_DATA_LEN,
    build_tracking_frame,
    crc8,
    crc8_fast,
    hexdump,
)

C_SOURCE = HERE / "crc_ref.c"
C_BINARY = HERE / "crc_ref"


def compile_reference() -> None:
    """编译固件原版 CRC 函数。"""
    print(f"[1/4] 编译固件原版 C 实现：{C_SOURCE.name}")
    result = subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra", "-o", str(C_BINARY), str(C_SOURCE)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("编译失败：")
        print(result.stderr)
        raise SystemExit(1)
    if result.stderr.strip():
        print(f"     编译器警告：{result.stderr.strip()}")
    print(f"     → 生成 {C_BINARY.name}")


def generate_vectors(count: int) -> list[bytes]:
    """生成覆盖多种长度的随机测试向量。

    重点覆盖长度 7 —— 正是协议里实际参与 CRC 计算的字节数。
    """
    rng = random.Random(20260914)  # 固定种子，保证可复现
    vectors: list[bytes] = []

    # 边界与特定向量
    vectors.append(bytes([0x00] * CRC_DATA_LEN))
    vectors.append(bytes([0xFF] * CRC_DATA_LEN))
    vectors.append(bytes([0x06, 0x01, 0xD4, 0x00, 0x42, 0x02, 0x00]))  # 文档示例帧
    vectors.append(bytes([0x06, 0x02, 0xD4, 0x00, 0x42, 0x02, 0x00]))

    # 长度 1..32 的随机序列
    for _ in range(count - len(vectors)):
        length = rng.randint(1, 32)
        vectors.append(bytes(rng.randrange(256) for _ in range(length)))
    return vectors


def run_c_reference(vectors: list[bytes]) -> list[int]:
    """把向量喂给 C 程序，取回 CRC 列表。"""
    stdin_data = "\n".join(hexdump(v) for v in vectors) + "\n"
    result = subprocess.run(
        [str(C_BINARY)],
        input=stdin_data,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("C 参考程序退出异常：")
        print(result.stderr)
        raise SystemExit(1)
    return [int(line) for line in result.stdout.split()]


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000

    compile_reference()

    print(f"[2/4] 生成 {count} 组测试向量（含边界值、全 0、全 FF、文档示例帧）")
    vectors = generate_vectors(count)

    print("[3/4] 运行固件原版 C 实现")
    c_results = run_c_reference(vectors)
    if len(c_results) != len(vectors):
        print(f"结果数量不匹配：C 返回 {len(c_results)}，期望 {len(vectors)}")
        return 1

    print("[4/4] 比对 Python 实现（逐位版 + 查表版）")
    mismatches = 0
    for vector, expected in zip(vectors, c_results):
        got = crc8(vector)
        got_fast = crc8_fast(vector)
        if got != expected or got_fast != expected:
            mismatches += 1
            if mismatches <= 5:
                print(
                    f"     ✗ 不一致 {hexdump(vector)}\n"
                    f"       C={expected:#04x}  python={got:#04x}  table={got_fast:#04x}"
                )

    print()
    print("=" * 62)
    if mismatches == 0:
        print(f"✅ 全部 {len(vectors)} 组向量完全一致（逐位版与查表版均通过）")
    else:
        print(f"❌ 有 {mismatches} / {len(vectors)} 组不一致")
        return 1
    print("=" * 62)

    # 附带输出文档示例帧的实际 CRC，便于人工核对单片机回传
    demo = build_tracking_frame(212, 578, clamp=False)
    print()
    print("文档示例帧（中心坐标 212,578，TYPE=01）实算结果：")
    print(f"    {hexdump(demo)}")
    print(f"    → 字节 9 (CRC) = {demo[9]:#04x} = {demo[9]}")
    print()
    print("可直接用于串口自测的完整帧：")
    print(f"    {hexdump(demo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
