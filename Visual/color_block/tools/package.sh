#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  分类打包 —— 把交付内容整理成发布压缩包
# ══════════════════════════════════════════════════════════════════════
#  产出：
#      dist/SmartClock-视觉端-<日期>.tar.gz     完整包（含数据集，体积大）
#      dist/SmartClock-视觉端-<日期>-lite.tar.gz  精简包（不含数据集与图片）
#      dist/MANIFEST.txt                        包内每个文件的用途
#
#  包内按类别分目录：
#      01_程序代码      运行所需的全部代码
#      02_配置          配置文件
#      03_模型          训练好的 RKNN / ONNX 模型
#      04_数据集        标注数据（仅完整包含）
#      05_文档          说明与排查手册
#      06_测试          单元测试
#      07_工具          安装、启动、验证、打包脚本
#      08_示例          最小可运行示例
#
#  用法：
#      bash tools/package.sh              # 打包
#      bash tools/package.sh --dry-run    # 只列出会打进去什么，不真的打包
#      bash tools/package.sh --no-dataset # 跳过数据集（加快速度）
# ══════════════════════════════════════════════════════════════════════

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || exit 1

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
info() { echo -e "  ${CYAN}·${NC} $*"; }

DRY_RUN=0
WITH_DATASET=1
for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=1 ;;
        --no-dataset) WITH_DATASET=0 ;;
        -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
        *) warn "忽略未知参数：$arg" ;;
    esac
done

STAMP="$(date +%Y%m%d)"
NAME="SmartClock-视觉端-${STAMP}"
STAGE="dist/${NAME}"
DIST="dist"

# 模型目录（与 Visual/color_block 同级）
MODEL_DIR="../lbm/model"

echo "── 分类打包 ──"
echo "  项目：$PROJECT_DIR"
echo "  输出：${DIST}/${NAME}.tar.gz"
[ "$WITH_DATASET" -eq 1 ] && echo "  数据集：包含" || echo "  数据集：跳过"
echo

# ── 复制辅助 ─────────────────────────────────────────────────────────
COPIED_FILES=0
COPIED_DIRS=0

copy_file() {                       # copy_file <源> <目标目录>
    local src="$1" dst="$2"
    if [ ! -f "$src" ]; then
        bad "缺少文件：$src"
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    %s\n' "$dst/$(basename "$src")"
        return 0
    fi
    mkdir -p "$dst" && cp -p "$src" "$dst/" && COPIED_FILES=$((COPIED_FILES + 1))
}

copy_tree() {                       # copy_tree <源目录> <目标目录> <排除模式...>
    local src="$1" dst="$2"; shift 2
    if [ ! -d "$src" ]; then
        bad "缺少目录：$src"
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    %s/  ← %s\n' "$dst" "$src"
        return 0
    fi
    mkdir -p "$dst"
    local args=(-a)
    for pattern in "$@"; do args+=(--exclude="$pattern"); done
    # 用 . 作为源，避免把 src 目录本身再嵌一层
    ( cd "$src" && tar "${args[@]}" -cf - . ) | ( cd "$dst" && tar -xf - )
    COPIED_DIRS=$((COPIED_DIRS + 1))
}

# ── 清理上一轮 ───────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 0 ]; then
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
fi

# 注意：这里把"什么属于哪一类"写在一处，方便日后核对
EXCLUDES_COMMON=(__pycache__ '*.pyc' '*.pyo' '.pytest_cache' '.DS_Store')

echo "① 程序代码 → 01_程序代码/"
copy_tree src      "$STAGE/01_程序代码/src"     "${EXCLUDES_COMMON[@]}"
copy_tree scripts  "$STAGE/01_程序代码/scripts" "${EXCLUDES_COMMON[@]}"

echo "② 配置 → 02_配置/"
copy_file config.yaml "$STAGE/02_配置"

echo "③ 模型 → 03_模型/"
MODEL_OK=0
if [ -d "$MODEL_DIR" ]; then
    for f in best.rknn best.onnx; do
        if [ -f "$MODEL_DIR/$f" ]; then
            copy_file "$MODEL_DIR/$f" "$STAGE/03_模型" && MODEL_OK=1
        fi
    done
    # 训练权重单独放一层：体积大、日常运行用不到，只有重新训练才需要，
    # 所以跟随"是否包含数据集"一起决定 —— 精简包不带它。
    if [ "$WITH_DATASET" -eq 1 ] && [ -f "$MODEL_DIR/best.pt" ]; then
        copy_file "$MODEL_DIR/best.pt" "$STAGE/03_模型/训练权重" >/dev/null
    fi
    # NPU 运行时库 —— 没装驱动时靠它兜底，是"拷过去就能跑"的关键
    if [ -f "$MODEL_DIR/../runtime/librknnrt.so" ]; then
        copy_file "$MODEL_DIR/../runtime/librknnrt.so" "$STAGE/03_模型/runtime" >/dev/null
        ok "已包含 librknnrt.so（NPU 运行时，免装驱动也能跑）"
    else
        warn "没找到 librknnrt.so，板子上必须自己装 NPU 驱动"
    fi

    # 代码靠这两个相对路径自动找模型和运行时库：
    #     <项目>/Visual/lbm/model/best.rknn
    #     <项目>/Visual/lbm/runtime/librknnrt.so
    # 这里放软链接，解压后不用挪文件就能直接跑，也不重复占空间。
    if [ "$DRY_RUN" -eq 0 ] && [ "$MODEL_OK" -eq 1 ]; then
        mkdir -p "$STAGE/Visual/lbm"
        ln -sfn ../../03_模型          "$STAGE/Visual/lbm/model"
        ln -sfn ../../03_模型/runtime  "$STAGE/Visual/lbm/runtime"
        ok "已建软链接 Visual/lbm/{model,runtime} → 03_模型"
    fi
else
    warn "没找到模型目录 $MODEL_DIR（模型可以不进包，程序里配置路径即可）"
fi

echo "④ 数据集 → 04_数据集/"
if [ "$WITH_DATASET" -eq 1 ]; then
    copy_tree redcard.yolo26 "$STAGE/04_数据集/redcard.yolo26" \
        "${EXCLUDES_COMMON[@]}"
else
    info "已跳过"
fi

echo "⑤ 文档 → 05_文档/"
copy_file README.md "$STAGE/05_文档"
copy_tree docs "$STAGE/05_文档/docs" "${EXCLUDES_COMMON[@]}"

echo "⑥ 测试 → 06_测试/"
copy_tree tests "$STAGE/06_测试/tests" "${EXCLUDES_COMMON[@]}"

# 测试是按"项目根目录 = tests/ 的上一级"去定位 src/ scripts/ config.yaml
# 的。包里这些东西分了类，所以在这里补一组软链接，让测试解压后就能跑。
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$STAGE/06_测试"
    ln -sfn ../01_程序代码/src           "$STAGE/06_测试/src"
    ln -sfn ../01_程序代码/scripts       "$STAGE/06_测试/scripts"
    ln -sfn ../08_示例                   "$STAGE/06_测试/examples"
    ln -sfn ../05_文档/docs              "$STAGE/06_测试/docs"
    ln -sfn ../07_工具                   "$STAGE/06_测试/tools"
    ln -sfn ../02_配置/config.yaml       "$STAGE/06_测试/config.yaml"
    if [ "$WITH_DATASET" -eq 1 ]; then
        ln -sfn ../04_数据集/redcard.yolo26 "$STAGE/06_测试/redcard.yolo26"
    fi
    ok "已建软链接让测试能定位 src/scripts/examples/配置/数据集"
fi

echo "⑦ 工具 → 07_工具/"
copy_tree tools "$STAGE/07_工具" "${EXCLUDES_COMMON[@]}"

echo "⑧ 示例 → 08_示例/"
copy_tree examples "$STAGE/08_示例" "${EXCLUDES_COMMON[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    info "（--dry-run：以上只是预览，没有真的写文件）"
    exit 0
fi

# ── 顶层说明 ─────────────────────────────────────────────────────────
cat > "$STAGE/先读我.md" <<'EOF'
# SmartClock 视觉端 · 交付包

本包按类别分目录存放，每个目录的作用见下表。

| 目录 | 内容 |
|---|---|
| `01_程序代码/` | 运行所需的全部代码（`src/` 核心库 + `scripts/` 命令行工具） |
| `02_配置/`      | `config.yaml`，全部可调参数（带中文注释） |
| `03_模型/`      | 训练好的红卡检测模型（`best.rknn` 给板子用，`best.onnx` 备用） |
| `04_数据集/`    | 标注好的红卡数据集（train/valid/test） |
| `05_文档/`      | `README.md` + `docs/`（部署、标定、协议、验收排查） |
| `06_测试/`      | 459 个单元测试，全部离线可跑，不需要摄像头/NPU/单片机 |
| `07_工具/`      | 安装、一键启动、验证、打包脚本 |
| `08_示例/`      | 最小可运行示例（看摄像头、串口冒烟、虚拟 STM32） |
| `Visual/lbm/`   | 软链接，指向 `03_模型/` —— 代码靠这个相对路径自动找到模型和 NPU 运行时库 |
| `06_测试/` 下的软链接 | 让测试能定位到分类后的 `src/ scripts/ 配置/ 数据集/` |

## 怎么跑起来

解压后**可以直接运行**，不用挪文件（模型与运行时库靠 `Visual/lbm` 软链接接上）：

```bash
cd 01_程序代码
python3 -c "from src.card_detector import default_model_path; print(default_model_path())"
# 应该打印 .../Visual/lbm/model/best.rknn
python3 -c "from src.rknn_ctypes import find_librknnrt; print(find_librknnrt())"
# 应该打印 .../Visual/lbm/runtime/librknnrt.so

bash ../07_工具/start.sh --check     # 先检查环境
bash ../07_工具/start.sh --dry-run   # 空跑，不接单片机
bash ../07_工具/start.sh             # 正式运行
```

如果你用的解压工具**不支持软链接**（比如 Windows 上的某些压缩软件），
手动补一下就行：

```bash
mkdir -p Visual/lbm/model Visual/lbm/runtime
cp 03_模型/*.rknn 03_模型/*.onnx Visual/lbm/model/
cp 03_模型/runtime/librknnrt.so  Visual/lbm/runtime/
```

或者直接在 `config.yaml` 里写死模型路径：

```yaml
detector:
  model_path: /绝对路径/03_模型/best.rknn
```

## 最快确认"软件本身没问题"

不需要任何硬件：

```bash
cd 01_程序代码
PYTHONPATH=. python3 scripts/demo_offline.py
PYTHONPATH=. python3 -m unittest discover -s ../06_测试/tests -t ../06_测试
```

## 关键提醒

- **`detector.roi_margin` 必须大于红卡宽度的一半**，否则卡片贴边时会被
  裁掉一角，挡板到不了最边上。界面会自动报警（B 变红），按 `B +10` 放大。
- 串口用 **USB-TTL 转串口**（`/dev/ttyUSB0`）：板子设备树里 10 个片上
  UART 一个都没开。
- 需要把用户加入 `dialout` 组才能免 sudo 访问串口。
EOF

# ── MANIFEST ─────────────────────────────────────────────────────────
cat > "$DIST/MANIFEST.txt" <<EOF
SmartClock 视觉端 · 交付清单
生成时间：$(date '+%Y-%m-%d %H:%M:%S')
包名：${NAME}.tar.gz

════════════════════════════════════════════════════════════════════
一、运行必需（少一个都跑不起来）
════════════════════════════════════════════════════════════════════
01_程序代码/src/protocol.py        10字节帧 + CRC-8，与 STM32 固件逐比特一致
01_程序代码/src/serialport.py      纯 Python 串口（无 pyserial 依赖）
01_程序代码/src/camera.py          USB 摄像头采集
01_程序代码/src/yolo.py            letterbox + 检测输出解码
01_程序代码/src/rknn_ctypes.py     librknnrt.so 的 ctypes 绑定（NPU 推理）
01_程序代码/src/card_detector.py   YOLO/RKNN 红卡检测器
01_程序代码/src/detector.py        HSV 降级检测器 + 统一 Detection 结构
01_程序代码/src/mapper.py          坐标映射 + 平滑 + 死区
01_程序代码/src/pipeline.py        主流水线（依赖注入，可离线测试）
01_程序代码/src/roi_editor.py      双 ROI 编辑 + 画面控制面板
01_程序代码/src/ui_buttons.py      画面内按钮条/状态栏（自绘）
01_程序代码/src/text_cjk.py        中文渲染（Pillow）
01_程序代码/src/config.py          配置加载/校验/回写
01_程序代码/src/cli.py             命令行公共设施
01_程序代码/scripts/main.py        主程序入口
02_配置/config.yaml                全部可调参数
03_模型/best.rknn                  红卡检测模型（NPU 用）
03_模型/runtime/librknnrt.so       NPU 运行时库（板子没装驱动时靠它兜底）
Visual/lbm/model → 03_模型          软链接：代码靠这个相对路径找模型
Visual/lbm/runtime → 03_模型/runtime 软链接：代码靠这个相对路径找运行时库
06_测试/{src,scripts,examples,tools,config.yaml,redcard.yolo26}
                                   软链接：让测试定位到分类后的目录

════════════════════════════════════════════════════════════════════
二、现场标定与验收要用
════════════════════════════════════════════════════════════════════
01_程序代码/scripts/camera_check.py    摄像头诊断
01_程序代码/scripts/verify_model.py    模型单图验证（带真值对比）
01_程序代码/scripts/validate_dataset.py 数据集定量评估
01_程序代码/scripts/diagnose_model.py  模型/运行时故障诊断
01_程序代码/scripts/preview.py         仅预览
01_程序代码/scripts/color_pick.py      HSV 降级方案取色
01_程序代码/scripts/serial_test.py     串口分层自测
01_程序代码/scripts/demo_offline.py    离线演示（无需硬件）
01_程序代码/scripts/demo_edge_center.py 边缘中心精度演示（双 ROI 修好了什么）
07_工具/start.sh                       一键启动（检查 + 运行）
07_工具/install.sh                     依赖检查与安装
07_工具/make_shortcuts.sh              生成短命令
07_工具/run_all_tests.sh               一键跑全部验证
07_工具/crc_crosscheck/                CRC 与固件交叉验证

════════════════════════════════════════════════════════════════════
三、文档
════════════════════════════════════════════════════════════════════
05_文档/README.md                  总说明：这套东西做什么、怎么用
05_文档/docs/部署与接线.md          板子接线与部署步骤
05_文档/docs/标定指南.md            双 ROI 标定（A 映射 / B 检测）
05_文档/docs/串口协议.md            10 字节帧格式与 CRC 说明
05_文档/docs/验收与排查.md          逐项验收标准 + 分层排查手册
05_文档/docs/images/                界面截图（正常 / B 太小报警）

════════════════════════════════════════════════════════════════════
四、验证与数据（可选，不影响运行）
════════════════════════════════════════════════════════════════════
06_测试/                            459 个单元测试 + 测试替身
04_数据集/redcard.yolo26/           红卡数据集（229/63/32 张）
03_模型/best.onnx                   ONNX 中间产物（换框架/重新转换时用）
03_模型/训练权重/best.pt            训练好的权重（重新训练用；精简包不含）
08_示例/                            最小可运行示例

════════════════════════════════════════════════════════════════════
五、统计
════════════════════════════════════════════════════════════════════
$(cd "$STAGE" && find . -type f | wc -l | awk '{printf "文件总数：%s\n", $1}')
$(du -sh "$STAGE" 2>/dev/null | awk '{printf "解压后大小：%s\n", $1}')
EOF

# ── 打包 ─────────────────────────────────────────────────────────────
echo
echo "⑨ 压缩"
( cd "$DIST" && tar -czf "${NAME}.tar.gz" "$NAME" )
ok "${DIST}/${NAME}.tar.gz  ($(du -h "$DIST/${NAME}.tar.gz" | cut -f1))"

if [ "$WITH_DATASET" -eq 1 ]; then
    # 精简包 = "能跑起来" 的最小集合：去掉数据集、ONNX 中间产物和训练权重，
    # 保留代码、配置、best.rknn、NPU 运行时库、文档、测试与工具。
    LITE="${NAME}-lite"
    rm -rf "$DIST/$LITE"
    ( cd "$STAGE" && tar --exclude='04_数据集' \
                        --exclude='03_模型/best.onnx' \
                        --exclude='03_模型/训练权重' -cf - . ) \
        | ( mkdir -p "$DIST/$LITE" && cd "$DIST/$LITE" && tar -xf - )
    ( cd "$DIST" && tar -czf "${LITE}.tar.gz" "$LITE" )
    ok "${DIST}/${LITE}.tar.gz  ($(du -h "$DIST/${LITE}.tar.gz" | cut -f1))  ← 不含数据集/ONNX/训练权重，便于发送"
    rm -rf "$DIST/$LITE"
fi

# 顺便算一下校验和，便于核对传输是否完整
if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$DIST" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256" )
    ok "校验和：${DIST}/${NAME}.tar.gz.sha256"
fi

ok "清单：${DIST}/MANIFEST.txt"

# ── 自检：真解压一次，确认包是能用的 ─────────────────────────────────
echo
echo "⑩ 自检（解压到临时目录跑一遍）"
CHECK_DIR="$(mktemp -d)"
trap 'rm -rf "$CHECK_DIR"' EXIT
if tar -xzf "$DIST/${NAME}.tar.gz" -C "$CHECK_DIR" 2>/dev/null; then
    ok "解压成功"

    EXTRACTED="$CHECK_DIR/$NAME"
    FOUND=0

    # ① 代码能不能找到模型（靠 Visual/lbm/model 软链接）
    if [ -f "$EXTRACTED/Visual/lbm/model/best.rknn" ]; then
        ok "模型链接可用：Visual/lbm/model/best.rknn"
        FOUND=1
    else
        bad "模型链接断了 —— 解压后 Visual/lbm/model/best.rknn 不存在"
    fi

    # ② NPU 运行时库链接
    if [ -f "$EXTRACTED/Visual/lbm/runtime/librknnrt.so" ]; then
        ok "运行时库链接可用：Visual/lbm/runtime/librknnrt.so"
    else
        warn "运行时库链接断了（板子上装了驱动也能跑，只是没有兜底）"
    fi

    # ③ 代码能不能 import、能不能自检出模型路径
    if command -v python3 >/dev/null 2>&1 && [ "$FOUND" -eq 1 ]; then
        MODEL_RESOLVED="$(cd "$EXTRACTED/01_程序代码" && \
            PYTHONPATH=. python3 -c \
            'from src.card_detector import default_model_path; print(default_model_path())' \
            2>/dev/null || true)"
        if [ -n "$MODEL_RESOLVED" ] && [ -f "$MODEL_RESOLVED" ]; then
            ok "代码自动定位到模型：$(basename "$MODEL_RESOLVED")"
        else
            bad "代码没能定位到模型（default_model_path() 返回空）"
        fi

        LIB_RESOLVED="$(cd "$EXTRACTED/01_程序代码" && \
            PYTHONPATH=. python3 -c \
            'from src.rknn_ctypes import find_librknnrt; print(find_librknnrt() or "")' \
            2>/dev/null || true)"
        if [ -n "$LIB_RESOLVED" ] && [ -f "$LIB_RESOLVED" ]; then
            ok "代码自动定位到 NPU 运行时库"
        else
            warn "代码没找到 librknnrt.so（板子装了驱动就没问题）"
        fi

        # ④ 测试能不能在解压后的目录里跑起来
        TEST_LOG="$CHECK_DIR/tests.log"
        if ( cd "$EXTRACTED/01_程序代码" && PYTHONPATH=. python3 -m unittest \
                discover -s "../06_测试/tests" -t "../06_测试" \
                >"$TEST_LOG" 2>&1 ); then
            ok "包内测试全部通过（$(grep -oE 'Ran [0-9]+ tests' "$TEST_LOG" | head -1)）"
        else
            bad "包内测试没跑通 —— 见 $TEST_LOG"
            tail -15 "$TEST_LOG" | sed 's/^/      /'
        fi
    fi
else
    bad "解压失败 —— 压缩包可能损坏"
fi

echo
echo "包内结构："
if command -v tree >/dev/null 2>&1; then
    tree -L 2 -d "$STAGE"
else
    ( cd "$STAGE" && find . -maxdepth 2 -type d | sort | sed 's|^\./|  |' )
fi
echo
# ── 产出清单 ─────────────────────────────────────────────────────────
echo
echo "交付内容："
ls -lh "$DIST"/*.tar.gz "$DIST"/*.sha256 "$DIST"/MANIFEST.txt 2>/dev/null \
    | awk '{printf "  %-8s %s\n", $5, $9}'
echo
ok "打包完成（复制了 ${COPIED_FILES} 个文件，${COPIED_DIRS} 个目录）"
