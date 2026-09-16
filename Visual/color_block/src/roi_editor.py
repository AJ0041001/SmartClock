"""双 ROI 编辑器 + 控制面板 —— 在实时画面上调参并保存。

解决的核心问题
--------------
色块**部分移出映射区域 A** 时，如果检测区域和 A 是同一个框，模型只能
看到框内剩下的那部分，算出来的中心是**可见部分的中心**而非真实中心。
表现就是：色块贴到左右边界时挡板跟着贴边，**但到不了最边**，
紧贴边缘落下的星星接不到。

方案是拆成两个矩形（都是**大框**，与模型画在卡片上的小检测框无关）：

    ┌──────────── B：检测区域（虚线）────────────┐
    │   ┌──────── A：映射区域（实线）────────┐   │
    │   │                                   │   │
    │   │           🟥 色块                  │   │
    │   │                                   │   │
    │   └───────────────────────────────────┘   │
    └───────────────────────────────────────────┘

  · **检测用 B** —— 色块在 A 边缘时它仍完整落在 B 内，中心算得准
  · **映射用 A** —— 中心超出 A 会被限幅到边缘，正是"挡板贴边"的语义

怎么框（键盘为主，鼠标为辅）
----------------------------
**按一下键 → 在画面里点两下（左上角、右下角）→ 矩形立刻锁定。**

    r  或  a     框选 A（映射区，黄色实线）
    t  或  b     框选 B（检测区，绿色虚线）
    ESC          取消正在进行的框选（不会退出程序）
    0            B = A + 边距（一键重置 B）
    + / -        B 每边放大 / 缩小 10 像素
    f            B 切到整幅画面
    s            保存到 config.yaml
    q            退出

框选过程中**整幅画面都归框选用**，界面上的按钮不会抢走你的点击。
画面顶部会一直显示当前步骤和 A/B 的数值，不用去看别处。

界面布局
--------
按钮条画在**画面内部**（半透明压条），窗口尺寸就等于摄像头画面尺寸。
这样在分辨率小的屏幕上也不会出现"按钮在屏幕外、根本点不到"的情况。

    ┌──────────────────────────────────┐
    │  视频区（含 A / B 两个大框）        │
    │                                   │
    │  [框A][框B]│[B=A+60][B+][B-]│[保存]│ ← 压在画面底部
    │  A 映射区=(…)   B 检测区=(…)       │
    └──────────────────────────────────┘

也可以用 ``debug.ui_layout: stacked`` 换回"按钮条单独占一行"的老样子。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from .mapper import CoordinateMapper, Roi
from .text_cjk import draw_text
from .ui_buttons import ButtonBar, StatusBar

# 配色（BGR）
COLOR_A = (0, 200, 255)          # 映射区域：橙黄
COLOR_B = (120, 255, 120)        # 检测区域：浅绿
COLOR_HINT = (0, 255, 255)
COLOR_WARN = (80, 80, 255)       # 检测框贴到 B 边界：B 太小了
COLOR_PANEL = (38, 38, 44)       # 画面内按钮压条的底色

#: 判定"检测框贴到 B 边界"的容差（像素）。
#: 只要框边缘落在这个距离内，就说明 B 可能裁掉了色块，中心不再可信。
CLIP_TOLERANCE = 3

#: B 每次增减的像素数
B_STEP = 10

#: 矩形最小边长（再小就没意义了）
MIN_ROI_SIDE = 20

#: 画面内压条模式下的状态栏高度（只放一行）
OVERLAY_STATUS_HEIGHT = 24

#: 默认写回的配置文件。
#: 可以用环境变量 ``SMARTCLOCK_CONFIG`` 覆盖 —— 测试就靠它把
#: "保存"重定向到 /dev/null，**绝不会覆盖现场标定好的 config.yaml**。
DEFAULT_CONFIG_PATH = "config.yaml"


def default_config_path() -> str:
    """解析默认配置文件路径（每次调用都读环境变量，便于测试重定向）。"""
    return os.environ.get("SMARTCLOCK_CONFIG") or DEFAULT_CONFIG_PATH


#: 启动后自动显示"操作指引"的秒数。
#: 现场最常见的问题是"不知道要先按键再点两下"，所以启动就先把它画在
#: 画面上，不用去看文档。
GUIDE_SECONDS = 12.0

#: 界面布局：按钮条压在画面内部 / 单独占一行
LAYOUT_OVERLAY = "overlay"
LAYOUT_STACKED = "stacked"
LAYOUT_MODES = (LAYOUT_OVERLAY, LAYOUT_STACKED)

#: 底部键盘提示（无中文字体时的备用版本）。
#: **必须是纯 ASCII** —— 系统没装中文字体时，这行还得看得懂。
HINT_TEXT = ("r=setA(2 clicks)  t=setB  0=B=A+margin  +/-=B size  "
             "f=full  s=save  q=quit")

#: 底部键盘提示（中文版，有中文字体时用）。
#: 现场是中文环境，中文更好认；但太宽就会被右边界裁掉，
#: 所以绘制时先量一下宽度，放不下就退回 HINT_TEXT。
HINT_TEXT_CJK = ("r=框选A(点两下)  t=框选B  0=B=A+边距  +/-=调B  "
                 "f=整幅  s=保存  q=退出")


class EditTarget(str, Enum):
    """正在框选哪个矩形。"""

    NONE = "none"
    A = "a"
    B = "b"


@dataclass
class DualRoiState:
    """双 ROI 编辑的纯状态机（不依赖 cv2，可测试）。"""

    target: EditTarget = EditTarget.NONE
    pending_corner: tuple[int, int] | None = None
    hover: tuple[int, int] | None = None
    """鼠标当前所在位置，用来画"橡皮筋"预览矩形。"""

    message: str = ""
    dirty: bool = False

    @property
    def selecting(self) -> bool:
        return self.target is not EditTarget.NONE

    def start(self, target: EditTarget) -> None:
        self.target = target
        self.pending_corner = None
        self.hover = None
        name = "A（映射区域）" if target is EditTarget.A else "B（检测区域）"
        self.message = f"正在框选 {name} —— 点击矩形的第一个角"

    def cancel(self) -> None:
        self.target = EditTarget.NONE
        self.pending_corner = None
        self.hover = None
        self.message = ""

    def click(self, x: int, y: int) -> tuple[EditTarget, Roi] | None:
        """处理视频区的一次点击。

        返回 ``(目标, 新矩形)``；若这次只是第一个角，返回 None。
        """
        if not self.selecting:
            return None

        if self.pending_corner is None:
            self.pending_corner = (x, y)
            self.message = f"第一个角 ({x}, {y}) —— 再点第二个角"
            return None

        x1, y1 = self.pending_corner
        self.pending_corner = None
        target = self.target
        self.target = EditTarget.NONE

        roi = Roi(
            x=min(x1, x),
            y=min(y1, y),
            w=abs(x - x1),
            h=abs(y - y1),
        )
        if roi.w < MIN_ROI_SIDE or roi.h < MIN_ROI_SIDE:
            self.message = "框太小（<20px），已忽略 —— 重新开始"
            return None

        self.dirty = True
        label = "A（映射）" if target is EditTarget.A else "B（检测）"
        self.message = (f"{label} 设为 ({roi.x},{roi.y}) {roi.w}x{roi.h}"
                        f" —— 按 Save 写回配置")
        return target, roi


class RoiEditor:
    """把状态机接到鼠标、键盘、画面与配置上。"""

    BUTTON_HEIGHT = 46
    STATUS_HEIGHT = 44

    def __init__(
        self,
        config,
        mapper: CoordinateMapper,
        detector=None,
        frame_size: tuple[int, int] = (640, 480),
        config_path: str | None = None,
    ) -> None:
        self.config = config
        self.mapper = mapper
        self.detector = detector
        self.frame_size = (int(frame_size[0]), int(frame_size[1]))
        # None 表示"用默认路径"（可被 SMARTCLOCK_CONFIG 重定向）
        self.config_path = config_path or default_config_path()

        self.state = DualRoiState()
        self.buttons = ButtonBar()
        self.status = StatusBar(self.STATUS_HEIGHT)
        self._build_buttons()

        # 最近一帧检测框是否贴到了 B 的边界。贴边意味着 B 可能裁掉了
        # 色块的一部分，算出来的中心会偏向可见部分的中心 —— 这正是
        # "挡板到不了最边上"的成因，必须在界面上明确提示。
        self.clipped: bool = False
        self.last_bbox: tuple[float, float, float, float] | None = None

        # 鼠标点 Quit 时置位，由主循环每帧检查。
        # 鼠标回调没有返回值能传给主循环，所以只能用标志位。
        self.exit_requested: bool = False

        # 启动指引：框过 A 之后就再也不显示，避免长期占着画面
        self._guide_deadline = time.monotonic() + GUIDE_SECONDS
        self._picked_a_once = False

        # A 先规范化（手写或遗留配置可能给出越界/极小的框）。
        # 必须赶在推导 B 之前做 —— B 是按 A 算出来的。
        original_a = self.mapper.roi
        self.mapper.roi = self._clamp(original_a)
        if self.mapper.roi.as_tuple() != original_a.as_tuple():
            self.state.message = (
                f"A 越界或过小，已修正为 ({self.mapper.roi.x},"
                f"{self.mapper.roi.y}) {self.mapper.roi.w}x"
                f"{self.mapper.roi.h}"
            )

        # B 的当前值（编辑器持有真值，同步给 detector）
        self.roi_b: Roi = self._init_roi_b()
        self._sync_b_to_detector()

    # ── 初始化 ──────────────────────────────────────────────────────

    def _init_roi_b(self) -> Roi:
        """从配置解析 B 的初始值；没配过就按 A + 边距推出。"""
        cfg = self.config.detector
        if cfg.roi_w > 0 and cfg.roi_h > 0:
            return self._clamp(Roi(cfg.roi_x, cfg.roi_y, cfg.roi_w, cfg.roi_h))
        auto = cfg.auto_roi_from(self.mapper.roi)
        return self._clamp(Roi(*auto))

    def _clamp(self, roi: Roi) -> Roi:
        """把矩形限制在画面内。

        ⚠️ 必须先定尺寸、再定位置。反过来写会出问题：先把 x 夹到
        ``width - 1``，再强制 ``w >= MIN_ROI_SIDE``，那么当 x 已经贴近
        右边界时，矩形右边缘就会跑到画面外（x + w > width）。
        编辑器画出来的框和检测器实际裁的区域就对不上了。
        """
        width, height = self.frame_size
        w = max(MIN_ROI_SIDE, min(int(roi.w), width))
        h = max(MIN_ROI_SIDE, min(int(roi.h), height))
        x = max(0, min(int(roi.x), width - w))
        y = max(0, min(int(roi.y), height - h))
        return Roi(x, y, w, h)

    @staticmethod
    def _contains(outer: Roi, inner: Roi) -> bool:
        """outer 是否完整包含 inner。"""
        return (
            outer.x <= inner.x
            and outer.y <= inner.y
            and outer.x + outer.w >= inner.x + inner.w
            and outer.y + outer.h >= inner.y + inner.h
        )

    @staticmethod
    def _union(a: Roi, b: Roi) -> Roi:
        """两个矩形的并集（最小外接矩形）。"""
        x1 = min(a.x, b.x)
        y1 = min(a.y, b.y)
        x2 = max(a.x + a.w, b.x + b.w)
        y2 = max(a.y + a.h, b.y + b.h)
        return Roi(x1, y1, x2 - x1, y2 - y1)

    def _build_buttons(self) -> None:
        # 宽度刻意压得比较紧：640 宽的画面上要保证"保存/退出"也完整可见。
        # 窗口更窄时 ButtonBar.layout() 会自动等比压缩。
        # 标签用中文 —— 板子上有 Noto Sans CJK，中文比英文更好认。
        margin = self.config.detector.roi_margin
        self.buttons.add("pick_a", "框A", width=64)
        self.buttons.add("pick_b", "框B", width=64)
        self.buttons.add_separator()
        self.buttons.add("b_auto", f"B=A+{margin}", width=84)
        self.buttons.add("b_grow", f"B +{B_STEP}", width=62)
        self.buttons.add("b_shrink", f"B -{B_STEP}", width=62)
        self.buttons.add_separator()
        self.buttons.add("toggle_full", "整幅", label_en="FULL", width=56)
        self.buttons.add("save", "保存", label_en="SAVE", width=56)
        self.buttons.add("quit", "退出", label_en="QUIT", width=56,
                         danger=True)

    def _sync_b_to_detector(self) -> None:
        if self.detector is not None and hasattr(self.detector, "roi"):
            self.detector.roi = self.roi_b.as_tuple()

    @property
    def video_height(self) -> int:
        return self.frame_size[1]

    @property
    def layout_mode(self) -> str:
        """界面布局：按钮条压在画面内（overlay）还是单独占一行（stacked）。"""
        mode = str(getattr(self.config.debug, "ui_layout", LAYOUT_OVERLAY))
        return mode if mode in LAYOUT_MODES else LAYOUT_OVERLAY

    @property
    def button_strip_top(self) -> int:
        """按钮条顶端的 y 坐标。

        overlay 模式下按钮画在**画面内部**的底部，所以窗口尺寸就等于
        摄像头画面尺寸 —— 屏幕再小也不会把按钮挤到看不见的地方。
        """
        if self.layout_mode == LAYOUT_STACKED:
            return self.video_height
        return max(0, self.video_height
                   - self.BUTTON_HEIGHT - OVERLAY_STATUS_HEIGHT)

    def button_rect_on_screen(self, action: str
                              ) -> tuple[int, int, int, int] | None:
        """按钮在**窗口坐标系**里的位置。

        overlay/stacked 两种布局的按钮条位置不同，测试和调试代码用这个
        就不用自己算偏移了。
        """
        button = self.buttons.get(action)
        if button is None or not button.action:
            return None
        x, y, w, h = button.rect
        return (x, self.button_strip_top + y, w, h)

    def button_local_y(self, y: int) -> int | None:
        """把窗口 y 换算成"按钮条内部坐标"；不在按钮条上则返回 None。"""
        top = self.button_strip_top
        if top <= y < top + self.BUTTON_HEIGHT:
            return y - top
        return None

    # ── 鼠标 ────────────────────────────────────────────────────────

    def on_mouse(self, event: int, x: int, y: int,
                 _flags: int, _param: object) -> None:
        """OpenCV 鼠标回调。

        **框选优先**：只要正在框选，整幅画面就都归框选用，按钮不抢点击。
        这很重要 —— 否则想点画面顶部/底部的角时会被按钮吃掉，框不出来。
        """
        if event == cv2.EVENT_MOUSEMOVE:
            # 框选到一半时跟着鼠标画"橡皮筋"，落点看得见心里才有底
            if self.state.selecting and self.state.pending_corner is not None:
                self.state.hover = (x, y)
            return

        if event != cv2.EVENT_LBUTTONDOWN:
            return

        if self.state.selecting:
            self._pick_corner(x, y)
            return

        local_y = self.button_local_y(y)
        if local_y is not None:
            action = self.buttons.hit_test(x, local_y)
            if action:
                # 必须接住返回值！Quit 是靠返回信号通知主循环的，
                # 鼠标回调没有调用者能拿到返回值，所以信号要落到
                # exit_requested 上，由主循环每帧检查。
                if self.handle_action(action) == "quit":
                    self.exit_requested = True
                return

        # 画面区域：不在框选状态下点击不做任何事（避免误改 ROI）
        self._pick_corner(x, y)

    def _pick_corner(self, x: int, y: int) -> None:
        """把一次点击交给框选状态机；凑齐两个角就落地成矩形。"""
        self.state.hover = None
        result = self.state.click(x, y)
        if result is None:
            if self.state.selecting and self.state.pending_corner:
                # 让用户在终端也能看到"第一个角已记录"
                first_x, first_y = self.state.pending_corner
                print(f"  → 第一个角已记录 ({first_x}, {first_y})，"
                      f"再点第二个角（右下角）")
            return

        target, roi = result
        if target is EditTarget.A:
            self.set_roi_a(roi)
            self._picked_a_once = True
            print(f"  → A（映射区）已锁定："
                  f"({roi.x},{roi.y}) {roi.w}x{roi.h}")
            self.state.message = (
                f"A 已锁定 ({roi.x},{roi.y}) {roi.w}x{roi.h}"
                f" → 按 0 让 B=A+边距，再按 s 保存"
            )
        else:
            self.set_roi_b(roi)
            print(f"  → B（检测区）已锁定："
                  f"({self.roi_b.x},{self.roi_b.y}) "
                  f"{self.roi_b.w}x{self.roi_b.h}")
        self.buttons.clear_active()

    # ── 动作 ────────────────────────────────────────────────────────

    @property
    def guide_visible(self) -> bool:
        """是否显示启动操作指引。

        默认**不显示**（``debug.show_guide: false``）：底部那行键盘提示已经
        写清楚了按键，教学窗反而挡住画面中间的卡片。
        """
        if not bool(getattr(self.config.debug, "show_guide", False)):
            return False
        if self.state.selecting or self._picked_a_once:
            return False
        return time.monotonic() < self._guide_deadline

    def set_roi_a(self, roi: Roi) -> None:
        """设置映射区域 A，并保证 B 仍然包得住它。"""
        self.mapper.roi = self._clamp(roi)
        # A 变大了就可能顶出 B 之外 —— 那样卡片在 A 边缘时又会被 B 裁掉，
        # 问题原样复现。所以这里自动把 B 撑到能包住 A。
        if not self._contains(self.roi_b, self.mapper.roi):
            self.set_roi_b(self._union(self.roi_b, self.mapper.roi))
            self.state.message = (
                f"A 越出 B，已自动把 B 撑到 ({self.roi_b.x},{self.roi_b.y}) "
                f"{self.roi_b.w}x{self.roi_b.h}"
            )

    def set_roi_b(self, roi: Roi) -> None:
        """设置检测区域 B。

        **强制不变式：B 必须完整包含 A。** 这条不满足，用户遇到的那个
        bug（卡片出界时中心算成可见部分的中心）就会原样复现，而且从界面上
        完全看不出来。所以这里不靠"提醒用户"来保证，而是直接兜住。
        """
        roi = self._clamp(roi)
        if not self._contains(roi, self.mapper.roi):
            roi = self._clamp(self._union(roi, self.mapper.roi))
        self.roi_b = roi
        self._sync_b_to_detector()

    # ── 检测框贴边告警 ──────────────────────────────────────────────

    def note_detection(
        self, bbox_xyxy: "tuple[float, float, float, float] | None"
    ) -> None:
        """记录最近一帧的检测框，判断它是否贴到了 B 的边界。

        贴边 = 色块很可能有一部分被 B 裁掉了 = 中心不再等于真实中心。
        这是"挡板到不了最边上"的直接证据，所以要在界面上报警。
        """
        self.last_bbox = bbox_xyxy
        if bbox_xyxy is None:
            self.clipped = False
            return

        x1, y1, x2, y2 = bbox_xyxy
        b = self.roi_b
        self.clipped = (
            x1 <= b.x + CLIP_TOLERANCE
            or y1 <= b.y + CLIP_TOLERANCE
            or x2 >= b.x + b.w - CLIP_TOLERANCE
            or y2 >= b.y + b.h - CLIP_TOLERANCE
        )

    @property
    def warning(self) -> str:
        """需要显示在状态栏的告警（正常时为空串）。"""
        if not self.clipped:
            return ""
        return "⚠ 检测框贴到 B 边界，中心会偏 → 按 B + 放大检测区域"

    @property
    def unsaved_hint(self) -> str:
        """有改动没保存时的提示。防止用户调了半天直接 Quit 白干。"""
        return "● 有改动未保存 → 按 Save 写回 config.yaml" if self.state.dirty else ""

    def handle_action(self, action: str) -> str | None:
        """执行一个按钮动作。返回特殊信号（如 ``"quit"``）供主循环响应。"""
        if action == "quit":
            # 鼠标点 Quit 时没有返回值收，所以顺手落一个标志位
            self.exit_requested = True
            return "quit"

        if action == "pick_a":
            self.state.start(EditTarget.A)
            self.buttons.set_active("pick_a")

        elif action == "pick_b":
            self.state.start(EditTarget.B)
            self.buttons.set_active("pick_b")

        elif action == "b_auto":
            margin = max(0, int(self.config.detector.roi_margin))
            auto = self.config.detector.auto_roi_from(self.mapper.roi)
            self.set_roi_b(Roi(*auto))
            self.state.dirty = True
            self.state.message = (
                f"B = A + {margin}px → ({self.roi_b.x},{self.roi_b.y}) "
                f"{self.roi_b.w}x{self.roi_b.h}"
            )

        elif action in ("b_grow", "b_shrink"):
            self._resize_b(1 if action == "b_grow" else -1)

        elif action == "toggle_full":
            width, height = self.frame_size
            is_full = (self.roi_b.x == 0 and self.roi_b.y == 0
                       and self.roi_b.w == width and self.roi_b.h == height)
            if is_full:
                auto = self.config.detector.auto_roi_from(self.mapper.roi)
                self.set_roi_b(Roi(*auto))
                self.state.message = "B 已恢复为 A+边距"
            else:
                self.set_roi_b(Roi(0, 0, width, height))
                self.state.message = "B 已设为整幅画面"
            self.state.dirty = True

        elif action == "save":
            self.apply_to_config()
            try:
                self.config.save(self.config_path)
                self.state.dirty = False
                self.state.message = "已保存到 config.yaml"
            except Exception as exc:
                # 保存失败必须保留 dirty，否则用户会以为已经存上了
                self.state.dirty = True
                self.state.message = f"保存失败：{exc}"

        return None

    def _resize_b(self, direction: int) -> None:
        """按每边 B_STEP 像素缩放 B，以中心为基准（避免越调越偏）。

        ``direction=+1`` 放大，``-1`` 缩小。

        缩小时有两条下限：`MIN_ROI_SIDE`，以及"B 必须包住 A"这条不变式。
        两条都可能让请求落空，所以这里**用实际结果说话** —— 改完发现没变，
        就明确告诉用户已经到最小值，而不是嘴上说"B -10px"、实际没动。
        （早期实现的 ``max(2*MIN_ROI_SIDE, w + 2*step)`` 更糟：B 比下限还小时
        按"缩小"反而会把它撑大。）
        """
        previous = self.roi_b.as_tuple()

        def resize_axis(size: int) -> int:
            if direction < 0:
                return size - 2 * B_STEP
            return size + 2 * B_STEP

        new_w = max(MIN_ROI_SIDE, resize_axis(self.roi_b.w))
        new_h = max(MIN_ROI_SIDE, resize_axis(self.roi_b.h))

        # 以中心为基准扩缩，保持居中
        cx = self.roi_b.x + self.roi_b.w / 2
        cy = self.roi_b.y + self.roi_b.h / 2
        self.set_roi_b(Roi(
            int(cx - new_w / 2), int(cy - new_h / 2),
            int(new_w), int(new_h),
        ))

        if self.roi_b.as_tuple() == previous:
            self.state.message = (
                "B 已到最小 —— 再小就包不住 A 了"
                "（卡片贴到 A 边界时会被裁掉一角）"
            )
            return

        self.state.dirty = True
        sign = "+" if direction > 0 else "-"
        self.state.message = (
            f"B {sign}{B_STEP}px/边 → ({self.roi_b.x},{self.roi_b.y}) "
            f"{self.roi_b.w}x{self.roi_b.h}"
        )

    # ── 键盘（与按钮等价的快捷方式）────────────────────────────────

    def on_key(self, key: int) -> str | None:
        """键盘快捷方式。返回 ``"quit"`` 表示请求退出主循环。

        ``r``/``t`` 是给现场用的主键位（"按一下键 → 在画面里点两下"），
        ``a``/``b`` 作为等价别名保留。
        """
        if key in (255, -1):          # 无按键
            return None

        if key in (ord("r"), ord("a")):
            return self.handle_action("pick_a")
        if key in (ord("t"), ord("b")):
            return self.handle_action("pick_b")
        if key in (ord("="), ord("+")):
            return self.handle_action("b_grow")
        if key in (ord("-"), ord("_")):
            return self.handle_action("b_shrink")
        if key == ord("0"):
            return self.handle_action("b_auto")
        if key == ord("f"):
            return self.handle_action("toggle_full")
        if key == ord("s"):
            return self.handle_action("save")
        if key in (ord("q"), 27):
            if self.state.selecting:
                self.state.cancel()
                self.buttons.clear_active()
                self.state.message = "已取消框选（再按 r 重新框 A）"
                return "cancel"
            return "quit"
        return None

    # ── 渲染 ────────────────────────────────────────────────────────

    @staticmethod
    def _draw_dashed_rect(canvas: np.ndarray, roi: Roi,
                          color: tuple[int, int, int],
                          dash: int = 12) -> None:
        """画虚线矩形（OpenCV 没有内置虚线）。"""
        x1, y1 = roi.x, roi.y
        x2, y2 = roi.x + roi.w, roi.y + roi.h
        for x in range(x1, x2, dash * 2):
            cv2.line(canvas, (x, y1), (min(x + dash, x2), y1), color, 2)
            cv2.line(canvas, (x, y2), (min(x + dash, x2), y2), color, 2)
        for y in range(y1, y2, dash * 2):
            cv2.line(canvas, (x1, y), (x1, min(y + dash, y2)), color, 2)
            cv2.line(canvas, (x2, y), (x2, min(y + dash, y2)), color, 2)

    @staticmethod
    def _label(canvas: np.ndarray, text: str, x: int, y: int,
               color: tuple[int, int, int]) -> None:
        """带黑描边的文字，任何背景上都看得清。

        用 text_cjk.draw_text 而不是 cv2.putText：中文也能正确显示，
        缺字体时会自动降级，不会画出一堆乱码。
        """
        draw_text(canvas, text, x, y, size=15, color=color, shadow=True)

    def draw_rois(self, video: np.ndarray) -> np.ndarray:
        """在视频画面上画出 A 与 B。"""
        canvas = video.copy()

        # B：虚线（检测区域）。检测框贴边时变红并给出提示 ——
        # 这是"挡板贴不到最边上"最直观的现场证据。
        color_b = COLOR_WARN if self.clipped else COLOR_B
        self._draw_dashed_rect(canvas, self.roi_b, color_b)
        label = f"B 检测区 {self.roi_b.w}x{self.roi_b.h}"
        if self.clipped:
            label += "  贴边！B 太小"
        self._label(canvas, label,
                    self.roi_b.x + 6, self.roi_b.y + 6, color_b)

        # A：实线 + 四角加粗（映射区域）
        roi = self.mapper.roi
        cv2.rectangle(canvas, (roi.x, roi.y),
                      (roi.x + roi.w, roi.y + roi.h), (0, 0, 0), 4)
        cv2.rectangle(canvas, (roi.x, roi.y),
                      (roi.x + roi.w, roi.y + roi.h), COLOR_A, 2)
        corner = 20
        for (cx, cy, dx, dy) in (
            (roi.x, roi.y, 1, 1),
            (roi.x + roi.w, roi.y, -1, 1),
            (roi.x, roi.y + roi.h, 1, -1),
            (roi.x + roi.w, roi.y + roi.h, -1, -1),
        ):
            cv2.line(canvas, (cx, cy), (cx + dx * corner, cy), COLOR_A, 4)
            cv2.line(canvas, (cx, cy), (cx, cy + dy * corner), COLOR_A, 4)
        self._label(canvas, f"A 映射区 {roi.w}x{roi.h}",
                    roi.x + 6, roi.y + roi.h - 24, COLOR_A)

        # 待确定的第一个角 + 鼠标位置的橡皮筋预览
        if self.state.pending_corner is not None:
            px, py = self.state.pending_corner
            cv2.drawMarker(canvas, (px, py), COLOR_HINT,
                           cv2.MARKER_TILTED_CROSS, 26, 3)
            if self.state.hover is not None:
                hx, hy = self.state.hover
                preview = Roi(min(px, hx), min(py, hy),
                              abs(hx - px), abs(hy - py))
                self._draw_dashed_rect(canvas, preview, COLOR_HINT)
                cv2.drawMarker(canvas, (hx, hy), COLOR_HINT,
                               cv2.MARKER_CROSS, 18, 2)
                self._label(canvas, f"{preview.w}x{preview.h}",
                            hx + 10, hy + 6, COLOR_HINT)

        self._draw_selection_banner(canvas)
        self._draw_startup_guide(canvas)
        return canvas

    def _draw_startup_guide(self, canvas: np.ndarray) -> None:
        """启动后先把"怎么调 ROI"画在画面上，不用去看文档。

        现场最容易卡住的就是"不知道怎么开始"：找不到按钮，或者不知道
        要先按键、再点两下。所以启动头十几秒把步骤直接画出来，
        框过一次 A 之后自动消失，不再挡画面。
        """
        if not self.guide_visible:
            return

        lines = [
            "怎么调这两个框：",
            "1) 按 r → 在画面里点两下，框出卡片会移动到的范围（A 映射区）",
            "2) 按 0 → B 检测区自动 = A 向外扩一圈",
            "3) 按 s → 保存；再把卡片沿 A 四边走一圈，B 变红就按 + 放大",
            "（t 框选 B，ESC 取消，q 退出。不想调就直接按 q 跑）",
        ]

        width, height = canvas.shape[1], canvas.shape[0]
        box_w = min(width - 16, 614)
        box_h = 25 * len(lines) + 14
        x0 = 8
        y0 = max(8, height // 2 - box_h // 2)

        region = canvas[y0:y0 + box_h, x0:x0 + box_w]
        if region.size:
            region[:] = (region.astype(np.float32) * 0.18).astype(np.uint8)
        cv2.rectangle(canvas, (x0, y0), (x0 + box_w, y0 + box_h),
                      COLOR_HINT, 2)
        for index, text in enumerate(lines):
            color = (255, 255, 255) if index == 0 else COLOR_HINT
            size = 17 if index == 0 else 15
            draw_text(canvas, text, x0 + 10, y0 + 8 + index * 25,
                      size=size, color=color)

    def _draw_selection_banner(self, canvas: np.ndarray) -> None:
        """框选过程中在**画面里**给出下一步提示。

        为什么不只写在状态栏：小屏幕上状态栏可能被裁掉，
        用户点了半天不知道程序在等什么。画在画面正中上方就一定能看见。
        """
        if not self.state.selecting:
            return

        target = self.state.target
        name = "A 映射区（黄框）" if target is EditTarget.A else "B 检测区（绿框）"
        if self.state.pending_corner is None:
            step = "第 1 步：点一下矩形【左上角】"
        else:
            px, py = self.state.pending_corner
            step = f"第 2 步：再点一下矩形【右下角】   （第一角 {px},{py}）"
        lines = [f"正在框选 {name}", step, "ESC 取消"]

        width = canvas.shape[1]
        box_w = min(width - 20, 460)
        box_h = 26 * len(lines) + 16
        x0 = max(0, (width - box_w) // 2)
        y0 = 8

        # 半透明黑底，保证任何画面上都看得清
        region = canvas[y0:y0 + box_h, x0:x0 + box_w]
        if region.size:
            region[:] = (region.astype(np.float32) * 0.25).astype(np.uint8)
        cv2.rectangle(canvas, (x0, y0), (x0 + box_w, y0 + box_h),
                      COLOR_HINT, 2)
        for index, text in enumerate(lines):
            color = (255, 255, 255) if index == 0 else COLOR_HINT
            draw_text(canvas, text, x0 + 10, y0 + 8 + index * 26,
                      size=16, color=color)

    def overlay_hint(self, video: np.ndarray) -> np.ndarray:
        """在画面底部画键盘提示。

        与 ``draw_rois`` 不同，这里是**原地修改**（省一次 640x480 的拷贝，
        每帧都要跑）。返回值就是入参本身，方便链式调用。
        """
        from .text_cjk import has_cjk_font, text_size

        text = HINT_TEXT
        if has_cjk_font():
            width, _ = text_size(HINT_TEXT_CJK, 14)
            if width <= video.shape[1] - 16:
                text = HINT_TEXT_CJK
        y = max(0, self.button_strip_top - 22)
        draw_text(video, text, 8, y, size=14, color=(215, 215, 215),
                  shadow=True)
        return video

    def compose(self, video: np.ndarray) -> np.ndarray:
        """把界面画到画面上，返回**最终窗口图像**。

        · ``overlay``（默认）：按钮条与状态行压在画面内部，
          所以返回图像和摄像头画面**一样大** —— 屏幕再小也不会把
          按钮挤到屏幕外。
        · ``stacked``：按钮条与状态栏另占两行，窗口更高。
        """
        width, height = video.shape[1], video.shape[0]

        # 告警优先级最高（直接关系到追踪准不准），其次是"有改动未保存"
        # （防止调完参数直接退出、白调一场），最后才是普通操作提示。
        message = (self.warning or self.unsaved_hint
                   or self.state.message)

        roi_a = self.mapper.roi
        values = (f"A 映射区=({roi_a.x},{roi_a.y},{roi_a.w},{roi_a.h})"
                  f"   B 检测区=({self.roi_b.x},{self.roi_b.y},"
                  f"{self.roi_b.w},{self.roi_b.h})")

        if self.layout_mode == LAYOUT_STACKED:
            self.status.set(values, message, warn=self.clipped)
            bar = self.buttons.render(width)
            status = self.status.render(width)
            return np.vstack([video, bar, status])

        # ── overlay：全部压在画面内部 ──
        self.buttons.render(width)          # 只为了算好各按钮的 rect
        self.status.set(message or values, warn=self.clipped)

        canvas = video
        top = self.button_strip_top
        bottom = min(height, top + self.BUTTON_HEIGHT
                     + OVERLAY_STATUS_HEIGHT)

        # 半透明压条：底下的画面仍然隐约可见，不会"黑掉一块"
        region = canvas[top:bottom, :]
        if region.size:
            region[:] = (region.astype(np.float32) * 0.35
                         + np.float32(20.0)).astype(np.uint8)

        # 按钮：直接把按钮条贴到压条区域内（不占额外高度）
        bar = self.buttons.render(width)
        bar_h = bar.shape[0]
        canvas[top:top + bar_h, :] = bar

        # 状态行再往下压一条
        status_top = top + bar_h
        status_h = min(bottom - status_top, OVERLAY_STATUS_HEIGHT)
        if status_h > 0:
            strip = np.full((status_h, width, 3), COLOR_PANEL, dtype=np.uint8)
            text_color = (255, 120, 120) if self.clipped else (200, 230, 200)
            draw_text(strip, self.status.lines[0] if self.status.lines else "",
                      6, 3, size=14, color=text_color)
            canvas[status_top:status_top + status_h, :] = strip

        return canvas

    # ── 持久化 ──────────────────────────────────────────────────────

    def apply_to_config(self) -> None:
        """把 A 与 B 写进配置对象（落盘由调用方 save）。

        刻意**不**在这里清 dirty：这一步只是往配置对象里赋值，
        真正落盘是调用方的事。如果这里就先清了，``config.save`` 抛异常时
        界面会显示"已保存"，用户以为存上了其实没存。
        """
        roi_a = self.mapper.roi
        self.config.mapping.roi_x = roi_a.x
        self.config.mapping.roi_y = roi_a.y
        self.config.mapping.roi_w = roi_a.w
        self.config.mapping.roi_h = roi_a.h

        self.config.detector.set_roi(self.roi_b.as_tuple())
