"""协议层单元测试。

最关键的是 :class:`TestFirmwareAlignment` —— 里面硬编码的 CRC 值全部
由固件原版 C 函数（tools/crc_crosscheck/crc_ref.c）实际计算得出，
不是自己算一遍再自己验证。任何对协议的改动只要破坏了兼容性，
这些用例立刻会红。
"""

from __future__ import annotations

import unittest

from src.protocol import (
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
    ControlType,
    FrameAssembler,
    FrameError,
    build_frame,
    build_idle_frame,
    build_tracking_frame,
    clamp_to_center_range,
    crc8,
    crc8_fast,
    hexdump,
    parse_frame,
)


class TestFirmwareAlignment(unittest.TestCase):
    """与 STM32 固件的逐字节对齐验证。

    期望值来源：编译并运行固件原版 bsp_game_crc8() 得到，见
    tools/crc_crosscheck/crc_ref.c。
    """

    #: (参与 CRC 的 7 字节, 固件算出的 CRC)
    REFERENCE_CRC = [
        (bytes([0x06, 0x01, 0xD4, 0x00, 0x42, 0x02, 0x00]), 0x7D),
        (bytes([0x06, 0x02, 0xD4, 0x00, 0x42, 0x02, 0x00]), 0x06),
        (bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]), 0x00),
        (bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]), 0x0C),
        (bytes([0x06, 0x01, 0x23, 0x00, 0x23, 0x00, 0x00]), 0xD9),
        (bytes([0x06, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00]), 0xE5),
    ]

    def test_crc_matches_firmware(self) -> None:
        for data, expected in self.REFERENCE_CRC:
            with self.subTest(data=hexdump(data)):
                self.assertEqual(crc8(data), expected)
                self.assertEqual(crc8_fast(data), expected)

    def test_doc_example_frame(self) -> None:
        """协议文档第 3 节的示例帧应逐字节复现。"""
        frame = build_tracking_frame(212, 578, clamp=False)
        self.assertEqual(
            hexdump(frame), "AA 55 06 01 D4 00 42 02 00 7D"
        )

    def test_crc8_all_zeros_is_zero(self) -> None:
        """初值 0 且数据全 0 时 CRC 必为 0 —— 经典自检点。"""
        self.assertEqual(crc8(bytes(7)), 0x00)

    def test_bitwise_equals_table(self) -> None:
        import random

        rng = random.Random(7)
        for _ in range(2000):
            data = bytes(rng.randrange(256) for _ in range(rng.randint(1, 16)))
            self.assertEqual(crc8(data), crc8_fast(data))


class TestFrameConstruction(unittest.TestCase):
    """帧结构正确性。"""

    def test_frame_layout(self) -> None:
        frame = build_frame(ControlType.SERIAL_AND_KEYS, 0x1234, 0x5678,
                            clamp=False)
        self.assertEqual(len(frame), 10)
        self.assertEqual(frame[0], 0xAA)
        self.assertEqual(frame[1], 0x55)
        self.assertEqual(frame[2], 0x06)
        self.assertEqual(frame[3], 0x01)
        # 小端：低字节在前
        self.assertEqual(frame[4], 0x34)
        self.assertEqual(frame[5], 0x12)
        self.assertEqual(frame[6], 0x78)
        self.assertEqual(frame[7], 0x56)
        self.assertEqual(frame[8], 0x00)
        self.assertEqual(frame[9], crc8(frame[2:9]))

    def test_little_endian_boundaries(self) -> None:
        for value in (0, 1, 255, 256, 1000, 0xFFFF):
            with self.subTest(value=value):
                frame = build_frame(ControlType.SERIAL_AND_KEYS, value, value,
                                    clamp=False)
                self.assertEqual(frame[4] | (frame[5] << 8), value)
                self.assertEqual(frame[6] | (frame[7] << 8), value)

    def test_crc_covers_reserved_byte(self) -> None:
        """保留字节必须参与 CRC —— 固件确实把它算进去了。"""
        frame_a = build_frame(ControlType.SERIAL_AND_KEYS, 100, 100,
                              reserved=0x00, clamp=False)
        frame_b = build_frame(ControlType.SERIAL_AND_KEYS, 100, 100,
                              reserved=0x01, clamp=False)
        self.assertNotEqual(frame_a[9], frame_b[9])

    def test_invalid_control_type(self) -> None:
        for bad in (0x00, 0x03, 0xFF):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    build_frame(bad, 100, 100)

    def test_idle_frame_uses_type_02(self) -> None:
        frame = build_idle_frame()
        self.assertEqual(frame[3], ControlType.KEYS_ONLY)

    def test_out_of_range_coordinate_rejected_without_clamp(self) -> None:
        with self.assertRaises(ValueError):
            build_frame(ControlType.SERIAL_AND_KEYS, 70000, 100, clamp=False)
        with self.assertRaises(ValueError):
            build_frame(ControlType.SERIAL_AND_KEYS, 100, -1, clamp=False)


class TestClamping(unittest.TestCase):
    """限幅逻辑需与文档给出的有效范围一致。"""

    def test_clamp_to_center_range(self) -> None:
        self.assertEqual(clamp_to_center_range(-100, -100),
                         (CENTER_X_MIN, CENTER_Y_MIN))
        self.assertEqual(clamp_to_center_range(9999, 9999),
                         (CENTER_X_MAX, CENTER_Y_MAX))
        self.assertEqual(clamp_to_center_range(200, 300), (200, 300))

    def test_clamp_constants_match_doc(self) -> None:
        """文档明确写的是 35..389 / 6..578，改动需同步更新文档。"""
        self.assertEqual((CENTER_X_MIN, CENTER_X_MAX), (35, 389))
        self.assertEqual((CENTER_Y_MIN, CENTER_Y_MAX), (6, 578))

    def test_frame_is_clamped_by_default(self) -> None:
        frame = build_frame(ControlType.SERIAL_AND_KEYS, 9999, -50)
        parsed = parse_frame(frame)
        self.assertEqual(parsed.x, CENTER_X_MAX)
        self.assertEqual(parsed.y, CENTER_Y_MIN)


class TestParsing(unittest.TestCase):
    """解析与校验。"""

    def test_roundtrip(self) -> None:
        for x, y in ((35, 6), (212, 578), (389, 292), (0, 0), (65535, 65535)):
            with self.subTest(x=x, y=y):
                frame = build_frame(ControlType.SERIAL_AND_KEYS, x, y,
                                    clamp=False)
                parsed = parse_frame(frame)
                self.assertEqual((parsed.x, parsed.y), (x, y))
                self.assertTrue(parsed.crc_ok)
                self.assertEqual(parsed.control_type,
                                 ControlType.SERIAL_AND_KEYS)

    def test_reject_bad_header(self) -> None:
        frame = bytearray(build_tracking_frame(100, 100))
        frame[0] = 0xAB
        with self.assertRaises(FrameError):
            parse_frame(frame)

    def test_reject_bad_length_field(self) -> None:
        frame = bytearray(build_tracking_frame(100, 100))
        frame[2] = 0x07
        with self.assertRaises(FrameError):
            parse_frame(frame)

    def test_reject_bad_type(self) -> None:
        frame = bytearray(build_tracking_frame(100, 100))
        frame[3] = 0x03
        with self.assertRaises(FrameError):
            parse_frame(frame)

    def test_reject_wrong_size(self) -> None:
        with self.assertRaises(FrameError):
            parse_frame(b"\xAA\x55\x06")

    def test_corrupted_crc_detected_not_raised(self) -> None:
        """CRC 错误不抛异常，而是通过 crc_ok=False 暴露 —— 便于统计。"""
        frame = bytearray(build_tracking_frame(100, 100))
        frame[9] ^= 0xFF
        parsed = parse_frame(frame)
        self.assertFalse(parsed.crc_ok)


class TestFrameAssembler(unittest.TestCase):
    """流式收帧状态机 —— 复刻固件 bsp_game_feed_byte 的容错行为。"""

    def test_single_frame(self) -> None:
        assembler = FrameAssembler()
        frame = build_tracking_frame(212, 578, clamp=False)
        frames = assembler.feed_bytes(frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)

    def test_leading_garbage_ignored(self) -> None:
        assembler = FrameAssembler()
        frame = build_tracking_frame(212, 578, clamp=False)
        frames = assembler.feed_bytes(b"\x01\x02\x03" + frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)

    def test_back_to_back_frames(self) -> None:
        assembler = FrameAssembler()
        a = build_tracking_frame(100, 100, clamp=False)
        b = build_tracking_frame(200, 200, clamp=False)
        frames = assembler.feed_bytes(a + b)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], a)
        self.assertEqual(frames[1], b)

    def test_aa_aa_55_resync(self) -> None:
        """固件在帧首位置收到连续的 0xAA 时会把后者当作新帧首。"""
        assembler = FrameAssembler()
        frame = build_tracking_frame(212, 578, clamp=False)
        frames = assembler.feed_bytes(b"\xAA\xAA" + frame[1:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)

    def test_byte_by_byte_stream(self) -> None:
        assembler = FrameAssembler()
        frame = build_tracking_frame(389, 578, clamp=False)
        collected = []
        for byte in frame:
            result = assembler.feed(byte)
            if result is not None:
                collected.append(result)
        self.assertEqual(collected, [frame])

    def test_split_frame_across_chunks(self) -> None:
        """串口一次读到的字节数不确定，必须能跨块拼接。"""
        assembler = FrameAssembler()
        frame = build_tracking_frame(77, 88, clamp=False)
        self.assertEqual(assembler.feed_bytes(frame[:4]), [])
        self.assertEqual(assembler.feed_bytes(frame[4:]), [frame])


class TestHexdump(unittest.TestCase):
    def test_hexdump_format(self) -> None:
        self.assertEqual(
            hexdump(b"\xAA\x55\x06\x01\xD4\x00\x42\x02\x00\x7D"),
            "AA 55 06 01 D4 00 42 02 00 7D",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
