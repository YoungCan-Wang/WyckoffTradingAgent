"""打印 theme_structure_cross_daily 建表语句，供人工在 Supabase SQL Editor 执行。

本项目不保留 .sql 文件，且 Supabase Python SDK 走 REST 不支持 DDL，
故 schema 以 core/theme_structure_cross_schema.py 的常量形式版本化并由此脚本输出。

用法::

    python scripts/print_theme_structure_cross_ddl.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from core.theme_structure_cross_schema import build_ddl


def main() -> int:
    print(build_ddl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
