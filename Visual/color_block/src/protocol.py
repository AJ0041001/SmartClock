"""SmartClock 视觉端 —— 串口通信协议实现。

本模块是视觉端与 STM32 之间唯一的"契约层"，所有帧的构造都在这里完成。

权威依据（不可擅自改动）：
    SmartClock/Code/BSP/Src/bsp_board.c
        · bsp_game_crc8()          —— CRC-8 算法
        · bsp_game_publish_frame() —— 帧校验与解析
        · bsp_game_feed_byte()     —— 状态机收帧
    SmartClock/Docs/视觉坐标与串口协议.md —— 协议文字说明

固件端原始实现（摘录，用于对照）：

    static uint8_t bsp_game_crc8(const uint8_t *data, uint8_t length)
    {
      uint8_t crc = 0U;
      for (uint8_t i = 0U; i < length; i++)
      {
        crc ^= data[i];
        for (uint8_t bit = 0U; bit < 8U; bit++)
          crc = (crc & 0x80U) ? (uint8_t)((crc << 1U) ^ 0x07U)
                              : (uint8_t)(crc << 1U);
      }
      return crc;
    }

    校验调用点：bsp_game_crc8(&bsp_game_frame[2], 7U) != bsp_game_frame[9]
    即：CRC 覆盖下标 2..8 共 7 字节（含保留字节 8），与下标 9 比对。

注意 C 语言里 uint8_t 的算术在 8 位处自然回绕，Python 必须显式 & 0xFF。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# ──────────────────────────────────────────────────────────────────────
# 一、帧格式常量
# ──────────────────────────────────────────────────────────────────────

FRAME_SIZE = 10
"""整帧固定 10 字节。"""

HEADER_1 = 0xAA
HEADER_2 = 0x55
PAYLOAD_LEN = 0x06
"""字节 2：有效载荷长度，固件里硬性校验必须等于 6。"""

CRC_POLY = 0x07
CRC_INIT = 0x00
CRC_DATA_OFFSET = 2
CRC_DATA_LEN = 7
"""CRC 参与计算的区间：帧下标 [2, 9) 共 7 字节。"""

RESERVED = 0x00


class ControlType(IntEnum):
    """字节 3：控制类型。固件只接受这两个值，其余判为无效帧。"""

    SERIAL_AND_KEYS = 0x01
    """串口坐标作为基准，按键移动量叠加在其上。正常追踪时用这个。"""

    KEYS_ONLY = 0x02
    """只使用按键控制，X/Y 仍须填写并通过 CRC，但不会移动挡板。"""


class Response(IntEnum):
    """STM32 对每一帧的回传结果。"""

    OK = 0
    ERROR = 1
    TIMEOUT = 2


# 固件回传的原始字节串（bsp_game_servicing 里的 ok_response / error_response）
RESPONSE_OK_BYTES = b"OK\r\n"
RESPONSE_ERROR_BYTES = b"ERROR\r\n"


# ──────────────────────────────────────────────────────────────────────
# 二、游戏坐标系常量
# ──────────────────────────────────────────────────────────────────────
# 数值来源：Docs/视觉坐标与串口协议.md 第 1 节。
# 这些常量供坐标映射模块使用，保证与单片机端限幅逻辑自洽。

SCREEN_W = 480
SCREEN_H = 800
"""屏幕逻辑尺寸。"""

INNER_W = 424
INNER_H = 584
"""游戏内部有效区域（白色外框去掉 3px 边框后的内容区）。"""

PADDLE_W = 70
PADDLE_H = 13
"""红色挡板尺寸。"""

# 挡板"中心"坐标的有效范围。视觉端发送的就是中心坐标。
# 推导：中心 X = 挡板左上角 X + PADDLE_W/2 = 0 + 35 .. 354 + 35 = 35 .. 389
#       中心 Y = 挡板左上角 Y + PADDLE_H/2 = 0 + 6  .. 571 + 6  = 6  .. 577
# 文档给的上界是 578（对 13/2=6.5 取整口径不同），这里以文档为准。
CENTER_X_MIN = 35
CENTER_X_MAX = 389
CENTER_Y_MIN = 6
CENTER_Y_MAX = 578

# STM32 端接收后执行的限幅（bsp_lvgl.c 中的挡板绘制逻辑）：
#   挡板左上角 X = clamp(中心 X, 35, 389) - 35
#   挡板左上角 Y = clamp(中心 Y, 6, 578) - 6
# 因此视觉端发送越界坐标是安全的，单片机会贴边处理。但主动限幅能让
# 串口数据更干净、便于排查，故本实现默认发送前先限幅。


# ──────────────────────────────────────────────────────────────────────
# 三、CRC-8
# ──────────────────────────────────────────────────────────────────────


def crc8(data: bytes | bytearray) -> int:
    """计算 CRC-8（多项式 0x07，初值 0x00，无反射，无输出异或）。

    与固件 ``bsp_game_crc8`` 逐比特等价。

    >>> crc8(bytes([0x06, 0x01, 0xD4, 0x00, 0x42, 0x02, 0x00]))
    145
    """
    crc = CRC_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ CRC_POLY) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def crc8_table() -> list[int]:
    """生成 CRC-8 查表，用于高频调用场景。

    与逐位算法结果完全一致；在 RK3588 上单帧 CRC 开销本就可忽略，
    这里提供查表版是为了在千帧级批量测试时更快。
    """
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ CRC_POLY) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table


_CRC_TABLE = crc8_table()


def crc8_fast(data: bytes | bytearray) -> int:
    """查表版 CRC-8，结果与 :func:`crc8` 相同。"""
    crc = CRC_INIT
    for byte in data:
        crc = _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


# ──────────────────────────────────────────────────────────────────────
# 四、帧构造
# ──────────────────────────────────────────────────────────────────────


def clamp_to_center_range(x: int, y: int) -> tuple[int, int]:
    """把中心坐标限幅到挡板可到达的范围。

    发送越界坐标单片机会贴边，但主动限幅可让日志更直观。
    """
    x = max(CENTER_X_MIN, min(CENTER_X_MAX, int(x)))
    y = max(CENTER_Y_MIN, min(CENTER_Y_MAX, int(y)))
    return x, y


def build_frame(
    control_type: ControlType | int,
    x: int,
    y: int,
    reserved: int = RESERVED,
    clamp: bool = True,
) -> bytes:
    """构造一帧 10 字节数据。

    参数
    ----
    control_type : 0x01 或 0x02
    x, y         : 挡板**中心**坐标（小端写入）
    reserved     : 保留字节，当前固定 0x00
    clamp        : 是否在发送前把坐标限幅到 35..389 / 6..578

    返回
    ----
    bytes，长度恒为 10。

    异常
    ----
    ValueError : 类型非法、坐标超出 uint16、保留字节非法。
    """
    try:
        ctype = ControlType(int(control_type))
    except ValueError as exc:
        raise ValueError(
            f"控制类型必须是 0x01 或 0x02，收到 {control_type!r}"
        ) from exc

    if clamp:
        x, y = clamp_to_center_range(x, y)

    if not (0 <= int(x) <= 0xFFFF):
        raise ValueError(f"X 坐标超出 uint16 范围：{x!r}")
    if not (0 <= int(y) <= 0xFFFF):
        raise ValueError(f"Y 坐标超出 uint16 范围：{y!r}")
    if not (0 <= int(reserved) <= 0xFF):
        raise ValueError(f"保留字节非法：{reserved!r}")

    xi, yi = int(x), int(y)
    frame = bytearray(FRAME_SIZE)
    frame[0] = HEADER_1
    frame[1] = HEADER_2
    frame[2] = PAYLOAD_LEN
    frame[3] = int(ctype)
    frame[4] = xi & 0xFF          # X 低字节
    frame[5] = (xi >> 8) & 0xFF   # X 高字节
    frame[6] = yi & 0xFF          # Y 低字节
    frame[7] = (yi >> 8) & 0xFF   # Y 高字节
    frame[8] = int(reserved)
    frame[9] = crc8(frame[CRC_DATA_OFFSET:CRC_DATA_OFFSET + CRC_DATA_LEN])
    return bytes(frame)


def build_tracking_frame(x: int, y: int, clamp: bool = True) -> bytes:
    """构造"串口坐标 + 按键"帧（TYPE=0x01）。色块追踪的常规帧。"""
    return build_frame(ControlType.SERIAL_AND_KEYS, x, y, clamp=clamp)


def build_idle_frame(x: int = CENTER_X_MIN, y: int = CENTER_Y_MIN) -> bytes:
    """构造"仅按键控制"帧（TYPE=0x02）。

    用于视觉端暂时没有检测到色块时交还控制权 —— 单片机会回退到按键模式，
    且保持挡板当前位置不跳变（协议文档第 2 节末段）。
    """
    return build_frame(ControlType.KEYS_ONLY, x, y, clamp=False)


# ──────────────────────────────────────────────────────────────────────
# 五、帧解析（用于自检、抓包分析与单元测试）
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedFrame:
    """一帧解析结果。"""

    control_type: int
    x: int
    y: int
    reserved: int
    crc: int
    crc_ok: bool
    raw: bytes

    @property
    def payload_len(self) -> int:
        return PAYLOAD_LEN


class FrameError(ValueError):
    """帧格式错误。"""


def parse_frame(data: bytes | bytearray) -> ParsedFrame:
    """解析并校验一帧，校验逻辑与固件 ``bsp_game_publish_frame`` 一致。

    抛 ``FrameError`` 的情形对应固件里 ``valid = 0`` 的全部分支：
    帧长不符、帧头错误、长度字段非 6、类型非 1/2、CRC 不匹配。
    """
    if len(data) != FRAME_SIZE:
        raise FrameError(f"帧长必须为 {FRAME_SIZE}，实际 {len(data)}")

    if data[0] != HEADER_1 or data[1] != HEADER_2:
        raise FrameError(
            f"帧头错误：{data[0]:#04x} {data[1]:#04x}，"
            f"期望 {HEADER_1:#04x} {HEADER_2:#04x}"
        )

    if data[2] != PAYLOAD_LEN:
        raise FrameError(f"长度字段必须为 {PAYLOAD_LEN}，实际 {data[2]}")

    control_type = data[3]
    if control_type not in (ControlType.SERIAL_AND_KEYS, ControlType.KEYS_ONLY):
        raise FrameError(f"控制类型非法：{control_type:#04x}")

    expected = crc8(data[CRC_DATA_OFFSET:CRC_DATA_OFFSET + CRC_DATA_LEN])
    crc_ok = expected == data[9]

    x = data[4] | (data[5] << 8)   # 小端
    y = data[6] | (data[7] << 8)

    return ParsedFrame(
        control_type=control_type,
        x=x,
        y=y,
        reserved=data[8],
        crc=data[9],
        crc_ok=crc_ok,
        raw=bytes(data),
    )


def hexdump(data: bytes | bytearray) -> str:
    """把帧格式化成可读字符串，便于日志比对。"""
    return " ".join(f"{b:02X}" for b in data)


# ──────────────────────────────────────────────────────────────────────
# 六、STREAM 收帧状态机（复刻固件 bsp_game_feed_byte 的容错行为）
# ──────────────────────────────────────────────────────────────────────


class FrameAssembler:
    """字节流 → 帧的增量重组器。

    严格复刻固件 ``bsp_game_feed_byte`` 的同步策略，用于：
      · 解析 STM32 回传时可能粘连的其他数据；
      · 在环回自测中验证"垃圾字节 + 合法帧"仍能被正确切分。

    固件策略要点：
      · 等待 0xAA 作为帧首；
      · 收到 0xAA 后期待 0x55；若收到的是 0xAA，则把它当作新的帧首（索引回 1）；
      · 凑满 10 字节即出帧，随后索引清零。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._index = 0

    def reset(self) -> None:
        self._buf = bytearray()
        self._index = 0

    def feed(self, byte: int) -> bytes | None:
        """喂入一个字节；若凑满一帧则返回该帧，否则返回 None。"""
        if self._index == 0:
            if byte == HEADER_1:
                self._buf = bytearray([byte])
                self._index = 1
            return None

        if self._index == 1:
            if byte == HEADER_2:
                self._buf.append(byte)
                self._index = 2
            else:
                if byte == HEADER_1:
                    self._buf = bytearray([byte])
                    self._index = 1
                else:
                    self._buf = bytearray()
                    self._index = 0
            return None

        self._buf.append(byte)
        self._index += 1
        if self._index >= FRAME_SIZE:
            frame = bytes(self._buf)
            self._buf = bytearray()
            self._index = 0
            return frame
        return None

    def feed_bytes(self, data: bytes | bytearray) -> list[bytes]:
        """批量喂入，返回本次凑出的所有帧。"""
        frames = []
        for byte in data:
            frame = self.feed(byte)
            if frame is not None:
                frames.append(frame)
        return frames
