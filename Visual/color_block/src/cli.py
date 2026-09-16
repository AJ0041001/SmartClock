"""命令行公共设施 —— 参数解析、日志、配置加载。

把各脚本重复的部分收敛到这里，保证所有工具的交互风格一致。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import AppConfig

#: 项目根目录（color_block/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)-24s | %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """统一日志配置。"""
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO
    logging.basicConfig(
        level=numeric,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        stream=sys.stderr,
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """给子命令挂上通用参数。"""
    parser.add_argument(
        "-c", "--config",
        default=str(DEFAULT_CONFIG),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG.name}）",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别，默认取配置文件里的 debug.log_level",
    )


def load_config(args: argparse.Namespace) -> AppConfig:
    """按命令行参数加载配置，并把常用项从参数覆盖进去。"""
    config = AppConfig.load(args.config)

    # 命令行覆盖配置文件：便于临时试参数而不用改文件
    overrides = (
        ("camera", "index"),
        ("camera", "width"),
        ("camera", "height"),
        ("camera", "fps"),
        ("camera", "fourcc"),
        ("detector", "preset"),
        ("detector", "min_area"),
        ("mapping", "roi_x"),
        ("mapping", "roi_y"),
        ("mapping", "roi_w"),
        ("mapping", "roi_h"),
        ("mapping", "fixed_y"),
        ("mapping", "invert_x"),
        ("mapping", "invert_y"),
        ("mapping", "swap_xy"),
        ("serial", "port"),
        ("serial", "baudrate"),
        ("serial", "timeout"),
    )
    for section, field in overrides:
        cli_name = f"{section}_{field}"
        if hasattr(args, cli_name):
            value = getattr(args, cli_name)
            if value is not None:
                setattr(getattr(config, section), field, value)

    if args.log_level:
        config.debug.log_level = args.log_level

    return config


def add_camera_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--camera-index", type=int, default=None,
                        help="摄像头设备号，对应 /dev/videoN")
    parser.add_argument("--camera-width", type=int, default=None)
    parser.add_argument("--camera-height", type=int, default=None)
    parser.add_argument("--camera-fps", type=int, default=None)
    parser.add_argument("--camera-fourcc", default=None,
                        choices=["MJPG", "YUYV", "YUY2"])


def add_detector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--detector-preset", default=None,
                        help="预设色：red/green/blue/yellow/orange/purple/"
                             "cyan/magenta/custom")
    parser.add_argument("--detector-min-area", type=float, default=None,
                        help="最小色块面积（像素²）")


def add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mapping-roi-x", type=int, default=None)
    parser.add_argument("--mapping-roi-y", type=int, default=None)
    parser.add_argument("--mapping-roi-w", type=int, default=None)
    parser.add_argument("--mapping-roi-h", type=int, default=None)
    parser.add_argument("--mapping-fixed-y", type=int, default=None)
    parser.add_argument("--mapping-invert-x", action="store_true", default=None)
    parser.add_argument("--mapping-invert-y", action="store_true", default=None)
    parser.add_argument("--mapping-swap-xy", action="store_true", default=None)


def add_serial_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial-port", default=None,
                        help="串口设备，如 /dev/ttyS3；auto 表示自动探测")
    parser.add_argument("--serial-baudrate", type=int, default=None,
                        help="波特率，须与 STM32 侧一致")
    parser.add_argument("--serial-timeout", type=float, default=None)


def banner(title: str, subtitle: str = "") -> str:
    """生成统一的标题栏。"""
    width = 66
    lines = ["═" * width, f"  {title}"]
    if subtitle:
        lines.append(f"  {subtitle}")
    lines.append("═" * width)
    return "\n".join(lines)


def report_config_problems(config: AppConfig) -> int:
    """打印配置问题。返回问题数量，便于脚本决定是否退出。"""
    problems = config.validate()
    if not problems:
        return 0
    print("\n⚠️  配置存在问题：", file=sys.stderr)
    for index, problem in enumerate(problems, 1):
        print(f"   {index}. {problem}", file=sys.stderr)
    print(file=sys.stderr)
    return len(problems)
