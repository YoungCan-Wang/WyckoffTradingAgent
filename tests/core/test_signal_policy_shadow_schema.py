"""``signal_policy_shadow_runs`` 补列语句与写入侧的自洽约束。

背景：影子账本停在 2026-07-01 整两个月无产出，归因重算一直报
``insufficient_shadow_sample``，一直被读成「门槛太严」。真因是 2026-07-04
``_policy_shadow_row`` 新增了 ``attribution_signal_weights`` /
``attribution_policy_meta`` 两个键而生产表没跟上：此后每次 upsert 都 42703，
且 ``upsert_policy_shadow_run`` 是 ``raise_on_error=False``，异常只进
``logger.warning``，日志那行还写着「已写入」。

和 ``recommendation_tracking`` 那次的关键差别：那张表有「剔掉报错列重试」的降级，
缺列只丢字段；这张表没有，缺列丢整行。所以同一种漂移在这里后果重一个量级，值得
把「写入侧发的键」和「schema 清单」在 CI 里钉死。

这些用例不连生产库。
"""

from __future__ import annotations

import inspect
import re

from core.signal_policy_shadow_schema import MISSING_COLUMNS, build_ddl, column_names


def test_ddl_is_idempotent_add_column() -> None:
    """人工执行一次，重跑不能炸 —— 不确定当前表状态时得能安全重放。"""
    ddl = build_ddl()
    assert ddl.count("add column if not exists") == len(MISSING_COLUMNS)
    assert "create table" not in ddl.lower()
    assert "drop" not in ddl.lower()


def test_ddl_covers_every_declared_column() -> None:
    ddl = build_ddl()
    for name, ddl_type, _ in MISSING_COLUMNS:
        assert f"add column if not exists {name} {ddl_type}" in ddl


def test_declared_columns_are_actually_emitted_by_the_writer() -> None:
    """补的列必须确实是 ``_policy_shadow_row`` 会发的键，否则补了也没人用。"""
    from workflows import funnel_ai_selection

    source = inspect.getsource(funnel_ai_selection._policy_shadow_row)
    for name in column_names():
        assert f'"{name}"' in source, f"{name} 不在 _policy_shadow_row 的 payload 里"


def test_writer_columns_are_all_either_live_or_pending_ddl() -> None:
    """写入侧新加键时,要么表里已有,要么必须登记进 MISSING_COLUMNS 等人工执行 DDL。

    这条是本次事故的直接防线:2026-07-04 那两个键当时既不在表里、也没有任何清单
    记着它们缺,于是没有任何环节会报警。``_LIVE_COLUMNS`` 是 2026-09-01 对生产表
    实测的列集合;下次改 payload 若引入新键,这条会失败并要求同步登记。
    """
    from workflows import funnel_ai_selection

    live_columns = {
        "market",
        "trade_date",
        "regime",
        "schema_version",
        "snapshot_level",
        "base_policy",
        "shadow_policy",
        "signal_weights",
        "base_selected",
        "shadow_selected",
        "diff_added",
        "diff_removed",
        "selection_summary",
        "policy_summary",
        "registry_summary",
        "health_summary",
        "registry_snapshot",
        "health_snapshot",
        "updated_at",
        "created_at",
    }
    source = inspect.getsource(funnel_ai_selection._policy_shadow_row)
    # payload 的键都是 return 字典里缩进 8 格的 `"name":` 字面量。
    emitted = set(re.findall(r'^\s{8}"([a-z_]+)":', source, flags=re.MULTILINE))
    assert emitted, "没能从 _policy_shadow_row 解析出 payload 键，正则需要跟着改"
    unaccounted = emitted - live_columns - column_names()
    assert not unaccounted, f"这些键既不在生产表里也没登记进 MISSING_COLUMNS: {sorted(unaccounted)}"
