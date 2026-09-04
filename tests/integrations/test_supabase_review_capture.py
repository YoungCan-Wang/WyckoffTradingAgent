"""复盘捕获率落库:行构建与 schema 防漂移。

schema 以 Python 常量版本化(本项目不留 .sql),写入侧和建表侧靠测试对齐,
不靠人记。
"""

from __future__ import annotations

import re
from pathlib import Path

from core.funnel_taxonomy import (
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_THEME_MISS,
)
from core.review_capture_schema import UNIQUE_KEY, build_ddl, payload_keys
from integrations.supabase_review_capture import build_capture_rows

_DENOM = {"universe": 5347, "l1": 3442, "l2": 2236, "l3": 654, "candidate": 137}


def _build(rows: list[dict], **kwargs):
    params = {
        "trade_date": "2026-09-03",
        "previous_trade_date": "2026-09-02",
        "denominators": _DENOM,
        "context_source": "trace_snapshot",
    }
    params.update(kwargs)
    return build_capture_rows(rows, **params)


def test_every_row_carries_same_day_denominators() -> None:
    """基准率必须逐日算再合并。

    复盘池按「今日 >7% 且前一日 <3%」选样,本身就是按结果选样;只有拿同一天的
    同层分母作对照,召回率才有意义。分母跟着行落下来,而不是事后 join 一张
    30 天就过期的 trace。
    """
    rows = _build(
        [
            {"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT},
            {"code": "000002", "stage": REVIEW_STAGE_THEME_MISS},
        ]
    )

    assert len(rows) == 2
    for row in rows:
        assert row["universe_count"] == 5347
        assert row["l1_count"] == 3442
        assert row["l2_count"] == 2236
        assert row["l3_count"] == 654
        assert row["candidate_count"] == 137
        assert row["pool_size"] == 2
        assert row["trade_date"] == "2026-09-03"
        assert row["previous_trade_date"] == "2026-09-02"


def test_is_candidate_reads_the_stage_constant_not_a_reclassification() -> None:
    """判别性用例:候选归属只认档位常量,不在落库侧重新判定。

    重新判定会让报告与落库两套口径漂移(见 memory two-gates-must-share-one-source)。
    这里两行的 l1/l2/l3 全为真、只有 stage 不同,若落库侧改用闸门标记去推断
    is_candidate,两行就会同为 true,这个用例会失败。
    """
    rows = _build(
        [
            {
                "code": "000001",
                "stage": REVIEW_STAGE_CANDIDATE_HIT,
                "l1_eligible": True,
                "l2_eligible": True,
                "l3_eligible": True,
            },
            {
                "code": "000002",
                "stage": REVIEW_STAGE_THEME_MISS,
                "l1_eligible": True,
                "l2_eligible": True,
                "l3_eligible": True,
            },
        ]
    )

    assert [row["is_candidate"] for row in rows] == [True, False]


def test_gain_comes_from_the_gain_map_and_is_never_read_off_the_row() -> None:
    """gain_pct 是选样条件,来源必须是行情帧算出的涨幅。

    复盘行本身不带涨幅——报告里也刻意不展示它,免得被当成前瞻收益读
    (见 memory control-row-must-measure-itself)。这里给行塞一个假的 gain_pct,
    落库行仍应取 gain_map 的值。
    """
    rows = _build(
        [{"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT, "gain_pct": 99.0}],
        gain_map={"000001": 7.83},
    )

    assert rows[0]["gain_pct"] == 7.83


def test_missing_gain_stores_null_not_zero() -> None:
    """缺涨幅存 null:填 0 会把「没量到」读成「没涨」,把分布往下拽。"""
    rows = _build([{"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT}], gain_map={})

    assert rows[0]["gain_pct"] is None


def test_trigger_labels_survive_as_a_list() -> None:
    """买点标签只对候选池内的票有值——触发检测只跑最终候选集,别处是不可测而非零。"""
    rows = _build(
        [
            {"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT, "trigger_labels": ["lps", "sos"]},
            {"code": "000002", "stage": REVIEW_STAGE_THEME_MISS},
        ]
    )

    assert rows[0]["trigger_labels"] == ["lps", "sos"]
    assert rows[1]["trigger_labels"] == []


def test_rows_without_a_code_are_skipped_but_pool_size_keeps_the_pool() -> None:
    """无代码的行不落库;pool_size 仍是复盘池规模,不能被跳过的行改掉分母。"""
    rows = _build(
        [
            {"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT},
            {"code": "", "stage": REVIEW_STAGE_THEME_MISS},
        ]
    )

    assert [row["ts_code"] for row in rows] == ["000001"]
    assert rows[0]["pool_size"] == 2


def test_missing_dates_yield_no_rows() -> None:
    """缺日期不写:trade_date 是唯一键的一半,写进去就无法覆盖重跑。"""
    row = [{"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT}]

    assert _build(row, trade_date="") == []
    assert _build(row, previous_trade_date="") == []


def test_payload_keys_match_schema_columns() -> None:
    """写入键与建表列必须一一对应,漂了就是静默丢字段。"""
    rows = _build([{"code": "000001", "stage": REVIEW_STAGE_CANDIDATE_HIT}])
    written = set(rows[0])

    assert written == set(payload_keys()), sorted(written ^ set(payload_keys()))


def test_unique_key_matches_upsert_conflict_target() -> None:
    """键错位会让一行冲突回滚整批,而且是静默的(见 memory dedup-key-must-match-db-constraint)。"""
    source = Path("integrations/supabase_review_capture.py").read_text(encoding="utf-8")

    assert f'_CONFLICT_KEY = "{",".join(UNIQUE_KEY)}"' in source


def test_ddl_is_idempotent_and_indexes_stage_and_code() -> None:
    """建表语句要能重复执行,并覆盖两条主查询:按档位聚合、按单票追溯。"""
    ddl = build_ddl()

    assert "create table if not exists public.review_capture_daily" in ddl
    assert re.search(r"create index if not exists \w+_stage_idx", ddl)
    assert re.search(r"create index if not exists \w+_code_idx", ddl)
    assert f"unique ({', '.join(UNIQUE_KEY)})" in ddl
