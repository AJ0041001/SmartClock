#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  SmartClock 色块视觉 —— 环境检查 / 依赖安装
# ══════════════════════════════════════════════════════════════════════
#  用法：
#      bash tools/install.sh --check     只检查，不安装（推荐先跑这个）
#      bash tools/install.sh --install   安装缺失的依赖（需要 sudo）
#      bash tools/install.sh --info      打印硬件与系统信息
# ══════════════════════════════════════════════════════════════════════

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
info() { echo -e "  ${BLUE}·${NC} $*"; }

MISSING_PY=()
MISSING_SYS=()

# ─────────────────────────────────────────────────────────────────────
# Python 依赖检查
# ─────────────────────────────────────────────────────────────────────
check_python_deps() {
    echo "── Python 依赖 ──"

    if ! command -v python3 >/dev/null 2>&1; then
        bad "python3 未安装"
        return 1
    fi

    local pyver
    pyver="$(python3 --version 2>&1 | awk '{print $2}')"
    ok "python3 $pyver"

    # 逐项检查并输出实际版本号
    local -A modules=(
        [cv2]="opencv-python"
        [numpy]="numpy"
        [yaml]="PyYAML"
    )

    for mod in "${!modules[@]}"; do
        local pkg="${modules[$mod]}"
        local result
        result="$(python3 -c "import $mod; print(getattr($mod,'__version__','?'))" 2>/dev/null)"
        if [ -n "$result" ]; then
            ok "$pkg ($mod) $result"
        else
            bad "$pkg ($mod) 未安装"
            MISSING_PY+=("$pkg")
        fi
    done

    # 串口刻意不用 pyserial，这里说明一下，免得有人以为漏了
    info "串口使用标准库 termios 实现，不需要 pyserial（这是有意设计）"
    echo
}

# ─────────────────────────────────────────────────────────────────────
# 系统工具检查
# ─────────────────────────────────────────────────────────────────────
check_system_tools() {
    echo "── 系统工具 ──"

    local -A tools=(
        [v4l2-ctl]="v4l-utils    # 摄像头诊断（camera_check.py 会用）"
        [lsusb]="usbutils      # 查看 USB 设备"
        [gcc]="gcc            # CRC 交叉验证需要编译 C 代码"
    )

    for tool in "${!tools[@]}"; do
        local desc="${tools[$tool]}"
        if command -v "$tool" >/dev/null 2>&1; then
            ok "$tool"
        else
            warn "$tool 未安装 —— $(echo "$desc" | cut -d'#' -f2- | xargs)"
            MISSING_SYS+=("${desc%% *}")
        fi
    done
    echo
}

# ─────────────────────────────────────────────────────────────────────
# 硬件检查
# ─────────────────────────────────────────────────────────────────────
check_hardware() {
    echo "── 硬件 ──"

    local model="未知"
    if [ -r /proc/device-tree/model ]; then
        model="$(tr -d '\0' < /proc/device-tree/model)"
    fi
    info "板卡型号：$model"

    local arch
    arch="$(uname -m)"
    if [ "$arch" = "aarch64" ]; then
        ok "架构 aarch64（ARM64）"
    else
        warn "架构 $arch —— 本项目针对 aarch64 编写，其他架构也能跑但未验证"
    fi

    # 摄像头
    local video_count=0
    if compgen -G "/dev/video*" >/dev/null 2>&1; then
        video_count=$(find /dev -maxdepth 1 -name 'video*' 2>/dev/null | wc -l)
        ok "摄像头节点：$video_count 个"
        for dev in /dev/video*; do
            local name=""
            local sysname="/sys/class/video4linux/$(basename "$dev")/name"
            [ -r "$sysname" ] && name=" ($(cat "$sysname"))"
            info "  $dev$name"
        done
    else
        warn "没有 /dev/video* —— USB 摄像头可能没插"
        info "  插上后运行：python3 scripts/camera_check.py --probe-all"
    fi

    # 串口
    echo
    echo "  串口设备："
    local serial_found=0
    for pattern in /dev/ttyUSB* /dev/ttyACM* /dev/ttyS* /dev/ttyAMA*; do
        for dev in $pattern; do
            [ -e "$dev" ] || continue
            ok "$dev"
            serial_found=1
        done
    done
    if [ "$serial_found" -eq 0 ]; then
        warn "没有可用的 /dev/ttyS* 或 /dev/ttyUSB*"
        info "  板载 UART 默认在设备树里是 disabled —— 详见 docs/部署与接线.md"
        info "  最简方案：用 USB-TTL 转换器，插上就有 /dev/ttyUSB0"
    fi

    # 用户组
    echo
    if id -nG 2>/dev/null | grep -qw dialout; then
        ok "当前用户在 dialout 组（可访问串口）"
    else
        warn "当前用户不在 dialout 组 —— 串口会 Permission denied"
        info "  解决：sudo usermod -aG dialout \$USER   然后注销重新登录"
    fi

    # 板载 UART 状态（鲁班猫特有）
    if [ -d /proc/device-tree ]; then
        echo
        echo "  板载 UART 设备树状态："
        local enabled=0
        for node in /proc/device-tree/serial@*; do
            [ -d "$node" ] || continue
            local status
            status="$(tr -d '\0' < "$node/status" 2>/dev/null)"
            if [ "$status" = "okay" ]; then
                enabled=$((enabled + 1))
            fi
        done
        if [ "$enabled" -eq 0 ]; then
            warn "所有板载 UART 都是 disabled —— 需要设备树插件才能用"
            info "  详见 docs/部署与接线.md 的"方案 B""
        else
            ok "$enabled 个 UART 已启用"
        fi
    fi
    echo
}

# ─────────────────────────────────────────────────────────────────────
# 项目自检
# ─────────────────────────────────────────────────────────────────────
check_project() {
    echo "── 项目文件 ──"

    local required=(
        "src/protocol.py"
        "src/serialport.py"
        "src/camera.py"
        "src/detector.py"
        "src/mapper.py"
        "src/pipeline.py"
        "src/config.py"
        "config.yaml"
        "scripts/main.py"
    )

    local missing=0
    for rel in "${required[@]}"; do
        if [ -f "$PROJECT_DIR/$rel" ]; then
            :
        else
            bad "缺失：$rel"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -eq 0 ]; then
        ok "核心文件齐全"
    fi

    # 导入自检
    if (cd "$PROJECT_DIR" && PYTHONPATH=. python3 -c "
import sys
from src import protocol, serialport, camera, detector, mapper, pipeline, config
" 2>/dev/null); then
        ok "所有模块可正常导入"
    else
        bad "模块导入失败，请检查 Python 依赖"
    fi
    echo
}

# ─────────────────────────────────────────────────────────────────────
# 安装
# ─────────────────────────────────────────────────────────────────────
do_install() {
    echo "── 安装缺失依赖 ──"
    echo

    if [ "${#MISSING_SYS[@]}" -gt 0 ]; then
        echo "需要安装的系统包：${MISSING_SYS[*]}"
        echo "执行：sudo apt-get install -y ${MISSING_SYS[*]}"
        echo
        read -r -p "现在执行？[y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            sudo apt-get update
            sudo apt-get install -y "${MISSING_SYS[@]}"
        fi
    fi

    if [ "${#MISSING_PY[@]}" -gt 0 ]; then
        echo
        echo "缺失的 Python 包：${MISSING_PY[*]}"
        echo "建议优先用 apt 安装（避免破坏系统的 OpenCV）："
        echo "  sudo apt-get install -y python3-opencv python3-numpy python3-yaml"
        echo
        read -r -p "现在执行？[y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            sudo apt-get install -y python3-opencv python3-numpy python3-yaml
        fi
    fi

    if [ "${#MISSING_SYS[@]}" -eq 0 ] && [ "${#MISSING_PY[@]}" -eq 0 ]; then
        echo "所有依赖都已满足，无需安装。"
    fi
}

# ─────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────
main() {
    local mode="${1:---check}"

    echo "══════════════════════════════════════════════════════════════"
    echo "  SmartClock 色块视觉 —— 环境检查"
    echo "══════════════════════════════════════════════════════════════"
    echo

    check_python_deps
    check_system_tools

    if [ "$mode" = "--info" ]; then
        check_hardware
        exit 0
    fi

    check_hardware
    check_project

    echo "══════════════════════════════════════════════════════════════"
    if [ "${#MISSING_PY[@]}" -eq 0 ] && [ "${#MISSING_SYS[@]}" -eq 0 ]; then
        echo -e "${GREEN}✅ 环境检查通过，可以直接运行。${NC}"
        echo
        echo "建议下一步："
        echo "  python3 scripts/demo_offline.py      # 离线验证（无需硬件）"
    else
        echo -e "${YELLOW}⚠️  有缺失项，见上方标记。${NC}"
        echo
        echo "运行以下命令安装："
        echo "  bash tools/install.sh --install"
    fi
    echo "══════════════════════════════════════════════════════════════"

    if [ "$mode" = "--install" ]; then
        echo
        do_install
    fi
}

main "$@"
