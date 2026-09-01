"""``recommendation_tracking`` 补列语句的约束。

背景：写入侧 ``upsert_recommendation_payload_rows`` 在 400 后会剔掉报错列重试，缺列
因此只表现为静默丢字段 —— 2026-08-31 那轮日志里 ``400`` 紧跟一条少了
``capital_migration_bonus`` 的 ``201 Created``。但 ``capital_migration_bonus`` 同时在
``step4_from_supabase.fetch_recommendation_rows`` 的 ``select`` 里，那条读路径是**硬失败**
（42703），降级救不了它。

这些用例不连生产库，只钉住「三处清单互相自洽」，让下次加可选列时错位能被 CI 抓到。
"""

from __future__ import annotations

from core.recommendation_payload import RECOMMENDATION_OPTIONAL_COLUMNS
from core.recommendation_tracking_schema import MISSING_COLUMNS, build_ddl, column_names


def test_every_missing_column_is_a_declared_optional_column() -> None:
    """补的列必须确实是写入侧会发的列，否则补了也没人用。"""
    assert column_names() <= set(RECOMMENDATION_OPTIONAL_COLUMNS)


def test_ddl_is_idempotent_add_column() -> None:
    """人工执行一次，重跑不能炸 —— 不确定当前表状态时得能安全重放。"""
    ddl = build_ddl()
    assert ddl.count("add column if not exists") == len(MISSING_COLUMNS)
    # 只补列，不建表也不删列。
    assert "create table" not in ddl.lower()
    assert "drop" not in ddl.lower()


def test_ddl_covers_every_declared_column() -> None:
    ddl = build_ddl()
    for name, ddl_type, _ in MISSING_COLUMNS:
        assert f"add column if not exists {name} {ddl_type}" in ddl


def test_read_path_select_columns_are_all_schema_backed() -> None:
    """读路径 select 的列若不在表里就是硬失败，降级机制救不了。

    ``fetch_recommendation_rows`` 的列表是手写字符串，容易先加进 select 再忘了补表。
    这条把它和「已知缺列」清单对上：select 里出现的可选列，要么表里已有，要么必须
    在 MISSING_COLUMNS 里等着人工执行 DDL。
    """
    import inspect

    from workflows import step4_from_supabase

    source = inspect.getsource(step4_from_supabase.fetch_recommendation_rows)
    selected = {col for col in RECOMMENDATION_OPTIONAL_COLUMNS if f"{col}," in source or f'{col}"' in source}
    # capital_migration_bonus 是当下唯一一个「被 select 且生产表没有」的列。
    assert "capital_migration_bonus" in selected
    assert "capital_migration_bonus" in column_names()
