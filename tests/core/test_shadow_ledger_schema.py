"""影子账本 DDL 以 Python 常量版本化（仓库禁止 .sql），生成的语句必须可执行。"""

from __future__ import annotations

from core.shadow_ledger_schema import SEED_ACCOUNT_ID, build_ddl, table_names


def test_all_five_tables_are_emitted() -> None:
    ddl = build_ddl()

    for name in table_names():
        assert f"create table if not exists public.{name} (" in ddl
    assert len(table_names()) == 5


def test_ddl_is_idempotent() -> None:
    """迁移会被重复执行（本地、CI、线上各一次），不能覆盖已有数据。"""
    ddl = build_ddl()

    assert ddl.count("if not exists") == len(table_names())
    assert "on conflict (account_id) do nothing" in ddl
    assert "drop table" not in ddl.lower()


def test_column_comments_do_not_swallow_commas() -> None:
    """`col spec  -- note,` 会把逗号吞进注释，生成无法执行的 SQL。"""
    for line in build_ddl().splitlines():
        stripped = line.strip()
        if "--" not in stripped or stripped.startswith("--"):
            continue
        before = stripped.split("--", 1)[0].rstrip()
        # 列定义行的逗号必须在注释之前
        assert not before.endswith(("text", "numeric", "int", "date", "jsonb")) or before.endswith(","), line


def test_rls_enabled_on_every_table() -> None:
    ddl = build_ddl()

    for name in table_names():
        assert f"alter table public.{name} enable row level security;" in ddl


def test_seed_account_is_shadow_only() -> None:
    """写入侧会断言 USER_SHADOW: 前缀；seed 账户必须同样合规，否则一启用就报错。"""
    assert SEED_ACCOUNT_ID.startswith("USER_SHADOW:")
    # 只看可执行语句：注释里提到 USER_LIVE 是在说明隔离关系，不是引用实盘表。
    statements = [line for line in build_ddl().splitlines() if not line.strip().startswith("--")]
    assert not [line for line in statements if "USER_LIVE" in line]


def test_no_live_tables_referenced() -> None:
    ddl = build_ddl()

    for live in ("portfolios", "trade_orders", "daily_nav", "positions "):
        assert f"references public.{live}" not in ddl
