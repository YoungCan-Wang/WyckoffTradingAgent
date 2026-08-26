#!/usr/bin/env bash
# 兼容已有本地命令；实际逻辑在跨平台 Python 脚本里，Windows CI 也走同一路径。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$BASE_PYTHON" ]]; then
  BASE_PYTHON="$(command -v python3.11 || command -v python3)"
fi
exec "$BASE_PYTHON" "$REPO_ROOT/scripts/build_python_ipc.py"
