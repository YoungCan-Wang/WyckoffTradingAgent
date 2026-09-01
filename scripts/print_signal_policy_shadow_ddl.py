"""打印 signal_policy_shadow_runs 的补列语句，供人工在 Supabase SQL Editor 执行。

本项目不保留 .sql 文件，且 Supabase Python SDK 走 REST 不支持 DDL,
故 schema 以 core/signal_policy_shadow_schema.py 的常量形式版本化并由此脚本输出。

执行完这段 DDL，影子账本才会重新有产出（自 2026-07-04 起因缺列静默丢行）。

用法::

    python scripts/print_signal_policy_shadow_ddl.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from core.signal_policy_shadow_schema import build_ddl


def main() -> int:
    print(build_ddl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
