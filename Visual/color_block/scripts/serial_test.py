#!/usr/bin/env python3
"""串口自测工具 —— 不依赖摄像头，单独验证与 STM32 的通信链路。

排查串口问题的顺序很重要。这个脚本按"从易到难"的顺序逐层验证：

    1. 设备是否存在      → 列出所有 /dev/tty*
    2. 能否打开          → 权限、占用问题
    3. 发了有没有回      → 通信是否双向
    4. 回的能不能被解析  → 协议是否对齐

用法::

    # 列出所有串口
    python3 scripts/serial_test.py --list

    # 自动探测哪个口接了 STM32（谁能回 OK 就是它）
    python3 scripts/serial_test.py --probe

    # 对指定口发一帧，看回什么
    python3 scripts/serial_test.py --port /dev/ttyS3

    # 让挡板从左扫到右，肉眼确认 STM32 侧动作
    python3 scripts/serial_test.py --port /dev/ttyS3 --sweep

    # 回环测试：把 USB-TTL 的 TX 和 RX 短接，验证自己发自己收
    python3 scripts/serial_test.py --port /dev/ttyUSB0 --loopback
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cli import (  # noqa: E402
    add_common_arguments,
    add_serial_arguments,
    banner,
    load_config,
    setup_logging,
)
from src.protocol import (  # noqa: E402
    CENTER_X_MAX,
    CENTER_X_MIN,
    CENTER_Y_MAX,
    CENTER_Y_MIN,
    RESPONSE_ERROR_BYTES,
    RESPONSE_OK_BYTES,
    Response,
    build_frame,
    build_idle_frame,
    build_tracking_frame,
    hexdump,
    parse_frame,
)
from src.serialport import (  # noqa: E402
    SerialError,
    SerialPort,
    describe_environment,
    list_serial_ports,
    probe_stm32,
)

#: 由固件原版 C 函数验证过的示例帧（中心坐标 212,578，TYPE=01）
KNOWN_GOOD_FRAME = bytes.fromhex("AA 55 06 01 D4 00 42 02 00 7D")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serial_test.py",
        description="STM32 串口链路自测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_arguments(parser)
    add_serial_arguments(parser)

    parser.add_argument("--list", action="store_true",
                        help="列出所有串口设备")
    parser.add_argument("--probe", action="store_true",
                        help="逐个串口发送探测帧，找出回 OK 的那个")
    parser.add_argument("--sweep", action="store_true",
                        help="让挡板从左到右扫一遍，肉眼验证 STM32 动作")
    parser.add_argument("--loopback", action="store_true",
                        help="回环测试（需把 TX 与 RX 短接）")
    parser.add_argument("--count", type=int, default=5,
                        help="每种测试发送的帧数（默认 5）")
    parser.add_argument("--interval", type=float, default=0.2,
                        help="发送间隔秒数（默认 0.2）")
    parser.add_argument("--raw", action="store_true",
                        help="发送原始十六进制帧，例如 "
                             "--raw 'AA 55 06 02 D4 00 42 02 00 06'")
    parser.add_argument("--bad-crc", action="store_true",
                        help="故意发一个 CRC 错误的帧，验证 STM32 会不会回 ERROR")
    return parser.parse_args()


def hex_to_bytes(text: str) -> bytes:
    """把 "AA 55 06" 或 "AA5506" 解析成字节串。"""
    cleaned = text.replace(" ", "").replace(",", "").replace("0x", "")
    if len(cleaned) % 2:
        raise ValueError("十六进制字符串长度必须是偶数")
    return bytes.fromhex(cleaned)


def show_ports() -> int:
    print(describe_environment())
    print()
    ports = list_serial_ports()
    if not ports:
        print("排查建议：")
        print("  · USB-TTL 转换器插好了吗？")
        print("  · 板子上的 UART 引脚是否已在设备树里启用？")
        print("  · 执行 `dmesg | tail -30` 看内核是否识别到串口")
        return 1
    return 0


def do_probe(config) -> int:
    print(f"── 自动探测（波特率 {config.serial.baudrate}）──")
    print(f"发送的探测帧：{hexdump(KNOWN_GOOD_FRAME)}")
    print("（这是经固件原版 C 代码验证过的合法帧，中心坐标 212,578）")
    print()

    results = probe_stm32(
        baudrate=config.serial.baudrate,
        timeout=config.serial.timeout,
    )

    if not results:
        print("没有可探测的串口。")
        return 1

    ok_devices = []
    for item in results:
        if not item.opened:
            print(f"  {item.device:<16} 打不开")
            continue
        mark = {
            Response.OK: "✓ 回 OK",
            Response.ERROR: "△ 回 ERROR",
            Response.TIMEOUT: "✗ 无响应",
        }[item.response]
        print(f"  {item.device:<16} {mark}   {item.detail}")
        if item.is_candidate:
            ok_devices.append(item.device)

    print()
    print("═" * 66)
    if ok_devices:
        print(f"✅ 找到目标：{', '.join(ok_devices)}")
        print(f"   请在 config.yaml 里设置：serial.port: {ok_devices[0]}")
        return 0
    print("❌ 没有任何串口返回 OK。")
    print()
    print("最常见的原因（按概率排序）：")
    print("  1. STM32 没在运行，或固件里没有调用 BSP_GameSerial_Start()")
    print("  2. TX/RX 接反了（STM32 的 TX 要接板子的 RX，反之亦然）")
    print("  3. 没有共地（两边 GND 必须连在一起）")
    print("  4. 波特率不一致（两边都应是 115200）")
    print("  5. 选错了串口节点（板子上通常有多个 ttyS）")
    return 1


def do_single(port: SerialPort, config, args) -> int:
    """发一帧，打印回传。"""
    frame = build_tracking_frame(212, 578, clamp=False)
    print(f"发送：{hexdump(frame)}")
    reply = port.read_until(RESPONSE_OK_BYTES, timeout=config.serial.timeout)
    if not reply:
        reply = port.read_until(RESPONSE_ERROR_BYTES, timeout=0.2)

    if RESPONSE_OK_BYTES in reply:
        print(f"回传：{reply!r}   → ✅ 单片机确认")
        return 0
    if RESPONSE_ERROR_BYTES in reply:
        print(f"回传：{reply!r}   → ❌ 单片机判为无效帧")
        return 1
    print(f"回传：{reply!r}   → ⏱ 超时无响应")
    return 1


def do_sweep(port: SerialPort, config, args) -> int:
    """让挡板从左扫到右，肉眼确认 STM32 上的动作。"""
    print(f"挡板将从 X={CENTER_X_MIN} 扫到 X={CENTER_X_MAX}，共 {args.count} 步")
    print("请盯着 STM32 屏幕，看红色挡板是否跟着移动。")
    print()

    y = (CENTER_Y_MIN + CENTER_Y_MAX) // 2
    ok_count = 0

    for index in range(args.count):
        ratio = index / max(1, args.count - 1)
        x = int(round(CENTER_X_MIN + ratio * (CENTER_X_MAX - CENTER_X_MIN)))
        frame = build_tracking_frame(x, y)
        response = port.send_frame_and_wait(frame, timeout=config.serial.timeout)

        mark = {Response.OK: "OK", Response.ERROR: "ERROR",
                Response.TIMEOUT: "超时"}[response]
        print(f"  [{index + 1:>2}/{args.count}] X={x:<4} "
              f"{hexdump(frame)}  → {mark}")
        if response is Response.OK:
            ok_count += 1
        time.sleep(args.interval)

    print()
    if ok_count == args.count:
        print("✅ 全部获确认。若挡板也真的跟着动了，说明链路完全打通。")
        return 0
    print(f"⚠️  {ok_count}/{args.count} 获确认。")
    return 1


def do_loopback(port: SerialPort, config, args) -> int:
    """回环测试：短接 TX/RX，自己发自己收。"""
    print("回环测试：需要把串口的 TX 与 RX 用杜邦线短接。")
    print("这个测试用来区分「板子串口本身坏了」和「对端没接好」。")
    print()

    frame = build_tracking_frame(212, 578, clamp=False)
    print(f"发送：{hexdump(frame)}")
    port.reset_input_buffer()
    port.write(frame)
    time.sleep(0.2)
    echoed = port.read(64, timeout=1.0)

    if not echoed:
        print("回读：无数据")
        print()
        print("❌ 没收到自己发的数据。说明问题出在本地串口，"
              "而不是对端 STM32。")
        print("   检查：TX/RX 是否真的短接了？选对设备了吗？")
        return 1

    print(f"回读：{hexdump(echoed)}")
    if echoed == frame:
        print("✅ 完整回环，本地串口收发正常。")
        print("   那么之前收不到 STM32 回传，问题在对端或接线。")
        return 0
    print("△ 收到了数据但与发送不一致（可能有丢字节）。")
    return 1


def main() -> int:
    args = parse_args()
    config = load_config(args)
    setup_logging(config.debug.log_level)

    print(banner("STM32 串口自测", "分层排查：设备 → 打开 → 收发 → 协议"))
    print()

    if args.list or (not args.probe and not args.port and not args.raw):
        code = show_ports()
        if not (args.probe or args.port or args.raw):
            print()
            print("下一步建议：")
            print("  python3 scripts/serial_test.py --probe")
            return code

    if args.probe:
        return do_probe(config)

    # 需要打开具体串口的场景
    if not args.serial_port or args.serial_port == "auto":
        print("请用 --serial-port 指定串口，例如 "
              "--serial-port /dev/ttyS3", file=sys.stderr)
        print("（或先跑 --probe 自动找出是哪一个）", file=sys.stderr)
        return 2

    print(f"打开串口：{args.serial_port} @ {config.serial.baudrate}")
    port = SerialPort(
        args.serial_port,
        baudrate=config.serial.baudrate,
        timeout=config.serial.timeout,
    )
    try:
        port.open()
    except SerialError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print()
        print("权限问题的解决办法（二选一）：", file=sys.stderr)
        print("  · 把当前用户加入 dialout 组后重新登录：", file=sys.stderr)
        print("      sudo usermod -aG dialout $USER", file=sys.stderr)
        print("  · 或临时用 sudo 运行本脚本", file=sys.stderr)
        return 3

    try:
        print()

        if args.raw:
            try:
                raw = hex_to_bytes(args.raw)
            except ValueError as exc:
                print(f"❌ --raw 解析失败：{exc}", file=sys.stderr)
                return 2
            print(f"发送原始帧：{hexdump(raw)}")
            if len(raw) == 10:
                try:
                    parsed = parse_frame(raw)
                    print(f"  本地预校验：类型=0x{parsed.control_type:02X}  "
                          f"X={parsed.x}  Y={parsed.y}  "
                          f"CRC={'通过' if parsed.crc_ok else '不通过'}")
                except Exception as exc:
                    print(f"  本地预校验失败：{exc}")
            port.reset_input_buffer()
            port.write(raw)
            reply = port.read(64, timeout=1.0)
            print(f"回传：{reply!r}")
            return 0

        if args.bad_crc:
            good = bytearray(build_tracking_frame(212, 578, clamp=False))
            good[9] ^= 0xFF
            print(f"故意发送 CRC 错误的帧：{hexdump(good)}")
            print("预期：STM32 应回 ERROR\\r\\n（若回 OK，说明校验逻辑有问题）")
            port.reset_input_buffer()
            port.write(bytes(good))
            reply = port.read_until(RESPONSE_ERROR_BYTES, timeout=1.0)
            if RESPONSE_ERROR_BYTES in reply:
                print(f"回传：{reply!r}  → ✅ 单片机正确识别了错误帧")
                return 0
            print(f"回传：{reply!r}  → ❌ 未按期回 ERROR")
            return 1

        if args.loopback:
            return do_loopback(port, config, args)

        if args.sweep:
            return do_sweep(port, config, args)

        # 默认：发一帧并重复若干次，看稳定性
        exit_code = 0
        for index in range(args.count):
            print(f"[{index + 1}/{args.count}] ", end="")
            exit_code = do_single(port, config, args)
            time.sleep(args.interval)
        return exit_code

    finally:
        port.close()


if __name__ == "__main__":
    raise SystemExit(main())
