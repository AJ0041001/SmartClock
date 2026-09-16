"""串口通讯端到端测试 —— 用伪终端(PTY)模拟 STM32，全自动验证。

为什么要做成测试而不是手动脚本
------------------------------
手动开两个终端对着敲，验证一次要几十秒，而且**不能回归**：
以后改了串口代码或协议，没人会记得再手工验一遍。

用 PTY 把虚拟 STM32 和客户端接起来，就能在单元测试里跑完整链路：

    serial_ping 的帧构造 → 真实 termios 串口读写 → 虚拟 STM32 校验 → 回传

这验证的是**真实的代码路径**（termios 配置、select 超时、读写循环），
只有电气层是模拟的。

覆盖场景
--------
  · 合法帧 → 收到 OK
  · CRC 错误 → 收到 ERROR
  · 帧头错误 → 收到 ERROR
  · 长度字段错误 → 收到 ERROR
  · 控制类型非法 → 收到 ERROR
  · 垃圾字节 + 合法帧混在一起 → 仍能正确切分
  · 连续多帧 → 全部正确处理
  · 扫描模式 → 每帧都获确认
"""

from __future__ import annotations

import importlib.util
import os
import pty
import select
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT_ROOT / "examples"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        module_name, EXAMPLES / filename
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ping = _load("serial_ping_mod", "serial_ping.py")
vstm32 = _load("virtual_stm32_mod", "virtual_stm32.py")


class SerialHarness:
    """把虚拟 STM32 和串口客户端接到一对 PTY 上。"""

    def __init__(self) -> None:
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.stm32 = vstm32.FakeStm32(verbose=False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        """在后台把 master 侧的数据喂给虚拟 STM32，并写回它的应答。"""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(self.master, 512)
            except OSError:
                break
            for byte in data:
                reply = self.stm32.feed(byte)
                if reply is not None:
                    try:
                        os.write(self.master, reply)
                    except OSError:
                        pass

    def start(self) -> "SerialHarness":
        self._thread.start()
        time.sleep(0.05)      # 让线程就绪
        return self

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class SerialTestCase(unittest.TestCase):
    """所有串口测试的基类：自动建立/销毁 PTY 环境。"""

    def setUp(self) -> None:
        self.harness = SerialHarness().start()
        self.port = self.harness.port

    def tearDown(self) -> None:
        self.harness.close()

    def send_raw(self, payload: bytes, wait: float = 0.4) -> bytes:
        """直接往串口写原始字节，返回对方的回传。"""
        fd = ping.open_serial(self.port, 115200)
        try:
            import termios as _t
            _t.tcflush(fd, _t.TCIFLUSH)
            os.write(fd, payload)
            time.sleep(wait)
            return ping.read_for(fd, 0.3)
        finally:
            os.close(fd)


class TestValidFrames(SerialTestCase):
    """合法帧必须回 OK。"""

    def test_single_frame(self) -> None:
        frame = ping.build_frame(212, 578, clamp=False)
        reply = self.send_raw(frame)
        self.assertIn(ping.RESPONSE_OK, reply)

    def test_various_coordinates(self) -> None:
        for x, y in ((35, 6), (212, 292), (389, 578), (100, 100)):
            with self.subTest(x=x, y=y):
                frame = ping.build_frame(x, y)
                reply = self.send_raw(frame)
                self.assertIn(ping.RESPONSE_OK, reply,
                              f"坐标 ({x},{y}) 未获确认")

    def test_keys_only_type(self) -> None:
        """TYPE=02 也应是合法帧。"""
        frame = ping.build_frame(212, 292, control_type=ping.TYPE_KEYS_ONLY)
        reply = self.send_raw(frame)
        self.assertIn(ping.RESPONSE_OK, reply)

    def test_consecutive_frames(self) -> None:
        """连发多帧，全部应被正确处理。"""
        frames = b"".join(
            ping.build_frame(x, 292) for x in (50, 150, 250, 350)
        )
        reply = self.send_raw(frames, wait=0.6)
        self.assertEqual(reply.count(ping.RESPONSE_OK), 4,
                         f"应收到 4 个 OK，实际 {reply!r}")
        self.assertEqual(self.harness.stm32.valid, 4)

    def test_garbage_then_valid_frame(self) -> None:
        """前面混入垃圾字节，合法帧仍应被识别。"""
        frame = ping.build_frame(200, 300)
        reply = self.send_raw(b"\x01\x02\x03\x04" + frame)
        self.assertIn(ping.RESPONSE_OK, reply)


class TestInvalidFrames(SerialTestCase):
    """非法帧必须回 ERROR —— 这是协议健壮性的核心。"""

    def test_bad_crc(self) -> None:
        frame = bytearray(ping.build_frame(212, 578, clamp=False))
        frame[9] ^= 0xFF                     # 破坏 CRC
        reply = self.send_raw(bytes(frame))
        self.assertIn(ping.RESPONSE_ERROR, reply)
        self.assertEqual(self.harness.stm32.invalid, 1)

    def test_bad_header(self) -> None:
        frame = bytearray(ping.build_frame(212, 578))
        frame[0] = 0xAB                      # 破坏帧头
        reply = self.send_raw(bytes(frame))
        # 帧头错误会导致状态机重新同步，可能完全收不到应答
        self.assertNotIn(ping.RESPONSE_OK, reply)

    def test_bad_length_field(self) -> None:
        frame = bytearray(ping.build_frame(212, 578, clamp=False))
        frame[2] = 0x07                      # 长度字段应为 6
        frame[9] = ping.crc8(frame[2:9])     # 重算 CRC 让它"看起来"合法
        reply = self.send_raw(bytes(frame))
        self.assertIn(ping.RESPONSE_ERROR, reply)

    def test_bad_control_type(self) -> None:
        frame = bytearray(ping.build_frame(212, 578, clamp=False))
        frame[3] = 0x03                      # 只允许 1 或 2
        frame[9] = ping.crc8(frame[2:9])
        reply = self.send_raw(bytes(frame))
        self.assertIn(ping.RESPONSE_ERROR, reply)

    def test_all_zero_frame(self) -> None:
        """全零字节不该被误判为合法帧。"""
        reply = self.send_raw(bytes(10))
        self.assertNotIn(ping.RESPONSE_OK, reply)


class TestFrameResync(SerialTestCase):
    """帧同步的容错行为（复刻固件 bsp_game_feed_byte）。"""

    def test_aa_aa_55_resync(self) -> None:
        """连续 0xAA 时，后一个应被当作新的帧首。"""
        frame = ping.build_frame(200, 300)
        # \xAA\xAA + 去掉第一个字节的合法帧 = \xAA + 完整帧尾
        reply = self.send_raw(b"\xAA\xAA" + frame[1:])
        self.assertIn(ping.RESPONSE_OK, reply)

    def test_two_frames_after_garbage(self) -> None:
        frames = ping.build_frame(100, 200) + ping.build_frame(300, 400)
        reply = self.send_raw(b"\xFF\xFE" + frames, wait=0.5)
        self.assertEqual(reply.count(ping.RESPONSE_OK), 2)


class TestPixelCoordinatesRoundTrip(SerialTestCase):
    """坐标的字节序round-trip —— 小端序写错了会在这里暴露。"""

    def test_coordinates_preserved(self) -> None:
        for x, y in ((35, 6), (212, 578), (389, 292), (1, 2)):
            with self.subTest(x=x, y=y):
                self.harness.stm32.last_x = None
                frame = ping.build_frame(x, y, clamp=False)
                reply = self.send_raw(frame)
                self.assertIn(ping.RESPONSE_OK, reply)
                self.assertEqual(
                    (self.harness.stm32.last_x, self.harness.stm32.last_y),
                    (x, y),
                    "STM32 侧解析出的坐标与发送的不一致 —— 字节序有问题",
                )

    def test_large_values(self) -> None:
        """接近 uint16 上限的值也要能正确传递。"""
        self.harness.stm32.last_x = None
        frame = ping.build_frame(60000, 30000, clamp=False)
        reply = self.send_raw(frame)
        self.assertIn(ping.RESPONSE_OK, reply)
        self.assertEqual(self.harness.stm32.last_x, 60000)
        self.assertEqual(self.harness.stm32.last_y, 30000)


class TestPingCliFunctions(SerialTestCase):
    """serial_ping.py 里的高层函数。"""

    def test_test_one_frame_returns_true(self) -> None:
        fd = ping.open_serial(self.port, 115200)
        try:
            ok = ping.test_one_frame(fd, quiet=True)
        finally:
            os.close(fd)
        self.assertTrue(ok)

    def test_describe_response(self) -> None:
        text, ok = ping.describe_response(b"OK\r\n")
        self.assertTrue(ok)
        self.assertIn("正常", text)

        text, ok = ping.describe_response(b"ERROR\r\n")
        self.assertFalse(ok)
        self.assertIn("ERROR", text)

        text, ok = ping.describe_response(b"")
        self.assertFalse(ok)
        self.assertIn("超时", text)


class TestVirtualStm32Behavior(unittest.TestCase):
    """虚拟 STM32 自身的行为（纯逻辑，不涉及串口）。"""

    def test_valid_frame(self) -> None:
        stm32 = vstm32.FakeStm32(verbose=False)
        frame = ping.build_frame(212, 578, clamp=False)
        reply = None
        for byte in frame:
            result = stm32.feed(byte)
            if result is not None:
                reply = result
        self.assertEqual(reply, ping.RESPONSE_OK)
        self.assertEqual((stm32.last_x, stm32.last_y), (212, 578))
        self.assertEqual(stm32.valid, 1)
        self.assertEqual(stm32.invalid, 0)

    def test_invalid_crc(self) -> None:
        stm32 = vstm32.FakeStm32(verbose=False)
        frame = bytearray(ping.build_frame(212, 578, clamp=False))
        frame[9] ^= 0xFF
        reply = None
        for byte in frame:
            result = stm32.feed(byte)
            if result is not None:
                reply = result
        self.assertEqual(reply, ping.RESPONSE_ERROR)
        self.assertEqual(stm32.invalid, 1)

    def test_resync(self) -> None:
        stm32 = vstm32.FakeStm32(verbose=False)
        frame = ping.build_frame(212, 578, clamp=False)
        replies = []
        for byte in b"\xAA\xAA" + frame[1:]:
            result = stm32.feed(byte)
            if result is not None:
                replies.append(result)
        self.assertEqual(replies, [ping.RESPONSE_OK])


class TestExampleFilesExist(unittest.TestCase):
    def test_examples_present(self) -> None:
        for name in ("serial_ping.py", "virtual_stm32.py"):
            with self.subTest(file=name):
                self.assertTrue((EXAMPLES / name).exists(),
                                f"examples/{name} 缺失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
