"""在 OpenCV 画面上绘制中文 —— 用 Pillow 补上 ``cv2.putText`` 的短板。

为什么需要这个模块
------------------
``cv2.putText`` 只支持 ASCII（Hershey 矢量字体）。直接画中文的结果是
**一串问号或整行空白** —— 这个坑非常隐蔽：程序不报错、坐标全对，
只是字看不见。

板子上自带 Noto Sans CJK 和文泉驿字体，Pillow 也装好了，所以中文完全
可以正常显示。这个模块做三件事：

1. 找一个能显示中文的字体文件（找不到就返回 None）
2. 用 Pillow 把文字渲染成带 alpha 的位图，再混合到 BGR 画面上
3. 把结果缓存起来 —— 文字内容不变时不必每帧重新渲染

降级策略
--------
字体全找不到时**自动丢掉非 ASCII 字符**再画。宁可少显示几个字，
也不要画出一堆乱码让人以为程序坏了。ASCII 部分（坐标、文件名、
置信度这些关键数字）都能保留。
"""

from __future__ import annotations

import functools
from pathlib import Path

import cv2
import numpy as np

#: 候选字体，按优先级排列。都是 Ubuntu 上常见的 CJK 字体。
FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
)

#: 找不到上面任何一个时，再扫一遍这些目录里带 CJK/Noto 字样的字体
FONT_SEARCH_DIRS: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".fonts"),
)


@functools.lru_cache(maxsize=1)
def find_cjk_font() -> str | None:
    """返回第一个可用的中文字体路径；都没有则返回 None。"""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path

    # 候选表没命中，退而求其次扫目录
    for directory in FONT_SEARCH_DIRS:
        root = Path(directory)
        if not root.is_dir():
            continue
        for pattern in ("**/*CJK*.ttc", "**/*CJK*.otf", "**/wqy*.ttc",
                        "**/uming.ttc", "**/*CJK*.ttf"):
            for candidate in sorted(root.glob(pattern)):
                if candidate.is_file():
                    return str(candidate)
    return None


def strip_non_ascii(text: str) -> str:
    """去掉非 ASCII 字符并压缩多余空格（没有中文字体时的降级显示）。"""
    kept = "".join(ch if ord(ch) < 128 else " " for ch in text)
    return " ".join(kept.split())


@functools.lru_cache(maxsize=1)
def _load_font(size: int):
    """加载 Pillow 字体对象（按字号缓存）。失败返回 None。"""
    path = find_cjk_font()
    if path is None:
        return None
    try:
        from PIL import ImageFont
    except ImportError:                       # pragma: no cover - 依赖缺失
        return None
    try:
        return ImageFont.truetype(path, size)
    except (OSError, ValueError):
        return None


@functools.lru_cache(maxsize=256)
def render_bitmap(text: str, size: int,
                  color: tuple[int, int, int]) -> np.ndarray | None:
    """把文字渲染成 (BGR, alpha) 位图。失败返回 None。

    返回 ``(bgr, alpha)``，``alpha`` 是 float32 的 (H, W, 1)，取值 0~1。
    ``color`` 按 **BGR** 传入（与 OpenCV 一致），内部转成 RGB 给 Pillow。
    """
    if not text:
        return None
    font = _load_font(size)
    if font is None:
        return None

    try:
        from PIL import Image, ImageDraw
    except ImportError:                       # pragma: no cover - 依赖缺失
        return None

    # 先用一个够大的画布测量，再按实际包围盒裁剪
    canvas = Image.new("RGBA", (size * (len(text) + 2), size * 2),
                       (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    b, g, r = color
    draw.text((2, 2), text, font=font, fill=(r, g, b, 255))

    bbox = canvas.getbbox()
    if bbox is None:
        return None
    canvas = canvas.crop(bbox)

    arr = np.asarray(canvas, dtype=np.uint8)          # (H, W, 4) RGBA
    bgr = np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])
    alpha = (arr[:, :, 3:4].astype(np.float32)) / 255.0
    return bgr, alpha


def draw_text(
    image: np.ndarray,
    text: str,
    x: int,
    y: int,
    size: int = 14,
    color: tuple[int, int, int] = (235, 235, 235),
    shadow: bool = False,
) -> np.ndarray:
    """在 ``image`` 的 ``(x, y)``（左上角）画一行文字，返回同一个数组。

    有中文字体就用 Pillow 画（支持中文），否则退回 ``cv2.putText``。
    """
    if not text:
        return image

    if shadow:
        # 先画一层黑色描边，保证浅色背景上也看得清
        _blit(image, text, x + 1, y + 1, size, (0, 0, 0))

    if _blit(image, text, x, y, size, color):
        return image

    # 没有中文字体：退回 OpenCV，并丢掉画不出来的字符
    fallback = text if text.isascii() else strip_non_ascii(text)
    if fallback:
        cv2.putText(image, fallback, (x, y + size),
                    cv2.FONT_HERSHEY_SIMPLEX, size / 30.0, color, 1,
                    cv2.LINE_AA)
    return image


def _blit(image: np.ndarray, text: str, x: int, y: int, size: int,
          color: tuple[int, int, int]) -> bool:
    """把渲染好的位图混合到画面上。成功返回 True。"""
    bitmap = render_bitmap(text, size, color)
    if bitmap is None:
        return False
    bgr, alpha = bitmap

    height, width = bgr.shape[:2]
    image_h, image_w = image.shape[:2]

    # 完全在画面外就跳过（避免 numpy 切片变成空数组）
    if x >= image_w or y >= image_h or x + width <= 0 or y + height <= 0:
        return True

    # 裁剪到画面内
    src_x0 = max(0, -x)
    src_y0 = max(0, -y)
    dst_x0 = max(0, x)
    dst_y0 = max(0, y)
    copy_w = min(width - src_x0, image_w - dst_x0)
    copy_h = min(height - src_y0, image_h - dst_y0)
    if copy_w <= 0 or copy_h <= 0:
        return True

    patch = bgr[src_y0:src_y0 + copy_h, src_x0:src_x0 + copy_w]
    mask = alpha[src_y0:src_y0 + copy_h, src_x0:src_x0 + copy_w]
    region = image[dst_y0:dst_y0 + copy_h, dst_x0:dst_x0 + copy_w]
    region[:] = (region.astype(np.float32) * (1.0 - mask)
                 + patch.astype(np.float32) * mask).astype(np.uint8)
    return True


def has_cjk_font() -> bool:
    """当前环境能否正常显示中文。"""
    return find_cjk_font() is not None


def text_size(text: str, size: int = 14) -> tuple[int, int]:
    """量一行文字的像素尺寸 ``(宽, 高)``。

    有中文字体时按真实字形量（中文比英文宽得多，用等宽假设会排歪）；
    没有字体时退回 OpenCV 的估算。
    """
    if not text:
        return (0, 0)
    bitmap = render_bitmap(text, size, (255, 255, 255))
    if bitmap is not None:
        bgr, _ = bitmap
        return (bgr.shape[1], bgr.shape[0])
    fallback = text if text.isascii() else strip_non_ascii(text)
    if not fallback:
        return (0, size)
    (width, height), _ = cv2.getTextSize(
        fallback, cv2.FONT_HERSHEY_SIMPLEX, size / 30.0, 1
    )
    return (width, height)


def readable_label(label: str, label_en: str = "") -> str:
    """挑一个当前环境画得出来的标签。

    中文标签在缺字体的机器上会变成空白（比乱码更糟：按钮看起来是空的）。
    所以纯中文标签要准备一个 ASCII 替代；``框A`` 这种"中文+ASCII"的
    标签会自动丢掉中文部分，剩下的 ``A`` 仍然有指示意义。
    """
    if label.isascii() or has_cjk_font():
        return label
    if label_en:
        return label_en
    return strip_non_ascii(label)


def describe() -> str:
    """给启动日志用的一句话说明。"""
    if has_cjk_font():
        return f"中文字体：{find_cjk_font()}"
    return "未找到中文字体，界面上的中文会被省略（数字与英文不受影响）"
