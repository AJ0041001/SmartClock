#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
虚拟 STM32 —— 用伪终端(PTY)模拟单片机，让你没有硬件也能测通讯
================================================================

它做三件事，完全复刻真实固件的行为：

  1. 创建一个虚拟串口（PTY），把设备路径打印出来
  2. 解析收到的 10 字节帧（含与固件相同的容错同步逻辑）
  3. 校验帧头/长度/类型/CRC，回 "OK\\r\\n" 或 "ERROR\\r\\n"

用它能验证什么
--------------
  ✅ 帧构造是否正确（字节序、CRC）
  ✅ 串口读写逻辑是否正常
  ✅ 协议解析与错误处理
  ✅ 整个软件链路是否通

用**不能**验证什么
------------------
  ❌ 真实的电气连接（TX/RX 接线、共地、电平）
  ❌ 波特率是否真的匹配
  ❌ STM32 固件本身的行为

所以用法是：**先用它排除软件问题，再去查硬件**。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────

  终端 1（先启动虚拟 STM32）：
      python3 examples/virtual_stm32.py

      它会打印类似：
          虚拟串口已创建：/dev/pts/5
          等待数据...

  终端 2（对虚拟串口跑测试）：
      python3 examples/serial_ping.py --port /dev/pts/5
      python3 examples/serial_ping.py --port /dev/pts/5 --sweep

  还可以故意发一个坏帧，验证错误检测：
      python3 examples/serial_ping.py --port /dev/pts/5 --bad-crc
"""

import argparse
import os
import pty
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serial_ping import (  # noqa: E402
    FRAME_SIZE,
    HEADER_1,
    HEADER_2,
    PAYLOAD_LEN,
    RESPONSE_ERROR,
    RESPONSE_OK,
    TYPE_KEYS_ONLY,
    TYPE_SERIAL_AND_KEYS,
    crc8,
    hexdump,
)


class FakeStm32:
    """复刻固件的收帧状态机与校验逻辑。

    行为对照 Code/BSP/Src/bsp_board.c 的 bsp_game_feed_byte() 与
    bsp_game_publish_frame()：

      · 等 0xAA 作帧首；收到 0xAA 后期待 0x55
      · 若此时收到的是 0xAA，把它当作新的帧首（而不是丢弃整帧）
      · 凑满 10 字节就校验：帧头、长度=6、类型∈{1,2}、CRC
      · 合法回 OK，非法回 ERROR
    """

    def __init__(self, verbose=True):
        self.buffer = bytearray()
        self.index = 0
        self.verbose = verbose

        # 统计
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.last_x = None
        self.last_y = None

    def feed(self, byte):
        """喂入一个字节。返回要回传的字节串，或 None。"""
        if self.index == 0:
            if byte == HEADER_1:
                self.buffer = bytearray([byte])
                self.index = 1
            return None

        if self.index == 1:
            if byte == HEADER_2:
                self.buffer.append(byte)
                self.index = 2
            elif byte == HEADER_1:
                # 固件的容错：把新的 0xAA 当作帧首
                self.buffer = bytearray([byte])
                self.index = 1
            else:
                self.buffer = bytearray()
                self.index = 0
            return None

        self.buffer.append(byte)
        self.index += 1
        if self.index >= FRAME_SIZE:
            frame = bytes(self.buffer)
            self.buffer = bytearray()
            self.index = 0
            return self.publish(frame)
        return None

    def publish(self, frame):
        """校验一帧，返回回传内容。对应固件 bsp_game_publish_frame()。"""
        self.total += 1
        ok = True
        reason = ""

        if frame[0] != HEADER_1 or frame[1] != HEADER_2:
            ok, reason = False, "帧头错误"
        elif frame[2] != PAYLOAD_LEN:
            ok, reason = False, "长度字段错误"
        elif frame[3] not in (TYPE_SERIAL_AND_KEYS, TYPE_KEYS_ONLY):
            ok, reason = False, "控制类型非法"
        elif crc8(frame[2:9]) != frame[9]:
            ok, reason = False, "CRC 不匹配"

        if ok:
            # 坐标解析必须在 verbose 判断之外 —— 否则 --quiet 模式下
            # 就不会记录坐标，测试也拿不到值。
            x = frame[4] | (frame[5] << 8)
            y = frame[6] | (frame[7] << 8)
            self.last_x, self.last_y = x, y

        if self.verbose:
            stamp = time.strftime("%H:%M:%S")
            if ok:
                print("  [%s] ✓ %s  TYPE=%02X  中心=(%d, %d)  → OK"
                      % (stamp, hexdump(frame), frame[3],
                         self.last_x, self.last_y))
            else:
                print("  [%s] ✗ %s  → ERROR（%s）"
                      % (stamp, hexdump(frame), reason))

        if ok:
            self.valid += 1
            return RESPONSE_OK
        self.invalid += 1
        return RESPONSE_ERROR

    def summary(self):
        lines = [
            "",
            "─" * 62,
            "虚拟 STM32 统计",
            "─" * 62,
            "  收到帧总数 : %d" % self.total,
            "  合法帧     : %d" % self.valid,
            "  非法帧     : %d" % self.invalid,
        ]
        if self.last_x is not None:
            lines.append("  最后坐标   : (%d, %d)" % (self.last_x, self.last_y))
        lines.append("─" * 62)
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="虚拟 STM32（PTY 模拟），用于无硬件验证串口通讯",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--quiet", action="store_true",
                        help="不逐帧打印")
    parser.add_argument("--duration", type=float, default=None,
                        help="运行指定秒数后退出（默认一直运行）")
    parser.add_argument("--pty-file", default=None,
                        help="把 PTY 设备路径写入该文件。"
                             "用于自动化测试或脚本调用")
    args = parser.parse_args()

    print("═" * 62)
    print("  虚拟 STM32")
    print("═" * 62)
    print()

    # 创建伪终端对：master 自己用，slave 给对方当"串口"
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    # 把 slave 设成原始模式，避免终端驱动改写字节
    try:
        import termios
        import tty
        tty.setraw(slave)
    except Exception:
        pass

    print("  虚拟串口已创建：%s" % slave_name)
    print()

    # 把路径落盘，供自动化脚本读取（stdout 可能被缓冲，不适合做同步）
    if args.pty_file:
        with open(args.pty_file, "w") as handle:
            handle.write(slave_name)
        print("  路径已写入：%s" % args.pty_file)
        print()
    print("  在**另一个终端**里运行：")
    print("      python3 examples/serial_ping.py --port %s" % slave_name)
    print("      python3 examples/serial_ping.py --port %s --sweep" % slave_name)
    print()
    print("  按 Ctrl+C 退出")
    print("─" * 62)
    print()

    stm32 = FakeStm32(verbose=not args.quiet)
    started = time.monotonic()

    try:
        while True:
            if args.duration and time.monotonic() - started >= args.duration:
                break

            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue

            try:
                chunk = os.read(master, 256)
            except OSError:
                break
            if not chunk:
                break

            for byte in chunk:
                reply = stm32.feed(byte)
                if reply is not None:
                    try:
                        os.write(master, reply)
                    except OSError:
                        pass

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C")
    finally:
        print(stm32.summary())
        os.close(master)
        os.close(slave)

    return 0


if __name__ == "__main__":
    sys.exit(main())
