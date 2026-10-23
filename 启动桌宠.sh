#!/usr/bin/env bash
# DeepSeek 桌宠 Linux 桌面启动脚本

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 检查 Python 环境
if command -v python3 &>/dev/null; then
    PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_CMD="python"
else
    echo "错误：未检测到 Python 解释器，请先安装 Python 3.10+"
    exit 1
fi

# 检查依赖项
$PY_CMD -c "import PySide6" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "提示：未检测到 PySide6，尝试从 requirements.txt 安装..."
    $PY_CMD -m pip install -r requirements.txt
fi

# 在 Wayland 环境下，若未指定平台则优先使用 xcb (XWayland) 以保证置顶/透明/自由定位等桌宠窗口特性稳定
if [ "$XDG_SESSION_TYPE" = "wayland" ] && [ -z "$QT_QPA_PLATFORM" ]; then
    export QT_QPA_PLATFORM=xcb
fi

echo "正在启动 DeepSeek 桌宠..."
exec $PY_CMD desktop_pet.py "$@"
