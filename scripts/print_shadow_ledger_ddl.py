"""打印影子账本五张表的建表语句，供人工在 Supabase SQL Editor 执行一次。

本项目不保留 .sql 文件，且 Supabase Python SDK 走 REST 不支持 DDL，
故 schema 以 core/shadow_ledger_schema.py 的常量形式版本化并由此脚本输出。

执行完这段 DDL 后，把 SHADOW_LEDGER_ENABLED=1 才会真正开始记账。

用法::

    python scripts/print_shadow_ledger_ddl.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from core.shadow_ledger_schema import build_ddl


def main() -> int:
    print(build_ddl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
