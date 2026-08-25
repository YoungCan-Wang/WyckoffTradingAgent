"""影子账本（paper ledger）五张表的建表语句。

本项目不保留 .sql 文件（scripts/quality_gate.py 会拦），且 Supabase Python SDK 走 REST
不支持 DDL、环境也无 psycopg2/asyncpg 与连接串，故 schema 以 Python 常量形式版本化，
由 scripts/print_shadow_ledger_ddl.py 打印后人工在 SQL Editor 执行一次。
与 core/factor_ic_schema.py 同一套做法。

所有表只服务 ``USER_SHADOW:*`` 账户，与 USER_LIVE 的 portfolios / positions /
trade_orders / daily_nav 完全隔离。
"""

from __future__ import annotations

from core.constants import (
    TABLE_SHADOW_ACCOUNT,
    TABLE_SHADOW_EVENTS,
    TABLE_SHADOW_NAV_DAILY,
    TABLE_SHADOW_POSITIONS,
    TABLE_SHADOW_TRADE_PLANS,
)

_ACCOUNT_COLUMNS = (
    ("account_id", "text primary key", "只接受 USER_SHADOW: 前缀，写入侧另有断言"),
    ("cash", "numeric not null default 100000", "可用现金"),
    ("equity", "numeric not null default 100000", "总权益 = 现金 + 持仓市值"),
    ("market_value", "numeric not null default 0", "持仓市值"),
    ("initial_capital", "numeric not null default 100000", "初始资金，算累计收益的基准"),
    ("as_of", "date", "最近一次结算的交易日"),
    ("updated_at", "timestamptz not null default now()", ""),
)

_POSITION_COLUMNS = (
    ("account_id", f"text not null references public.{TABLE_SHADOW_ACCOUNT} (account_id)", ""),
    ("code", "text not null", ""),
    ("name", "text not null default ''", ""),
    ("shares", "int not null default 0 check (shares >= 0)", ""),
    ("sellable_shares", "int not null default 0", "T+1：当日买入不可卖"),
    ("avg_cost", "numeric not null default 0", ""),
    ("buy_dt", "date", ""),
    ("last_mark", "numeric", "最近一次盯市价"),
    ("stop_loss", "numeric", ""),
    ("opened_at", "timestamptz not null default now()", ""),
)

_EVENT_COLUMNS = (
    ("event_key", "text primary key", "幂等键，重跑同一交易日不会重复记账"),
    ("account_id", f"text not null references public.{TABLE_SHADOW_ACCOUNT} (account_id)", ""),
    ("as_of", "date not null", ""),
    ("code", "text not null", ""),
    ("name", "text not null default ''", ""),
    ("event_type", "text not null", "buy / sell / mark 等"),
    ("price", "numeric", ""),
    ("qty", "int not null default 0", ""),
    ("fees", "jsonb not null default '{}'", "佣金/印花税/过户费分项，便于复算"),
    ("reason", "text not null default ''", ""),
    ("payload", "jsonb not null default '{}'", ""),
    ("created_at", "timestamptz not null default now()", ""),
)

_NAV_COLUMNS = (
    ("account_id", f"text not null references public.{TABLE_SHADOW_ACCOUNT} (account_id)", ""),
    ("as_of", "date not null", ""),
    ("cash", "numeric not null default 0", ""),
    ("market_value", "numeric not null default 0", ""),
    ("equity", "numeric not null default 0", ""),
    ("pnl_day", "numeric not null default 0", ""),
    ("pnl_total", "numeric not null default 0", ""),
)

_PLAN_COLUMNS = (
    ("plan_key", "text primary key", "幂等键"),
    ("account_id", f"text not null references public.{TABLE_SHADOW_ACCOUNT} (account_id)", ""),
    ("code", "text not null", ""),
    ("name", "text not null default ''", ""),
    ("action", "text not null", ""),
    ("status", "text not null default 'planned'", "planned / filled / expired"),
    ("signal_date", "date not null", "信号产生日；成交必须发生在其之后，杜绝前瞻"),
    ("entry_mode", "text not null default 'next_open'", ""),
    ("suggested_price", "numeric", ""),
    ("stop_price", "numeric", ""),
    ("shares_hint", "int not null default 0", ""),
    ("reason", "text not null default ''", ""),
    ("trigger_date", "date", ""),
    ("entry_date", "date", ""),
    ("entry_price", "numeric", ""),
    ("fill_reason", "text not null default ''", ""),
    ("created_at", "timestamptz not null default now()", ""),
    ("updated_at", "timestamptz not null default now()", ""),
)

TABLES: tuple[tuple[str, tuple[tuple[str, str, str], ...], str], ...] = (
    (TABLE_SHADOW_ACCOUNT, _ACCOUNT_COLUMNS, ""),
    (TABLE_SHADOW_POSITIONS, _POSITION_COLUMNS, "primary key (account_id, code)"),
    (TABLE_SHADOW_EVENTS, _EVENT_COLUMNS, ""),
    (TABLE_SHADOW_NAV_DAILY, _NAV_COLUMNS, "primary key (account_id, as_of)"),
    (TABLE_SHADOW_TRADE_PLANS, _PLAN_COLUMNS, ""),
)

SEED_ACCOUNT_ID = "USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928"
SEED_CAPITAL = 100000


def table_names() -> tuple[str, ...]:
    return tuple(name for name, _cols, _extra in TABLES)


def build_ddl() -> str:
    """幂等 DDL：可重复执行，不会覆盖已有数据。"""
    blocks: list[str] = [
        "-- 影子账本（paper ledger）。与 USER_LIVE 的持仓/订单/净值完全隔离。",
        "-- 由 core/shadow_ledger_schema.py 生成，请勿手改；改 schema 请改那个模块。",
        "",
    ]
    for name, columns, extra in TABLES:
        lines = [f"create table if not exists public.{name} ("]
        # 注释必须跟在逗号之后：`col spec  -- note,` 会把逗号吞进注释，生成无效 SQL。
        parts = [f"  {col} {spec}" for col, spec, _note in columns]
        if extra:
            parts.append(f"  {extra}")
        notes = [note for _col, _spec, note in columns] + ([""] if extra else [])
        body = []
        for idx, (text, note) in enumerate(zip(parts, notes, strict=True)):
            tail = "," if idx < len(parts) - 1 else ""
            body.append(f"{text}{tail}" + (f"  -- {note}" if note else ""))
        lines.append("\n".join(body))
        lines.append(");")
        blocks.append("\n".join(lines))
        blocks.append("")
    for name in table_names():
        blocks.append(f"alter table public.{name} enable row level security;")
    blocks.append("")
    blocks.append(
        f"insert into public.{TABLE_SHADOW_ACCOUNT} "
        "(account_id, cash, equity, market_value, initial_capital)\nvalues (\n"
        f"  '{SEED_ACCOUNT_ID}',\n  {SEED_CAPITAL},\n  {SEED_CAPITAL},\n  0,\n  {SEED_CAPITAL}\n)\n"
        "on conflict (account_id) do nothing;"
    )
    return "\n".join(blocks).rstrip() + "\n"
