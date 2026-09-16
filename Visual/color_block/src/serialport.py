"""串口通信封装 —— 纯 Python 实现，不依赖 pyserial。

为什么不用 pyserial？
    鲁班猫板子上默认没有安装 pyserial，而它需要联网 pip/apt 安装。
    为了让本方案"拷过去就能跑"，这里直接用 termios 操作 POSIX 串口设备。
    若环境里恰好装了 pyserial，本模块也不会冲突（完全不引用它）。

对应固件：USART2，接收空闲中断 + DMA 乒乓，回传 ``OK\\r\\n`` / ``ERROR\\r\\n``。
"""

from __future__ import annotations

import errno
import glob
import os
import select
import termios
import time
from dataclasses import dataclass
from typing import Iterable

from .protocol import (
    RESPONSE_ERROR_BYTES,
    RESPONSE_OK_BYTES,
    FrameAssembler,
    Response,
    build_tracking_frame,
)

# ──────────────────────────────────────────────────────────────────────
# 波特率常量映射
# ──────────────────────────────────────────────────────────────────────
# termios 在部分 Python 构建里不暴露 B460800 以上，这里做容错查找。
_BAUD_ATTRS = {
    9600: "B9600",
    19200: "B19200",
    38400: "B38400",
    57600: "B57600",
    115200: "B115200",
    230400: "B230400",
    460800: "B460800",
    921600: "B921600",
}


def resolve_baudrate(baudrate: int) -> int:
    """把整数波特率翻译成 termios 常量，找不到就报错。"""
    name = _BAUD_ATTRS.get(baudrate)
    if name is None:
        supported = ", ".join(str(b) for b in sorted(_BAUD_ATTRS))
        raise ValueError(f"不支持的波特率 {baudrate}，可选：{supported}")
    value = getattr(termios, name, None)
    if value is None:
        raise ValueError(f"本机 termios 未定义 {name}，无法设置波特率 {baudrate}")
    return value


# ──────────────────────────────────────────────────────────────────────
# 设备枚举
# ──────────────────────────────────────────────────────────────────────

#: 鲁班猫4 常见的串口设备命名。ttyS* 是 SoC 原生 UART，ttlUSB/ACM 是 USB 转串口。
_SERIAL_GLOBS = (
    "/dev/ttyS*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/ttyAMA*",
    "/dev/ttyFIQ*",
)


@dataclass(frozen=True)
class SerialPortInfo:
    """一个可用串口设备的描述。"""

    device: str
    description: str = ""


def list_serial_ports(globs: Iterable[str] = _SERIAL_GLOBS) -> list[SerialPortInfo]:
    """列出当前存在的串口设备。

    注意：直接用 glob 扫 /dev，不依赖 pyserial 的 list_ports，
    因此在最小化系统上也能工作。
    """
    found: list[SerialPortInfo] = []
    seen: set[str] = set()

    for pattern in globs:
        for device in sorted(glob.glob(pattern)):
            if device in seen:
                continue
            seen.add(device)

            description = ""
            # 尝试从 sysfs 读取出厂描述，失败就留空（不致命）
            name = os.path.basename(device)
            uevent = f"/sys/class/tty/{name}/device/uevent"
            try:
                with open(uevent, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.startswith("PRODUCT=") or line.startswith("DRIVER="):
                            description = line.strip()
                            break
            except OSError:
                pass

            found.append(SerialPortInfo(device=device, description=description))
    return found


def describe_environment() -> str:
    """生成一段可读的串口环境报告，供自检脚本打印。"""
    ports = list_serial_ports()
    if not ports:
        return "未发现任何串口设备。请确认 USB-TTL 已插好，或串口已在设备树中启用。"
    lines = [f"发现 {len(ports)} 个串口设备："]
    for info in ports:
        suffix = f"  ({info.description})" if info.description else ""
        lines.append(f"  · {info.device}{suffix}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# 串口对象
# ──────────────────────────────────────────────────────────────────────


class SerialError(OSError):
    """串口操作失败。"""


class SerialPort:
    """一个阻塞式（带超时）的串口连接。

    典型用法::

        with SerialPort("/dev/ttyS3", 115200, timeout=0.2) as port:
            port.write(frame)
            print(port.read_until_response())

    本类只做最朴素的读写，帧的组装/校验交给 :mod:`src.protocol`。
    """

    def __init__(
        self,
        device: str,
        baudrate: int = 115200,
        timeout: float = 0.2,
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._fd: int | None = None

    # ── 生命周期 ────────────────────────────────────────────────────

    def open(self) -> "SerialPort":
        if self._fd is not None:
            return self

        try:
            # O_NOCTTY: 不要把这个串口变成控制终端
            # O_NONBLOCK: 打开阶段不阻塞，之后用 select 控制读超时
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialError(
                f"无法打开串口 {self.device}：{exc.strerror}。"
                f"请检查设备是否存在、当前用户是否在 dialout 组。"
            ) from exc

        try:
            self._configure(fd)
        except Exception:
            os.close(fd)
            raise

        self._fd = fd
        return self

    def _configure(self, fd: int) -> None:
        """把 fd 配置成 8N1 原始模式。"""
        try:
            attrs = termios.tcgetattr(fd)
        except termios.error as exc:
            raise SerialError(f"读取串口属性失败：{exc}") from exc

        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs

        # ── 输入：关闭所有软件流处理，保持字节原样 ──
        iflag &= ~(
            termios.IGNBRK
            | termios.BRKINT
            | termios.PARMRK
            | termios.ISTRIP
            | termios.INLCR
            | termios.IGNCR
            | termios.ICRNL
            | termios.IXON
            | termios.IXOFF
            | termios.IXANY
        )

        # ── 输出：不做任何转换 ──
        oflag &= ~termios.OPOST

        # ── 控制：8 数据位、无校验、1 停止位、禁用调制解调器控制线 ──
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL

        # ── 本地模式：原始模式，关闭回显与信号字符 ──
        lflag &= ~(
            termios.ECHO
            | termios.ECHONL
            | termios.ICANON
            | termios.ISIG
            | termios.IEXTEN
        )

        # 读超时：VMIN=0 / VTIME=0 表示纯非阻塞，配合 select 使用
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0

        speed = resolve_baudrate(self.baudrate)
        ispeed = speed
        ospeed = speed

        termios.tcsetattr(
            fd,
            termios.TCSANOW,
            [iflag, oflag, cflag, lflag, ispeed, ospeed, cc],
        )
        termios.tcflush(fd, termios.TCIOFLUSH)

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        finally:
            self._fd = None

    def __enter__(self) -> "SerialPort":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    # ── 读写 ────────────────────────────────────────────────────────

    def _require_fd(self) -> int:
        if self._fd is None:
            raise SerialError("串口尚未打开，请先调用 open()")
        return self._fd

    def write(self, data: bytes) -> int:
        """写全部字节（内部循环处理部分写）。返回写入字节数。"""
        fd = self._require_fd()
        total = 0
        view = memoryview(data)
        while total < len(view):
            try:
                written = os.write(fd, view[total:])
            except BlockingIOError:
                # 发送缓冲满，等一小会儿再试
                select.select([], [fd], [], self.timeout)
                continue
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise SerialError(f"串口写入失败：{exc}") from exc
            total += written
        return total

    def read(self, size: int = 4096, timeout: float | None = None) -> bytes:
        """读取至多 ``size`` 字节。超时无数据则返回空 bytes。"""
        fd = self._require_fd()
        wait = self.timeout if timeout is None else timeout
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            return b""
        try:
            return os.read(fd, size)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return b""
            raise SerialError(f"串口读取失败：{exc}") from exc

    def read_until(
        self,
        terminator: bytes,
        timeout: float | None = None,
        max_bytes: int = 4096,
    ) -> bytes:
        """读到指定结尾串为止（含结尾串）。

        对应固件回传的 ``OK\\r\\n`` / ``ERROR\\r\\n``。
        """
        wait = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait
        buffer = bytearray()

        while len(buffer) < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self.read(4096, timeout=remaining)
            if chunk:
                buffer.extend(chunk)
                index = buffer.find(terminator)
                if index != -1:
                    return bytes(buffer[: index + len(terminator)])
        return bytes(buffer)

    def reset_input_buffer(self) -> None:
        fd = self._require_fd()
        termios.tcflush(fd, termios.TCIFLUSH)

    # ── 协议层辅助 ──────────────────────────────────────────────────

    def send_frame_and_wait(self, frame: bytes, timeout: float = 0.5) -> Response:
        """发送一帧并等待 STM32 回传结果。

        返回 :class:`~src.protocol.Response`：
          · OK      —— 单片机校验通过
          · ERROR   —— 单片机判为无效帧（CRC 或格式错误）
          · TIMEOUT —— 超时没收到任何完整回传
        """
        self.reset_input_buffer()
        self.write(frame)

        reply = self.read_until(RESPONSE_OK_BYTES, timeout=timeout)
        if RESPONSE_OK_BYTES in reply:
            return Response.OK

        # OK 没等到，可能有 ERROR 排在后面；把剩余缓冲也读一下
        reply += self.read(4096, timeout=0.05)
        if RESPONSE_ERROR_BYTES in reply:
            return Response.ERROR
        return Response.TIMEOUT


# ──────────────────────────────────────────────────────────────────────
# 探针：自动发现哪一个是连到 STM32 的口
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    """单个串口的探测结果。"""

    device: str
    opened: bool
    response: Response
    detail: str = ""

    @property
    def is_candidate(self) -> bool:
        """STM32 只要能正确解析就会回 OK —— 这是最强的存在性证据。"""
        return self.response is Response.OK


def probe_stm32(
    baudrate: int = 115200,
    timeout: float = 0.4,
    devices: Iterable[str] | None = None,
    x: int = 212,
    y: int = 578,
) -> list[ProbeResult]:
    """逐个打开串口，发一帧合法的 TYPE=01 坐标帧，看谁回 ``OK``。

    这是判断"哪个 /dev/ttySx 接到了 STM32"最可靠的方法：
    别的设备要么打不开，要么不会回一个恰好是 ``OK\\r\\n`` 的响应。

    参数
    ----
    devices : 指定要探测的设备列表；None 表示扫描全部。
    """
    if devices is None:
        devices = [info.device for info in list_serial_ports()]

    frame = build_tracking_frame(x, y, clamp=False)
    results: list[ProbeResult] = []

    for device in devices:
        port = SerialPort(device, baudrate=baudrate, timeout=timeout)
        try:
            port.open()
        except SerialError as exc:
            results.append(
                ProbeResult(
                    device=device,
                    opened=False,
                    response=Response.TIMEOUT,
                    detail=str(exc),
                )
            )
            continue

        try:
            # 连发两帧提高命中率：某些 USB-TTL 首次发送会丢包
            response = port.send_frame_and_wait(frame, timeout=timeout)
            if response is Response.TIMEOUT:
                response = port.send_frame_and_wait(frame, timeout=timeout)
            results.append(
                ProbeResult(
                    device=device,
                    opened=True,
                    response=response,
                    detail={
                        Response.OK: "收到 OK —— 很可能就是 STM32",
                        Response.ERROR: "收到 ERROR —— 波特率或协议不匹配",
                        Response.TIMEOUT: "无回传",
                    }[response],
                )
            )
        finally:
            port.close()

    return results
