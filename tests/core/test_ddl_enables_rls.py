"""新表必须随建表语句一起开启 RLS。

Supabase 把 public 表的全部权限默认授予 anon / authenticated，不开 RLS 的表等于对持有公开
anon key 的任何人开放读写删。线上审计发现的四张无 RLS 表都出自「建表 DDL 里漏了这一句」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import factor_ic_schema, review_capture_schema, review_shadow_lane_schema, shadow_ledger_schema

_ROOT = Path(__file__).resolve().parents[2]
_CREATE = re.compile(r"create table if not exists public\.(\w+)", re.IGNORECASE)


@pytest.mark.parametrize(
    "module",
    [factor_ic_schema, review_capture_schema, review_shadow_lane_schema, shadow_ledger_schema],
    ids=lambda module: module.__name__.rsplit(".", 1)[-1],
)
def test_builder_enables_rls_on_every_table_it_creates(module) -> None:
    ddl = module.build_ddl()
    created = _CREATE.findall(ddl)

    assert created
    for name in created:
        assert f"alter table public.{name} enable row level security;" in ddl


def test_every_python_ddl_source_mentions_rls() -> None:
    """新增建表 DDL 的模块忘了开 RLS 时，在这里就会被点名，而不是等线上审计。"""
    offenders = []
    for folder in ("core", "integrations", "scripts"):
        for path in sorted((_ROOT / folder).glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if _CREATE.search(text) and "enable row level security" not in text.lower():
                offenders.append(str(path.relative_to(_ROOT)))

    assert offenders == []
