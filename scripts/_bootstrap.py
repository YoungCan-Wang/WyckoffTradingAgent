"""Make repo-root imports work when scripts are executed by path."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 包导入只需要补齐路径；仅 `python scripts/*.py` 的顶层 `_bootstrap`
# 才加载本地配置，避免测试收集或库导入读取真实凭证。
if __name__ == "_bootstrap":
    from dotenv import load_dotenv

    load_dotenv(Path(ROOT) / ".env")
