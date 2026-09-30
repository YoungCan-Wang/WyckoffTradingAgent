"""打印持仓表写策略加固 DDL：写操作必须同时满足「本人」与「有效星球会员」。

背景：`/api/portfolio` 有星球会员 403，但 `portfolios` / `portfolio_positions` 的 RLS
此前只校验 `split_part(portfolio_id, ':', 2) = auth.uid()::text`。浏览器里的
anon key + 用户 JWT 可直接 PostgREST 写入，绕过 API 门控（#499 也关不掉这条）。

本项目不保留一次性 .sql 文件；在 Supabase SQL Editor 执行本脚本输出即可。

用法::

    python scripts/print_portfolio_rls_membership_ddl.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from core.portfolio_rls_membership import build_portfolio_write_rls_ddl


def main() -> int:
    print(build_portfolio_write_rls_ddl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
