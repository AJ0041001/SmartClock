#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  创建便捷命令 —— 以后不用再敲长路径
# ══════════════════════════════════════════════════════════════════════
#  作用：在 ~/.local/bin/ 下生成几个短命令，从任何目录都能直接调用。
#
#  用法：
#      bash tools/make_shortcuts.sh
#
#  生成的命令：
#      serial-ping     串口通讯测试
#      virtual-stm32   虚拟 STM32
#      card-verify     模型验证
#      card-validate   数据集定量评估
#      card-preview    实时预览
#      card-run        主程序
#      card-check      环境检查
#      color-block     进入项目目录（需用 source 方式加载）
#
#  卸载：
#      bash tools/make_shortcuts.sh --remove
# ══════════════════════════════════════════════════════════════════════

set -euo pipefail

# 自动定位项目目录（本脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 可以用 CB_BIN_DIR 覆盖输出目录（测试时有用）
BIN_DIR="${CB_BIN_DIR:-$HOME/.local/bin}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }

# ─────────────────────────────────────────────────────────────────────
# 卸载
# ─────────────────────────────────────────────────────────────────────
remove_shortcuts() {
    echo "── 移除便捷命令 ──"
    local removed=0
    for name in serial-ping virtual-stm32 cam-view card-verify \
                card-validate card-preview card-run card-start \
                card-check card-pick card-diagnose cb; do
        if [ -e "$BIN_DIR/$name" ]; then
            rm -f "$BIN_DIR/$name"
            ok "已移除 $name"
            removed=$((removed + 1))
        fi
    done
    if [ "$removed" -eq 0 ]; then
        warn "没有找到需要移除的命令"
    fi
    echo
    echo "完成，共移除 $removed 个。"
}

if [ "${1:-}" = "--remove" ]; then
    remove_shortcuts
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────
# 安装
# ─────────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════════════════════"
echo "  创建便捷命令"
echo "══════════════════════════════════════════════════════════════"
echo
echo "  项目目录：$PROJECT_DIR"
echo "  命令目录：$BIN_DIR"
echo

if [ ! -d "$PROJECT_DIR/scripts" ]; then
    err "项目结构不对，找不到 $PROJECT_DIR/scripts"
    exit 1
fi

mkdir -p "$BIN_DIR"

# 生成一个包装脚本
#   参数1: 命令名
#   参数2: 目标脚本的相对路径
#   参数3: 说明
make_wrapper() {
    local name="$1"
    local target="$2"
    local desc="$3"
    local wrapper="$BIN_DIR/$name"

    cat > "$wrapper" <<EOF
#!/bin/bash
# $desc
# 由 tools/make_shortcuts.sh 自动生成，请勿手工编辑
exec python3 "$PROJECT_DIR/$target" "\$@"
EOF
    chmod +x "$wrapper"
    ok "$(printf '%-16s' "$name") $desc"
}

echo "── 生成命令 ──"
make_wrapper "serial-ping"    "examples/serial_ping.py"    "串口通讯测试"
make_wrapper "virtual-stm32"  "examples/virtual_stm32.py"  "虚拟 STM32（无硬件测试）"
make_wrapper "cam-view"       "examples/camera_view.py"    "摄像头查看器（只看画面）"
make_wrapper "card-verify"    "scripts/verify_model.py"    "模型验证"
make_wrapper "card-validate"  "scripts/validate_dataset.py" "数据集定量评估"
make_wrapper "card-preview"   "scripts/preview.py"         "实时预览与标定"
make_wrapper "card-run"       "scripts/main.py"            "主程序（追踪并发送）"
make_wrapper "card-check"     "scripts/camera_check.py"    "摄像头诊断"
make_wrapper "card-pick"      "scripts/color_pick.py"      "取色标定（HSV 引擎用）"
make_wrapper "card-diagnose"  "scripts/diagnose_model.py"  "模型诊断"

# card-start 是一键启动脚本（bash，不是 python），单独生成
cat > "$BIN_DIR/card-start" <<EOF
#!/bin/bash
# 一键启动：检查环境 → 启动主程序（带实时画面）
exec bash "$PROJECT_DIR/tools/start.sh" "\$@"
EOF
chmod +x "$BIN_DIR/card-start"
ok "$(printf '%-16s' 'card-start') 一键启动（检查 + 运行）"

# card-check 默认需要参数，给个友好的默认值
cat > "$BIN_DIR/card-check" <<EOF
#!/bin/bash
# 摄像头诊断（默认 --probe-all）
if [ \$# -eq 0 ]; then
    exec python3 "$PROJECT_DIR/scripts/camera_check.py" --probe-all
fi
exec python3 "$PROJECT_DIR/scripts/camera_check.py" "\$@"
EOF
chmod +x "$BIN_DIR/card-check"

# 进入项目目录的快捷方式（需要 source 才能改变当前 shell 的目录）
cat > "$BIN_DIR/cb" <<EOF
#!/bin/bash
# 打印项目目录路径。用 source cb 可以真正切过去：
#     source cb        （或 . cb）
if [ "\${BASH_SOURCE[0]}" = "\$0" ]; then
    echo "$PROJECT_DIR"
    echo
    echo "提示：用 source 才能真正切换目录："
    echo "    source cb"
else
    cd "$PROJECT_DIR" || return 1
    echo "已进入 $PROJECT_DIR"
fi
EOF
chmod +x "$BIN_DIR/cb"
ok "$(printf '%-16s' 'cb') 项目目录快捷方式（用 source cb 切换）"

echo

# ─────────────────────────────────────────────────────────────────────
# 检查 PATH
# ─────────────────────────────────────────────────────────────────────
echo "── 检查 PATH ──"
case ":$PATH:" in
    *":$BIN_DIR:"*)
        ok "$BIN_DIR 已在 PATH 中"
        ;;
    *)
        warn "$BIN_DIR 不在 PATH 中"
        echo
        echo "  需要把它加进 PATH。执行下面这条命令即可："
        echo
        echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
        echo "      source ~/.bashrc"
        echo
        echo "  或者临时生效（当前终端有效）："
        echo
        echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo
echo "══════════════════════════════════════════════════════════════"
echo -e "${GREEN}完成${NC}"
echo "══════════════════════════════════════════════════════════════"
echo
echo "现在可以从任何目录直接调用，例如："
echo
echo "    serial-ping --list              # 列出串口"
echo "    serial-ping --probe             # 自动找串口"
echo "    virtual-stm32                   # 启动虚拟 STM32"
echo "    card-verify                     # 验证模型"
echo "    card-validate --sheet           # 数据集评估"
echo "    source cb                       # 进入项目目录"
echo
echo "查看全部命令：ls $BIN_DIR"
echo
