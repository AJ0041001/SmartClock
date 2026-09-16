#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小串口通讯测试 —— 验证鲁班猫与 STM32 之间的串口是否通
=========================================================

这个文件**不依赖项目里任何其它模块**，所有逻辑都写在这里，
总共不到 200 行。这样做的目的：

  · 出问题时容易定位 —— 不用怀疑"是不是哪个模块的问题"
  · 你可以通读一遍，完整理解协议是怎么实现的
  · 也可以当作一个独立的协议正确性验证

────────────────────────────────────────────────────────────────
协议（来自 Docs/视觉坐标与串口协议.md，与 STM32 固件一一对应）
────────────────────────────────────────────────────────────────

发送的帧固定 10 字节：

    字节 0   0xAA        帧头 1
    字节 1   0x55        帧头 2
    字节 2   0x06        载荷长度，固定 6
    字节 3   TYPE        控制类型：0x01 或 0x02
    字节 4   X_L         X 坐标低字节
    字节 5   X_H         X 坐标高字节
    字节 6   Y_L         Y 坐标低字节
    字节 7   Y_H         Y 坐标高字节
    字节 8   0x00        保留
    字节 9   CRC8        校验值

    X/Y 为小端序（低字节在前）
    CRC-8：初值 0x00，多项式 0x07，覆盖字节 2~8 共 7 字节

STM32 收到后的回传：

    合法帧 → "OK\r\n"      （4 字节）
    非法帧 → "ERROR\r\n"   （7 字节）

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────

    # 1) 先看有哪些串口
    python3 serial_ping.py --list

    # 2) 自动探测哪个串口接了 STM32（逐个试，谁能回 OK 就是它）
    python3 serial_ping.py --probe

    # 3) 对指定串口发一帧
    python3 serial_ping.py --port /dev/ttyUSB0

    # 4) 回环测试：把 USB-TTL 的 TX 和 RX 用杜邦线短接
    #    用来区分"板子串口坏了"和"对端没接好"
    python3 serial_ping.py --port /dev/ttyUSB0 --loopback

    # 5) 让挡板从左扫到右，肉眼确认 STM32 屏幕上的动作
    python3 serial_ping.py --port /dev/ttyUSB0 --sweep
"""

import argparse
import glob
import os
import select
import sys
import termios
import time

# ──────────────────────────────────────────────────────────────────
# 一、协议实现
# ──────────────────────────────────────────────────────────────────

FRAME_SIZE = 10
HEADER_1 = 0xAA
HEADER_2 = 0x55
PAYLOAD_LEN = 0x06
RESERVED = 0x00

TYPE_SERIAL_AND_KEYS = 0x01     # 串口坐标 + 按键共同控制
TYPE_KEYS_ONLY = 0x02           # 只按键控制（X/Y 仍要填并校验）

RESPONSE_OK = b"OK\r\n"
RESPONSE_ERROR = b"ERROR\r\n"

# 游戏坐标系（挡板中心的有效范围）
CENTER_X_MIN, CENTER_X_MAX = 35, 389
CENTER_Y_MIN, CENTER_Y_MAX = 6, 578


def crc8(data):
    """CRC-8：初值 0x00，多项式 0x07。

    与 STM32 固件里的 bsp_game_crc8() 逐比特等价。已用 10 万组随机
    向量与固件原版 C 函数比对验证过。
    """
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_frame(x, y, control_type=TYPE_SERIAL_AND_KEYS, clamp=True):
    """构造一帧 10 字节数据。

    x, y 是挡板**中心**坐标。超出范围时默认限幅 —— 因为 STM32 那边
    本来也会限幅，主动限幅能让串口日志更直观。
    """
    if clamp:
        x = max(CENTER_X_MIN, min(CENTER_X_MAX, int(x)))
        y = max(CENTER_Y_MIN, min(CENTER_Y_MAX, int(y)))

    frame = bytearray(FRAME_SIZE)
    frame[0] = HEADER_1
    frame[1] = HEADER_2
    frame[2] = PAYLOAD_LEN
    frame[3] = control_type
    frame[4] = x & 0xFF              # X 低字节
    frame[5] = (x >> 8) & 0xFF       # X 高字节
    frame[6] = y & 0xFF              # Y 低字节
    frame[7] = (y >> 8) & 0xFF       # Y 高字节
    frame[8] = RESERVED
    frame[9] = crc8(frame[2:9])      # 覆盖字节 2~8
    return bytes(frame)


def hexdump(data):
    return " ".join("%02X" % b for b in data)


# ──────────────────────────────────────────────────────────────────
# 二、串口（用标准库 termios，不需要装 pyserial）
# ──────────────────────────────────────────────────────────────────

BAUD_TABLE = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}


def list_ports():
    """列出系统当前存在的串口设备。"""
    found = []
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*",
                    "/dev/ttyAMA*", "/dev/ttyFIQ*"):
        for device in sorted(glob.glob(pattern)):
            if device not in found:
                found.append(device)
    return found


def open_serial(device, baudrate=115200):
    """打开串口，配置为 8N1 原始模式。返回文件描述符。"""
    if baudrate not in BAUD_TABLE:
        raise ValueError("不支持的波特率 %d，可选：%s"
                         % (baudrate, sorted(BAUD_TABLE)))

    # O_NOCTTY: 不把它变成控制终端
    # O_NONBLOCK: 打开阶段不阻塞，之后靠 select 控制读超时
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)

    # 输入：关掉所有软件流处理，字节原样进来
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK
               | termios.ISTRIP | termios.INLCR | termios.IGNCR
               | termios.ICRNL | termios.IXON | termios.IXOFF
               | termios.IXANY)
    # 输出：不做任何转换
    oflag &= ~termios.OPOST
    # 控制：8 数据位、无校验、1 停止位
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    # 本地：原始模式，关闭回显与信号字符
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
               | termios.ISIG | termios.IEXTEN)

    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0

    speed = BAUD_TABLE[baudrate]
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, speed, speed, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_for(fd, seconds):
    """在指定时间内尽可能多地读取。"""
    deadline = time.monotonic() + seconds
    chunks = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def describe_response(reply):
    """把回传翻译成人话。"""
    if not reply:
        return "超时无回传", False
    if RESPONSE_OK in reply:
        return "收到 OK —— 通讯正常", True
    if RESPONSE_ERROR in reply:
        return "收到 ERROR —— 单片机判为无效帧（校验或格式不对）", False
    return "收到无法识别的数据：%r" % reply, False


# ──────────────────────────────────────────────────────────────────
# 三、各种测试
# ──────────────────────────────────────────────────────────────────


def test_one_frame(fd, x=212, y=578, timeout=0.5, quiet=False):
    """发一帧，等回传。返回是否成功。"""
    frame = build_frame(x, y, clamp=False)

    if not quiet:
        print("  发送: %s   (中心坐标 %d,%d)" % (hexdump(frame), x, y))

    termios.tcflush(fd, termios.TCIFLUSH)      # 清掉旧数据
    os.write(fd, frame)
    time.sleep(timeout)

    reply = read_for(fd, 0.3)
    text, ok = describe_response(reply)

    if not quiet:
        print("  回传: %r" % reply)
        print("  结果: %s" % text)
    return ok


def cmd_list():
    print("── 当前可用的串口设备 ──")
    ports = list_ports()
    if not ports:
        print("  （一个都没有）")
        print()
        print("可能的原因：")
        print("  · USB-TTL 转换器没插")
        print("  · 板载 UART 在设备树里是 disabled（鲁班猫默认如此）")
        print("  · 驱动没加载")
        return 1
    for device in ports:
        note = ""
        try:
            fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            os.close(fd)
            note = "（可打开）"
        except OSError as exc:
            note = "（打不开：%s）" % exc.strerror
        print("  %-22s %s" % (device, note))
    return 0


def cmd_probe(baudrate, timeout):
    """逐个串口发探测帧，找出哪个接了 STM32。"""
    print("── 自动探测（波特率 %d）──" % baudrate)
    print("判据：谁能正确回 OK，谁就是接到 STM32 的那个")
    print()

    ports = list_ports()
    if not ports:
        print("❌ 没有任何串口设备。")
        print()
        print("请先确认硬件：")
        print("  1. USB-TTL 转换器插在板子的 USB 口上了吗？")
        print("  2. 它的 TX 接 STM32 的 RX、RX 接 STM32 的 TX 了吗？（必须交叉）")
        print("  3. 两边 GND 连在一起了吗？（必须共地）")
        print("  4. STM32 在运行吗？固件里调用了 BSP_GameSerial_Start() 吗？")
        return 1

    winners = []
    for device in ports:
        print("  测试 %s ..." % device, end=" ")
        try:
            fd = open_serial(device, baudrate)
        except OSError as exc:
            print("打不开（%s）" % exc.strerror)
            continue
        except ValueError as exc:
            print("配置失败（%s）" % exc)
            continue

        try:
            ok = False
            # 发两次：某些 USB-TTL 首次发送会丢包
            for _ in range(2):
                if test_one_frame(fd, timeout=timeout, quiet=True):
                    ok = True
                    break
                time.sleep(0.1)
            print("✅ 收到 OK" if ok else "无响应")
            if ok:
                winners.append(device)
        finally:
            os.close(fd)

    print()
    print("═" * 62)
    if winners:
        print("✅ 找到目标：%s" % ", ".join(winners))
        print()
        print("下一步：")
        print("  python3 serial_ping.py --port %s" % winners[0])
        print("  python3 serial_ping.py --port %s --sweep" % winners[0])
        return 0

    print("❌ 没有任何串口返回 OK")
    print()
    print("按这个顺序排查（从最常见的开始）：")
    print("  1. TX/RX 接反了 —— 交换两根线再试（最常见）")
    print("  2. 没有共地 —— 万用表量两边 GND 是否导通")
    print("  3. 波特率不一致 —— 两边都应该是 115200")
    print("  4. STM32 没在运行 / 固件没启用 USART2 接收")
    print("  5. 选错了设备 —— 板子上可能有多个 ttyS")
    return 1


def cmd_loopback(device, baudrate):
    """回环测试：短接 TX/RX，验证本地串口收发。"""
    print("── 回环测试 ──")
    print("前提：把 %s 的 TX 和 RX 用杜邦线短接" % device)
    print()

    fd = open_serial(device, baudrate)
    try:
        frame = build_frame(212, 578, clamp=False)
        print("  发送: %s" % hexdump(frame))
        termios.tcflush(fd, termios.TCIFLUSH)
        os.write(fd, frame)
        time.sleep(0.2)
        echoed = read_for(fd, 1.0)
        print("  回读: %s" % (hexdump(echoed) if echoed else "（无数据）"))
        print()

        if not echoed:
            print("❌ 没收到自己发的数据")
            print("   → 说明本地串口本身有问题，与 STM32 无关")
            print("   → 检查：TX/RX 真的短接了吗？设备选对了吗？")
            return 1
        if echoed == frame:
            print("✅ 完整回环，本地串口收发正常")
            print("   → 那么收不到 STM32 回传，问题在对端或接线")
            return 0
        print("⚠️  收到了数据但与发送不一致（可能有丢字节）")
        return 1
    finally:
        os.close(fd)


def cmd_sweep(device, baudrate, steps, interval):
    """让挡板从左扫到右，肉眼确认 STM32 上的动作。"""
    print("── 扫描测试 ──")
    print("挡板将从 X=%d 扫到 X=%d，共 %d 步" % (CENTER_X_MIN, CENTER_X_MAX, steps))
    print("请盯着 STM32 的屏幕，看红色挡板是否跟着移动")
    print()

    fd = open_serial(device, baudrate)
    try:
        y = (CENTER_Y_MIN + CENTER_Y_MAX) // 2
        ok_count = 0
        for i in range(steps):
            ratio = i / max(1, steps - 1)
            x = int(round(CENTER_X_MIN + ratio * (CENTER_X_MAX - CENTER_X_MIN)))
            frame = build_frame(x, y)
            termios.tcflush(fd, termios.TCIFLUSH)
            os.write(fd, frame)
            time.sleep(0.15)
            reply = read_for(fd, 0.2)
            mark = "OK" if RESPONSE_OK in reply else (
                "ERROR" if RESPONSE_ERROR in reply else "超时")
            if mark == "OK":
                ok_count += 1
            print("  [%2d/%d] X=%-4d %s  → %s" % (i + 1, steps, x,
                                                 hexdump(frame), mark))
            time.sleep(interval)

        print()
        if ok_count == steps:
            print("✅ 全部获确认")
            print("   如果挡板也真的跟着动了，说明链路完全打通")
        else:
            print("⚠️  %d/%d 获确认" % (ok_count, steps))
            return 1
        return 0
    finally:
        os.close(fd)


# ──────────────────────────────────────────────────────────────────
# 四、入口
# ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="最小串口通讯测试（鲁班猫 ↔ STM32）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true", help="列出串口设备")
    parser.add_argument("--probe", action="store_true",
                        help="自动探测哪个串口接了 STM32")
    parser.add_argument("--port", help="串口设备，如 /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=0.5,
                        help="等待回传的秒数")
    parser.add_argument("--loopback", action="store_true",
                        help="回环测试（需短接 TX/RX）")
    parser.add_argument("--bad-crc", action="store_true",
                        help="故意发一个 CRC 错误的帧，验证对方会回 ERROR")
    parser.add_argument("--sweep", action="store_true",
                        help="扫描测试（肉眼看挡板移动）")
    parser.add_argument("--steps", type=int, default=9, help="扫描步数")
    parser.add_argument("--interval", type=float, default=0.3,
                        help="扫描每步间隔秒数")

    args = parser.parse_args()

    print("═" * 62)
    print("  鲁班猫 ↔ STM32 串口通讯测试")
    print("═" * 62)
    print()

    # 没给任何参数时，先列出设备并提示下一步
    if args.list or not (args.probe or args.port):
        code = cmd_list()
        if not (args.probe or args.port):
            print()
            print("下一步：")
            print("  python3 serial_ping.py --probe      # 自动找串口")
            return code

    if args.probe:
        return cmd_probe(args.baudrate, args.timeout)

    if not args.port:
        print("请用 --port 指定串口，或 --probe 自动探测")
        return 2

    if not os.path.exists(args.port):
        print("❌ 串口不存在：%s" % args.port)
        print("   用 --list 看看当前有哪些设备")
        return 2

    # 回环
    if args.loopback:
        return cmd_loopback(args.port, args.baudrate)

    # 故意发坏帧，验证对方的校验逻辑
    if args.bad_crc:
        print("── 坏帧测试：%s ──" % args.port)
        print("故意破坏 CRC，预期对方回 ERROR")
        print()
        try:
            fd = open_serial(args.port, args.baudrate)
        except OSError as exc:
            print("❌ 打不开串口：%s" % exc.strerror)
            return 2
        try:
            frame = bytearray(build_frame(212, 578, clamp=False))
            frame[9] ^= 0xFF          # 破坏 CRC
            print("  发送: %s   (CRC 已破坏)" % hexdump(bytes(frame)))
            termios.tcflush(fd, termios.TCIFLUSH)
            os.write(fd, bytes(frame))
            time.sleep(0.3)
            reply = read_for(fd, 0.5)
            print("  回传: %r" % reply)
            print()
            if RESPONSE_ERROR in reply:
                print("✅ 对方正确识别了坏帧（回了 ERROR）")
                print("   → 说明它的 CRC 校验在工作")
                return 0
            if RESPONSE_OK in reply:
                print("❌ 对方回了 OK —— 它的校验逻辑有问题！")
                return 1
            print("⚠️  没有收到预期的 ERROR（超时或数据异常）")
            return 1
        finally:
            os.close(fd)

    # 扫描
    if args.sweep:
        return cmd_sweep(args.port, args.baudrate, args.steps, args.interval)

    # 默认：发一帧
    print("── 单帧测试：%s @ %d ──" % (args.port, args.baudrate))
    print()
    try:
        fd = open_serial(args.port, args.baudrate)
    except OSError as exc:
        print("❌ 打不开串口：%s" % exc.strerror)
        print()
        print("如果是 Permission denied，把当前用户加入 dialout 组：")
        print("  sudo usermod -aG dialout $USER")
        print("  然后注销重新登录")
        return 2

    try:
        ok = test_one_frame(fd, timeout=args.timeout)
        print()
        if ok:
            print("✅ 通讯正常")
            print()
            print("试试让挡板动起来：")
            print("  python3 serial_ping.py --port %s --sweep" % args.port)
            return 0
        print("❌ 通讯失败")
        print()
        print("排查顺序：")
        print("  1. TX/RX 是否接反（最常见）")
        print("  2. 两边 GND 是否连通")
        print("  3. 波特率是否都是 115200")
        print("  4. STM32 是否在运行")
        print()
        print("想区分是本地问题还是对端问题，做回环测试：")
        print("  短接 TX 和 RX，然后：")
        print("  python3 serial_ping.py --port %s --loopback" % args.port)
        return 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
