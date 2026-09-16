#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  一键启动 —— 每次运行只需要这一条命令
# ══════════════════════════════════════════════════════════════════════
#  它会自动完成：
#     1. 检查摄像头是否就绪
#     2. 检查串口是否就绪、权限是否够
#     3. 检查配置文件有没有问题
#     4. 启动主程序（带实时画面显示）
#
#  用法：
#      bash tools/start.sh              # 正常启动（带画面）
#      bash tools/start.sh --dry-run    # 空跑，不接串口
#      bash tools/start.sh --no-preview # 不显示画面（省 CPU）
#      bash tools/start.sh --check      # 只检查，不启动
# ══════════════════════════════════════════════════════════════════════

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || exit 1

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
info() { echo -e "  ${CYAN}·${NC} $*"; }

CHECK_ONLY=0
EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        *) EXTRA_ARGS+=("$arg") ;;
    esac
done

echo "══════════════════════════════════════════════════════════════"
echo "  SmartClock 视觉追踪 —— 启动检查"
echo "══════════════════════════════════════════════════════════════"
echo

PROBLEMS=0

# ─────────────────────────────────────────────────────────────────────
# 1. 摄像头
# ─────────────────────────────────────────────────────────────────────
echo "── 摄像头 ──"
CAM_INDEX=$(PYTHONPATH="$PROJECT_DIR" python3 -c "
from src.config import AppConfig
print(AppConfig.load('config.yaml').camera.index)
" 2>/dev/null || echo 0)

CAM_DEV="/dev/video$CAM_INDEX"
if [ -e "$CAM_DEV" ]; then
    CAM_NAME=""
    [ -r "/sys/class/video4linux/video$CAM_INDEX/name" ] && \
        CAM_NAME=$(cat "/sys/class/video4linux/video$CAM_INDEX/name")
    ok "$CAM_DEV 存在  $CAM_NAME"

    if [ -r "$CAM_DEV" ] && [ -w "$CAM_DEV" ]; then
        ok "权限正常"
    else
        warn "权限不足（但通常摄像头是 video 组，一般没问题）"
    fi
else
    bad "$CAM_DEV 不存在"
    info "可用设备：$(ls /dev/video* 2>/dev/null | tr '\n' ' ' || echo '无')"
    info "解决办法：插好摄像头，或改 config.yaml 里的 camera.index"
    PROBLEMS=$((PROBLEMS + 1))
fi
echo

# ─────────────────────────────────────────────────────────────────────
# 2. 串口
# ─────────────────────────────────────────────────────────────────────
echo "── 串口 ──"
SERIAL_PORTS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ')
if [ -n "$SERIAL_PORTS" ]; then
    ok "发现：$SERIAL_PORTS"

    # 检查第一个能不能读写
    FIRST_PORT=$(echo "$SERIAL_PORTS" | awk '{print $1}')
    if [ -r "$FIRST_PORT" ] && [ -w "$FIRST_PORT" ]; then
        ok "$FIRST_PORT 可读写"
    else
        bad "$FIRST_PORT 权限不足（Permission denied）"
        echo
        echo "     原因：当前用户不在 dialout 组"
        echo
        echo "     永久解决（执行后需重新登录或重启）："
        echo "         sudo usermod -aG dialout \$USER"
        echo
        echo "     立刻生效（当前终端）："
        echo "         newgrp dialout"
        echo
        echo "     临时放开（重启后失效）："
        echo "         sudo chmod 666 $FIRST_PORT"
        PROBLEMS=$((PROBLEMS + 1))
    fi
else
    bad "没有找到 USB 串口设备（/dev/ttyUSB* 或 /dev/ttyACM*）"
    info "检查：USB-TTL 转换器插好了吗？"
    info "注意：板载 UART 默认在设备树里是 disabled，用不了"
    PROBLEMS=$((PROBLEMS + 1))
fi
echo

# ─────────────────────────────────────────────────────────────────────
# 3. 配置文件
# ─────────────────────────────────────────────────────────────────────
echo "── 配置 ──"
CONFIG_OUT=$(PYTHONPATH="$PROJECT_DIR" python3 -c "
from src.config import AppConfig
c = AppConfig.load('config.yaml')
problems = c.validate()
if problems:
    for p in problems:
        print('PROBLEM:' + p)
else:
    print('OK')
print('ENGINE:' + c.detector.engine)
print('SERIAL:' + c.serial.port)
print('ROI:%d,%d,%d,%d' % (c.mapping.roi_x, c.mapping.roi_y,
                            c.mapping.roi_w, c.mapping.roi_h))
" 2>&1)

if echo "$CONFIG_OUT" | grep -q "^OK"; then
    ok "config.yaml 校验通过"
else
    echo "$CONFIG_OUT" | grep "^PROBLEM:" | sed 's/^PROBLEM:/     ✗ /'
    PROBLEMS=$((PROBLEMS + 1))
fi

ENGINE=$(echo "$CONFIG_OUT" | grep "^ENGINE:" | cut -d: -f2)
SERIAL_CFG=$(echo "$CONFIG_OUT" | grep "^SERIAL:" | cut -d: -f2)
ROI=$(echo "$CONFIG_OUT" | grep "^ROI:" | cut -d: -f2)
info "检测引擎：$ENGINE"
info "串口配置：$SERIAL_CFG"
info "映射 ROI：$ROI"
echo

# ─────────────────────────────────────────────────────────────────────
# 4. 模型文件
# ─────────────────────────────────────────────────────────────────────
echo "── 模型 ──"
MODEL="../lbm/model/best.rknn"
if [ -f "$MODEL" ]; then
    ok "best.rknn 存在（$(du -h "$MODEL" | cut -f1)）"
else
    bad "找不到 $MODEL"
    PROBLEMS=$((PROBLEMS + 1))
fi
echo

# ─────────────────────────────────────────────────────────────────────
# 结论
# ─────────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════════════════════"
if [ "$PROBLEMS" -gt 0 ]; then
    echo -e "  ${RED}发现 $PROBLEMS 个问题${NC}，请先解决上面的 ✗ 项"
    echo "══════════════════════════════════════════════════════════════"
    exit 1
fi

echo -e "  ${GREEN}全部就绪${NC}"
echo "══════════════════════════════════════════════════════════════"
echo

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "（--check 模式，不启动）"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────────────
echo "启动主程序…"
echo
echo "  窗口出现后："
echo "    按 r  → 在画面里点两下，框出卡片会移动到的范围（A 映射区）"
echo "    按 0  → B 检测区自动 = A 向外扩一圈"
echo "    按 s  → 保存       按 q → 退出"
echo "  （画面上也会显示这套指引）"
echo

exec python3 scripts/main.py "${EXTRA_ARGS[@]}"
