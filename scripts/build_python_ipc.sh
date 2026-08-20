#!/usr/bin/env bash
# 把 IPC 服务打成自包含二进制，供 electron-builder 放进应用资源目录。
#
# 用独立的构建 venv，不碰仓库的 .venv —— 开发环境不该被打包工具污染。
# 构建环境必须是 3.11+：项目声明 requires-python >=3.11，系统自带的 3.9
# 会静默装不上（pip 报错但退出码仍是 0）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_VENV="${WYCKOFF_BUILD_VENV:-$REPO_ROOT/.build-venv}"
OUT_DIR="$REPO_ROOT/dist/python"

# 优先用仓库 venv 的解释器：它的版本已经被开发流程验证过。
BASE_PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$BASE_PYTHON" ]]; then
  BASE_PYTHON="$(command -v python3.11 || command -v python3)"
fi

version="$("$BASE_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$version" in
  3.1[1-9]|3.[2-9][0-9]) ;;
  *) echo "构建需要 Python 3.11+，当前是 ${version}（${BASE_PYTHON}）" >&2; exit 1 ;;
esac

echo "==> 构建环境：$BUILD_VENV （基于 $BASE_PYTHON, ${version}）"
if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
  "$BASE_PYTHON" -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/pip" install -q --upgrade pip
"$BUILD_VENV/bin/pip" install -q pyinstaller "$REPO_ROOT"

echo "==> 打包"
rm -rf "$OUT_DIR"
"$BUILD_VENV/bin/pyinstaller" \
  --clean --noconfirm \
  --distpath "$OUT_DIR" \
  --workpath "$REPO_ROOT/dist/pywork" \
  "$REPO_ROOT/packaging/wyckoff-ipc.spec"

BIN="$OUT_DIR/wyckoff-ipc/wyckoff-ipc"
[[ -x "$BIN" ]] || { echo "产物缺失：$BIN" >&2; exit 1; }

# 冒烟：打包成功不等于能跑。协议和调度入口都真跑一次，失败就别让它进安装包。
echo "==> 冒烟测试"
reply="$(printf '%s\n' '{"id":"smoke","method":"health","params":{}}' '__shutdown__' | "$BIN" 2>/dev/null | head -1)"
case "$reply" in
  *'"type": "ready"'*) ;;
  *) echo "冒烟失败，首行不是 ready：$reply" >&2; exit 1 ;;
esac

daemon_reply="$("$BIN" --daemon --status 2>/dev/null)"
case "$daemon_reply" in
  daemon:*) echo "==> OK  $(du -sh "$OUT_DIR/wyckoff-ipc" | cut -f1)  $BIN" ;;
  *) echo "调度入口冒烟失败：$daemon_reply" >&2; exit 1 ;;
esac
