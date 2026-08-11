#!/usr/bin/env bash
set -euo pipefail

# Wyckoff Trading Agent — 一键安装脚本
# curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash

PACKAGE="youngcan-wyckoff-analysis"
INSTALL_DIR="$HOME/.wyckoff/venv"
BIN_DIR="$HOME/.local/bin"
MIN_PYTHON="3.11"

info()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m==>\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m==>\033[0m %s\n" "$*"; }
err()   { printf "\033[1;31m==>\033[0m %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. 检测 Python
# ---------------------------------------------------------------------------
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(find_python) || {
    err "未找到 Python $MIN_PYTHON+。请先安装：
  macOS:  brew install python@3.11
  Ubuntu: sudo apt install python3.11
  其他:   https://www.python.org/downloads/"
}

info "使用 Python: $PYTHON ($($PYTHON --version 2>&1))"

# ---------------------------------------------------------------------------
# 2. 检测 / 安装 uv
# ---------------------------------------------------------------------------
if ! command -v uv &>/dev/null; then
    info "安装 uv（Python 包管理器）..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        err "uv 安装失败，请手动安装: https://docs.astral.sh/uv/"
    fi
fi

info "使用 uv: $(uv --version)"

# ---------------------------------------------------------------------------
# 3. 创建 venv 并安装
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR" ]; then
    info "更新已有安装..."
else
    info "创建虚拟环境: $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    uv venv --python "$PYTHON" "$INSTALL_DIR"
fi

info "安装 ${PACKAGE}[browser]..."
uv pip install --python "$INSTALL_DIR/bin/python" --upgrade "${PACKAGE}[browser]"
info "安装 Playwright Chromium（browser_research）..."
if ! "$INSTALL_DIR/bin/python" -m playwright install chromium; then
    warn "Chromium 安装失败：浏览器搜索暂不可用，可稍后执行 wyckoff update 重试"
fi

# ---------------------------------------------------------------------------
# 4. 创建 symlink
# ---------------------------------------------------------------------------
mkdir -p "$BIN_DIR"

WYCKOFF_BIN="$INSTALL_DIR/bin/wyckoff"
LINK_PATH="$BIN_DIR/wyckoff"

if [ -f "$WYCKOFF_BIN" ]; then
    ln -sf "$WYCKOFF_BIN" "$LINK_PATH"
    ok "已链接: $LINK_PATH → $WYCKOFF_BIN"
else
    err "安装异常：未找到 $WYCKOFF_BIN"
fi

# ---------------------------------------------------------------------------
# 5. 检查 PATH
# ---------------------------------------------------------------------------
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    warn "$BIN_DIR 不在 PATH 中，请添加到 shell 配置："
    SHELL_NAME=$(basename "$SHELL")
    case "$SHELL_NAME" in
        zsh)  echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
        bash) echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
        *)    echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
    echo ""
fi

# ---------------------------------------------------------------------------
# 6. 完成
# ---------------------------------------------------------------------------
ok "安装完成！"
echo ""
echo "  启动:   wyckoff"
echo "  升级:   wyckoff update   # 含 [browser] + Chromium"
echo "  浏览器: TUI 内输入 /browser 查看 Chrome CDP 状态"
echo "  卸载:   rm -rf ~/.wyckoff ~/.local/bin/wyckoff"
echo ""
