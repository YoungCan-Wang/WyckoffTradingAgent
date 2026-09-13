"""打印 daily_nav 当日盈亏列的 ALTER，供人工在 Supabase SQL Editor 执行。

用法::

    python scripts/print_daily_nav_ddl.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from core.daily_nav_schema import build_ddl


def main() -> int:
    print(build_ddl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
