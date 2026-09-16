"""画面内按钮条 —— 不依赖 tkinter，用 OpenCV 自绘可点击按钮。

为什么不用 tkinter
------------------
板子上没装 ``python3-tk``，而且 tkinter 与 OpenCV 的 ``imshow`` 各有自己的
事件循环，混用会有"窗口卡死"的经典问题。

所以选择在画面里直接画按钮、自己做点击命中判定。好处：

  · 零额外依赖，拷到板子上就能跑
  · 与视频帧共用同一个窗口和事件循环，不会卡
  · 按钮位置随画面宽度自动排布

按钮的绘制与命中判定是纯计算（输入坐标、输出动作），可以单元测试，
不需要真的开窗口点。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .text_cjk import draw_text, readable_label, text_size

# 配色（BGR）
BG_COLOR = (42, 42, 46)
BORDER_COLOR = (70, 70, 76)
TEXT_COLOR = (235, 235, 235)
TEXT_DISABLED = (120, 120, 120)
BTN_COLOR = (72, 72, 78)
BTN_HOVER = (96, 96, 104)
BTN_ACTIVE = (0, 150, 220)      # 当前处于该模式时高亮
BTN_DANGER = (60, 60, 190)

TEXT_STATUS_COLOR = (210, 230, 210)   # 状态栏正文
WARN_TEXT_COLOR = (90, 90, 255)       # 状态栏告警（红）

BAR_HEIGHT = 46
PADDING = 8
BUTTON_GAP = 6
BUTTON_FONT_SIZE = 17
"""按钮字号。中文在这个字号下最清楚，也不至于撑破按钮宽度。"""


@dataclass
class Button:
    """一个按钮。"""

    action: str
    """点击后返回的动作名。"""

    label: str
    """显示文字。可以含中文（用 Pillow 渲染，见 text_cjk）。"""

    label_en: str = ""
    """没有中文字体时的替代标签。

    纯中文标签在缺字体的机器上会渲染成空白（按钮看起来是空的），
    所以必须准备一个 ASCII 备胎。``框A`` 这种中英混合的标签不用写，
    ``readable_label()`` 会自动留下 ASCII 部分。
    """

    width: int = 92
    enabled: bool = True
    active: bool = False
    """是否处于激活状态（如当前正在框选该区域），激活时高亮显示。"""

    danger: bool = False
    """危险操作（如退出），用不同配色。"""

    rect: tuple[int, int, int, int] = field(
        default=(0, 0, 0, 0), repr=False
    )
    """在工具条图像中的位置 (x, y, w, h)，由 render() 计算。"""

    def contains(self, x: int, y: int) -> bool:
        bx, by, bw, bh = self.rect
        return bx <= x < bx + bw and by <= y < by + bh


class ButtonBar:
    """一排按钮。支持自动排布、绘制、命中判定。"""

    def __init__(self) -> None:
        self.buttons: list[Button] = []
        self._height = BAR_HEIGHT

    # ── 构建 ────────────────────────────────────────────────────────

    def add(self, action: str, label: str, **kwargs) -> Button:
        button = Button(action=action, label=label, **kwargs)
        self.buttons.append(button)
        return button

    def add_separator(self) -> None:
        """加一个视觉分隔（用空白按钮实现，不参与命中）。"""
        sep = Button(action="", label="", width=14)
        sep.enabled = False
        self.buttons.append(sep)

    def clear_active(self) -> None:
        for button in self.buttons:
            button.active = False

    def set_active(self, action: str) -> None:
        self.clear_active()
        for button in self.buttons:
            if button.action == action:
                button.active = True

    def set_enabled(self, action: str, enabled: bool) -> None:
        for button in self.buttons:
            if button.action == action:
                button.enabled = enabled

    def get(self, action: str) -> Button | None:
        for button in self.buttons:
            if button.action == action:
                return button
        return None

    @property
    def height(self) -> int:
        return self._height

    # ── 布局 ────────────────────────────────────────────────────────

    #: 画面太窄时按钮允许被压到的最小宽度。
    #: 之所以压到这么小：宁可字挤一点，也不能让 Save / Quit 跑到画面外
    #: 变成"根本点不到"。640 宽的正常画面上不会触发压缩。
    MIN_BUTTON_WIDTH = 12

    def layout(self, width: int) -> None:
        """计算并写入每个按钮的 rect。

        窗口宽度不够时按比例压缩按钮宽度 —— 宁可字小一点，也不能把
        最后的按钮（往往是 Save / Quit）挤到画面外，那样用户根本点不到。

        与绘制分开是为了让命中判定不依赖 cv2，可以直接测。
        """
        drawable = [b for b in self.buttons if b.width > 0]
        if not drawable:
            return

        y = PADDING
        h = self._height - 2 * PADDING
        # 只有真正的按钮后面才加间隙（分隔符紧贴前后）
        gap_count = sum(1 for b in drawable if b.action)
        available = width - 2 * PADDING - BUTTON_GAP * gap_count

        total = sum(b.width for b in drawable)
        scale = 1.0
        if available > 0 and total > available:
            scale = available / float(total)

        x = PADDING
        for button in drawable:
            if scale >= 1.0:
                w = button.width
            else:
                w = max(self.MIN_BUTTON_WIDTH, int(button.width * scale))
            button.rect = (x, y, w, h)
            # 分隔符只占位，不加间隙
            x += w + (BUTTON_GAP if button.action else 0)

    # ── 绘制 ────────────────────────────────────────────────────────

    def render(self, width: int) -> np.ndarray:
        """生成工具条图像，同时更新各按钮的 rect。"""
        bar = np.full((self._height, width, 3), BG_COLOR, dtype=np.uint8)

        # 顶部一条分隔线，与视频区域区分开
        cv2.line(bar, (0, 0), (width, 0), BORDER_COLOR, 1)

        self.layout(width)

        for button in self.buttons:
            if button.width <= 0:
                continue

            x, y, w, h = button.rect

            if button.action == "":
                # 分隔符：只占位，不绘制
                continue

            # 底色
            if not button.enabled:
                color = BTN_COLOR
            elif button.active:
                color = BTN_ACTIVE
            elif button.danger:
                color = BTN_DANGER
            else:
                color = BTN_COLOR

            # 底色：右下角用 x+w-1 / y+h-1，让**画出来的范围**和
            # contains() 的半开区间完全一致。否则最右一列/最下一行
            # 看着是按钮、点上去却没反应（1 像素的死区）。
            cv2.rectangle(bar, (x, y), (x + w - 1, y + h - 1), color, -1)
            cv2.rectangle(bar, (x, y), (x + w - 1, y + h - 1),
                          BORDER_COLOR, 1)

            # 文字居中。用 text_cjk 量宽高：中文的字宽和英文差很多，
            # 套 cv2 的等宽估算会排歪。
            text_color = TEXT_COLOR if button.enabled else TEXT_DISABLED
            text = readable_label(button.label, button.label_en)
            tw, th = text_size(text, BUTTON_FONT_SIZE)
            tx = x + max(4, (w - tw) // 2)
            ty = y + max(1, (h - th) // 2)
            draw_text(bar, text, tx, ty, size=BUTTON_FONT_SIZE,
                      color=text_color)

        return bar

    # ── 交互 ────────────────────────────────────────────────────────

    def hit_test(self, x: int, y: int) -> str | None:
        """判断点击落在哪个按钮上。

        ``y`` 是**相对工具条顶部**的坐标（调用方需要先减去偏移）。
        返回动作名；没点中或按钮被禁用则返回 None。
        """
        for button in self.buttons:
            if not button.action or not button.enabled:
                continue
            if button.contains(x, y):
                return button.action
        return None


class StatusBar:
    """画面底部的状态栏，用于显示当前参数。

    高度按两行设计：第一行放 A / B 的数值，第二行放提示或告警。
    挤成一行的话，中文告警会被右边界裁掉一半 —— 那还不如不显示。
    """

    #: 最多画几行（超出的忽略）
    MAX_ROWS = 2

    def __init__(self, height: int = 44) -> None:
        self.height = height
        self.lines: list[str] = []
        self.warn: bool = False

    def set(self, *lines: str, warn: bool = False) -> None:
        """设置要显示的行。``warn=True`` 时最后一行用醒目配色。"""
        self.lines = list(lines)
        self.warn = bool(warn)

    def render(self, width: int) -> np.ndarray:
        bar = np.full((self.height, width, 3), (28, 28, 32), dtype=np.uint8)
        rows = self.lines[: self.MAX_ROWS]
        if not rows:
            return bar

        row_height = self.height // max(1, len(rows))
        # 行数少时文字不会被挤扁：单行就占满整个高度
        size = 15 if row_height >= 20 else 12

        for index, line in enumerate(rows):
            if not line:
                continue
            is_warning_row = self.warn and index == len(rows) - 1
            color = WARN_TEXT_COLOR if is_warning_row else TEXT_STATUS_COLOR
            # 状态栏里会出现中文（如"已保存"），必须用支持中文的渲染器，
            # cv2.putText 画中文会变成一串空白。
            draw_text(bar, line, PADDING, index * row_height + 4,
                      size=size, color=color)
        return bar
