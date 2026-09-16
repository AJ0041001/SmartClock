#!/usr/bin/env python3
"""模型验证工具 —— 逐步确认 best.rknn 在板子上能正确工作。

这是"一步一步来"的第一步：在任何业务代码之前，先把模型本身跑通。

检查项（逐项独立，失败会明确指出卡在哪一步）
--------------------------------------------
    1. librknnrt.so 能否找到并加载
    2. best.rknn 能否载入
    3. NPU 运行时能否初始化          ← 最可能失败的一步
    4. 输入/输出张量形状是否符合预期
    5. 能否完成一次真实推理
    6. 输出数值是否合理（非全零、非 NaN）
    7. 在有真实照片时，能否检出卡片

用法::

    # 基础验证（不需要照片）
    python3 scripts/verify_model.py

    # 用真实照片验证检测效果
    python3 scripts/verify_model.py --image assets/captures/card1.jpg

    # 指定模型路径
    python3 scripts/verify_model.py --model ../lbm/model/best.rknn

    # 只做环境诊断，不初始化 NPU
    python3 scripts/verify_model.py --env-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.cli import banner  # noqa: E402
from src.yolo import (  # noqa: E402
    decode_predictions as _decode_predictions,
    letterbox as _letterbox,
)
from src.rknn_ctypes import (  # noqa: E402
    RKNN_NPU_CORE_0_1_2,
    RKNNLite,
    RknnError,
    find_librknnrt,
    selftest_diagnostics,
)

#: 期望的模型规格（来自 Visual/lbm/README.md）
EXPECTED_INPUT_SHAPE = (1, 640, 640, 3)
EXPECTED_OUTPUT_SHAPE = (1, 5, 8400)
EXPECTED_CLASSES = ["card"]

DEFAULT_MODEL = (
    Path(__file__).resolve().parent.parent.parent / "lbm" / "model" / "best.rknn"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_model.py",
        description="逐步验证 RKNN 模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--model", default=str(DEFAULT_MODEL),
                        help=f"模型路径（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--image", action="append", default=None,
                        help="用真实照片验证，可重复指定多张")
    parser.add_argument("--conf", type=float, default=0.5,
                        help="置信度阈值（默认 0.5）")
    parser.add_argument("--nms", type=float, default=0.45,
                        help="NMS 阈值（默认 0.45）")
    parser.add_argument("--env-only", action="store_true",
                        help="只做环境诊断，不初始化 NPU")
    parser.add_argument("--save-dir", default="assets/output",
                        help="结果图保存目录")
    parser.add_argument("--dump-raw", action="store_true",
                        help="打印输出张量的原始统计（排查用）")
    parser.add_argument("--pass-through", type=int, default=0, choices=[0, 1],
                        help="输入直通模式。默认 0（正确）；"
                             "设 1 可用于复现 NaN 问题以确认诊断")
    parser.add_argument("--box-format", default="auto",
                        choices=["auto", "xywh", "xyxy"],
                        help="框输出格式。默认 auto 自动判别；"
                             "ultralytics 导出通常是 xywh（中心+宽高）")
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────────────
# 预处理 / 后处理
#
# 真正的实现在 src/yolo.py —— 这里只是薄适配层。
# 刻意不在这里重复写一份：验证工具和正式检测器用同一套逻辑，
# 才能保证"验证时对"等于"上线时对"。两份实现是 bug 的温床。
# ──────────────────────────────────────────────────────────────────────


def letterbox(image: np.ndarray, size: int = 640
              ) -> tuple[np.ndarray, float, int, int]:
    """等比缩放 + 灰边补齐。

    返回 (处理后的图, 缩放比例, 上边距, 左边距)。
    实现委托给 :func:`src.yolo.letterbox`。
    """
    canvas, info = _letterbox(image, size)
    return canvas, info.scale, info.top, info.left


def decode_outputs(output: np.ndarray, conf: float, nms: float,
                   scale: float, top: int, left: int,
                   orig_shape: tuple[int, int],
                   box_format: str = "auto",
                   ) -> list[tuple[tuple[int, int, int, int], float]]:
    """把 (1, 5, 8400) 的原始输出解码成原图坐标系下的检测框。

    实现委托给 :func:`src.yolo.decode_predictions`，这里只做返回值的
    格式适配（``DetectedBox`` → ``(bbox, score)`` 元组）。

    ⚠️ 关于框格式 —— 本项目的关键坑之一
    ----------------------------------
    ultralytics 导出 ONNX 时，网络原始输出是 **cxcywh**
    （中心 x, 中心 y, 宽, 高），**不是 xyxy**。仓库 README 写成 xyxy 是错的。

    实测证据：本模型在合成图上输出的最高分候选是
        (322.8, 318.5, 165.6, 159.2)
    而合成图中红色方块的真实位置（letterbox 后）是
        中心 (320, 320)、尺寸 160 x 160
    按 cxcywh 解读完全吻合；按 xyxy 解读则 x2<x1 且 y2<y1，几何上不可能。

    用错格式的表现很隐蔽：程序能跑通、有置信度，
    但画出来的框完全错位、算出的中心也是错的。
    """
    from src.yolo import LetterboxInfo

    boxes = _decode_predictions(
        output,
        conf_threshold=conf,
        nms_threshold=nms,
        info=LetterboxInfo(
            scale=scale, top=top, left=left, input_size=640
        ),
        orig_shape=orig_shape,
        box_format=box_format,
    )
    return [((b.x1, b.y1, b.x2, b.y2), b.score) for b in boxes]


# ──────────────────────────────────────────────────────────────────────
# 验证流程
# ──────────────────────────────────────────────────────────────────────


class Checker:
    """记录各项检查结果。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.warned = 0

    def ok(self, title: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  ✅ {title}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")

    def fail(self, title: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  ❌ {title}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")

    def warn(self, title: str, detail: str = "") -> None:
        self.warned += 1
        print(f"  ⚠️  {title}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")

    def summary(self) -> int:
        print()
        print("═" * 70)
        print(f"  通过 {self.passed} 项   失败 {self.failed} 项   "
              f"警告 {self.warned} 项")
        print("═" * 70)
        return 1 if self.failed else 0


def main() -> int:
    args = parse_args()
    checker = Checker()

    print(banner("RKNN 模型验证", "逐步确认模型在板子上能否正确运行"))
    print()

    # ── 步骤 0：环境诊断 ────────────────────────────────────────────
    print("【步骤 0】环境诊断")
    print(selftest_diagnostics())
    print()

    lib_path = find_librknnrt()
    if lib_path:
        checker.ok("找到 librknnrt.so", lib_path)
    else:
        checker.fail(
            "找不到 librknnrt.so",
            "解决办法（任选其一）：\n"
            "  sudo cp Visual/lbm/runtime/librknnrt.so /usr/lib/ && sudo ldconfig\n"
            "  或 export RKNN_RT_PATH=/绝对路径/librknnrt.so",
        )
        return checker.summary()

    if args.env_only:
        return checker.summary()

    # ── 步骤 1：模型文件 ────────────────────────────────────────────
    print("【步骤 1】模型文件")
    model_path = Path(args.model)
    if not model_path.exists():
        checker.fail(f"模型不存在：{model_path}")
        return checker.summary()

    size_mb = model_path.stat().st_size / 1048576
    checker.ok(f"模型文件存在：{model_path.name}（{size_mb:.1f} MB）")
    print()

    rknn = RKNNLite(verbose=True)
    try:
        # ── 步骤 2：载入模型 ────────────────────────────────────────
        print("【步骤 2】载入模型")
        try:
            rknn.load_rknn(model_path)
            checker.ok("模型载入内存成功")
        except RknnError as exc:
            checker.fail(f"模型载入失败：{exc}")
            return checker.summary()
        print()

        # ── 步骤 3：初始化 NPU 运行时 ───────────────────────────────
        print("【步骤 3】初始化 NPU 运行时")
        print("  （这一步最容易出问题：需要 /dev/rknpu 可访问、")
        print("    驱动版本与 librknnrt.so 兼容）")
        try:
            rknn.init_runtime(core_mask=RKNN_NPU_CORE_0_1_2)
            checker.ok("NPU 运行时初始化成功（3 核）")
        except RknnError as exc:
            checker.fail(
                f"NPU 运行时初始化失败：{exc}",
                "排查方向：\n"
                "  · 确认当前用户能访问 /dev/rknpu（ls -l /dev/rknpu）\n"
                "  · 确认 NPU 驱动已加载（dmesg | grep -i rknpu）\n"
                "  · 确认 librknnrt.so 版本与驱动匹配\n"
                "  · 在容器/沙盒里运行时，需要把 /dev/rknpu 映射进去",
            )
            return checker.summary()
        print()

        # ── 步骤 4：张量形状 ────────────────────────────────────────
        print("【步骤 4】输入输出张量形状")
        try:
            n_input, n_output = rknn.query_io_num()
            checker.ok(f"输入 {n_input} 个，输出 {n_output} 个")

            in_attrs = rknn.get_input_attrs()
            for info in in_attrs:
                print(f"       输入 {info.describe()}")

            out_attrs = rknn.get_output_attrs()
            for info in out_attrs:
                print(f"       输出 {info.describe()}")

            if in_attrs and tuple(in_attrs[0].dims) == EXPECTED_INPUT_SHAPE:
                checker.ok(f"输入形状符合预期 {EXPECTED_INPUT_SHAPE}")
            else:
                checker.warn(
                    f"输入形状 {tuple(in_attrs[0].dims) if in_attrs else '?'} "
                    f"与预期 {EXPECTED_INPUT_SHAPE} 不符",
                    "若不同，预处理里的 letterbox 尺寸需要相应调整",
                )

            if out_attrs and tuple(out_attrs[0].dims) == EXPECTED_OUTPUT_SHAPE:
                checker.ok(f"输出形状符合预期 {EXPECTED_OUTPUT_SHAPE}")
            else:
                checker.warn(
                    f"输出形状 {tuple(out_attrs[0].dims) if out_attrs else '?'} "
                    f"与预期 {EXPECTED_OUTPUT_SHAPE} 不符",
                    "若不同，后处理的解码逻辑需要相应调整",
                )
        except RknnError as exc:
            checker.fail(f"查询张量属性失败：{exc}")
            return checker.summary()
        print()

        # ── 步骤 5：推理 ────────────────────────────────────────────
        print("【步骤 5】执行一次真实推理")

        test_images: list[tuple[str, np.ndarray]] = []

        if args.image:
            for path_str in args.image:
                path = Path(path_str)
                if not path.exists():
                    checker.warn(f"照片不存在，跳过：{path}")
                    continue
                image = cv2.imread(str(path))
                if image is None:
                    checker.warn(f"照片无法解码，跳过：{path}")
                    continue
                test_images.append((str(path), image))
        else:
            # 没有真实照片时，用合成图先验证"推理链路能跑通"
            synthetic = np.full((480, 640, 3), 20, dtype=np.uint8)
            cv2.rectangle(synthetic, (240, 160), (400, 320), (0, 0, 200), -1)
            test_images.append(("(合成图：黑底红方块)", synthetic))
            print("  未提供 --image，使用合成图验证推理链路")

        if not test_images:
            checker.fail("没有可用的测试图像")
            return checker.summary()

        total_time = 0.0
        for name, image in test_images:
            canvas, scale, top, left = letterbox(image)
            rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
            batch = rgb[None, ...]           # (1, 640, 640, 3) uint8

            try:
                start = time.monotonic()
                outputs = rknn.inference(
                    inputs=[batch],
                    data_format="nhwc",
                    inputs_pass_through=[args.pass_through],
                )
                elapsed = time.monotonic() - start
                total_time += elapsed
            except RknnError as exc:
                checker.fail(f"推理失败（{name}）：{exc}")
                return checker.summary()

            output = outputs[0]
            print(f"\n  ── {name} ──")
            print(f"     推理耗时 {elapsed * 1000:.1f} ms")
            print(f"     输出 shape={output.shape}  dtype={output.dtype}")
            print(f"     输入模式 pass_through={args.pass_through}")

            if args.dump_raw:
                print("     ── 原始输出统计 ──")
                print(f"       NaN={int(np.isnan(output).sum())}  "
                      f"Inf={int(np.isinf(output).sum())}  "
                      f"总元素={output.size}")
                finite_vals = output[np.isfinite(output)]
                if finite_vals.size:
                    print(f"       有限值范围 {finite_vals.min():.4f} ~ "
                          f"{finite_vals.max():.4f}  "
                          f"均值 {finite_vals.mean():.4f}")
                if output.ndim == 3:
                    boxes = output[0, :4, :]
                    scores = output[0, 4, :]
                    print(f"       框坐标范围 {boxes.min():.2f} ~ "
                          f"{boxes.max():.2f}")
                    print(f"       置信度范围 {scores.min():.4f} ~ "
                          f"{scores.max():.4f}")
                    top_idx = np.argsort(scores)[-5:][::-1]
                    print("       置信度最高的 5 个候选：")
                    for i in top_idx:
                        b = output[0, :4, i]
                        print(f"         [{i:>4}] conf={scores[i]:.4f}  "
                              f"box=({b[0]:.1f},{b[1]:.1f},"
                              f"{b[2]:.1f},{b[3]:.1f})")

            # ── 数值合理性 ──
            #
            # 判据要分层次，不能一刀切：
            #   · 低置信度候选里冒出几个 Inf —— 是 YOLO 原始输出的正常现象
            #     （dist2bbox 在极端预测下会溢出），只要不进入最终检测就无害
            #   · 高置信度候选里出现非有限值 —— 严重，会污染检测结果
            #   · 大面积非有限值 —— 系统性问题（如输入模式错误）
            nan_count = int(np.isnan(output).sum())
            inf_count = int(np.isinf(output).sum())
            nonfinite = nan_count + inf_count
            total = output.size

            bad_high = 0
            if output.ndim == 3 and output.shape[1] == 5:
                scores_row = output[0, 4, :]
                boxes_rows = output[0, :4, :]
                high_mask = scores_row > args.conf
                if high_mask.any():
                    bad_high = int(
                        (~np.isfinite(boxes_rows[:, high_mask])).sum()
                        + (~np.isfinite(scores_row[high_mask])).sum()
                    )

            finite_vals = output[np.isfinite(output)]
            has_signal = finite_vals.size > 0 and \
                bool(np.abs(finite_vals).max() > 1e-6)

            if nonfinite == 0 and has_signal:
                checker.ok(
                    f"输出数值合理（范围 {output.min():.3f} ~ "
                    f"{output.max():.3f}）"
                )
            elif nonfinite / max(1, total) > 0.5:
                checker.fail(
                    "输出大面积非有限值（系统性错误）",
                    "\n".join([
                        f"NaN={nan_count}  Inf={inf_count}  总元素={total}",
                        "",
                        "这几乎总是输入模式的问题：本模型输入是 float16，"
                        "且归一化 (std=255)",
                        "烘焙在 RKNN 内部，必须让 RKNN 自己做转换"
                        "（pass_through=0）。",
                        "若传成 1，NPU 会把 uint8 字节当成半精度浮点解读。",
                    ]),
                )
            elif bad_high > 0:
                checker.fail(
                    "高置信度候选里含非有限值",
                    f"NaN={nan_count}  Inf={inf_count}  "
                    f"其中落在阈值以上的有 {bad_high} 个\n"
                    f"这会让检测框坐标算错，必须解决。",
                )
            elif nonfinite > 0:
                checker.warn(
                    f"低置信度候选里有 {nonfinite} 个非有限值"
                    f"（NaN={nan_count} Inf={inf_count}，占 "
                    f"{nonfinite / total * 100:.3f}%）",
                    "这是 YOLO 原始输出的常见现象：极端预测下坐标溢出。\n"
                    "只要不出现在阈值以上的候选里就不影响检测，已在解码时剔除。",
                )
            else:
                checker.fail("输出无有效信号（全为零）")

            # 置信度分布
            if output.ndim == 3 and output.shape[1] == 5:
                scores = output[0, 4, :]
                above = int((scores > args.conf).sum())
                print(f"     置信度：最高 {scores.max():.3f}  "
                      f"中位 {np.median(scores):.3f}  "
                      f"超过 {args.conf} 的候选 {above} 个")

            # 后处理
            try:
                detections = decode_outputs(
                    output, args.conf, args.nms, scale, top, left,
                    image.shape[:2],
                    box_format=args.box_format,
                )
            except Exception as exc:
                checker.fail(f"后处理失败：{exc}")
                continue

            if detections:
                print(f"     检测结果：{len(detections)} 个")
                annotated = image.copy()
                for index, (bbox, score) in enumerate(detections):
                    x1, y1, x2, y2 = bbox
                    cv2.rectangle(annotated, (x1, y1), (x2, y2),
                                  (0, 255, 0), 2)
                    label = f"{EXPECTED_CLASSES[0]} {score:.2f}"
                    cv2.putText(annotated, label, (x1, max(20, y1 - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (0, 255, 0), 2, cv2.LINE_AA)
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    cv2.drawMarker(annotated, (cx, cy), (0, 0, 255),
                                   cv2.MARKER_CROSS, 20, 2)
                    print(f"       #{index + 1}  conf={score:.3f}  "
                          f"bbox=({x1},{y1},{x2},{y2})  中心=({cx},{cy})")

                save_dir = Path(args.save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                # 文件名安全化：合成图的名字带中文和括号，直接拼进路径
                # 会得到 verify_(合成图：黑底红方块).jpg 这种难敲的名字，
                # 终端里补全都费劲。统一转成 ASCII 安全的短名。
                stem = Path(name).stem or "image"
                if "(合成图" in name:
                    stem = "synthetic"
                safe_stem = "".join(
                    ch if (ch.isascii() and (ch.isalnum() or ch in "-_"))
                    else "_"
                    for ch in stem
                ).strip("_") or "image"
                out_path = save_dir / f"verify_{safe_stem}.jpg"
                cv2.imwrite(str(out_path), annotated)
                print(f"     已保存标注图：{out_path}")
                if "合成图" in name:
                    print(f"     （文件名已做 ASCII 化，原始名称：{name}）")
                checker.ok(f"检出 {len(detections)} 个目标")
            else:
                if "(合成图" in name:
                    checker.warn(
                        "合成图未检出目标",
                        "合成图是纯色方块，与真实红卡的纹理/边缘差异较大，\n"
                        "检不出属于正常范围。真正的判据是接入真实照片后的表现。",
                    )
                else:
                    checker.warn(
                        f"未检出卡片（conf>{args.conf}）",
                        "可以尝试：降低 --conf；或确认照片里确实是红卡；"
                        "或检查信纸背景是否与训练时一致",
                    )

        if len(test_images) > 1:
            print(f"\n  平均推理耗时 {total_time / len(test_images) * 1000:.1f} ms")

    finally:
        rknn.release()

    print()
    print("【结论】")
    if checker.failed == 0:
        print("  ✅ 模型在板子上工作正常，可以进行下一步（接入流水线）。")
    else:
        print("  ❌ 有检查项失败，请先按上面的提示解决。")

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
