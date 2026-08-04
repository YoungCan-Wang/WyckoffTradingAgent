from __future__ import annotations

from core.market_trade_mode import resolve_market_trade_mode
from workflows.daily_job_persistence import step3_review_symbols
from workflows.daily_job_step3 import filter_confirmed_step3_codes
from workflows.step3_reporting import _empty_step3_report, _step3_title


def test_mainline_step3_reserves_context_for_confirmed_before_fresh_signals() -> None:
    symbols = [
        {"code": "000003", "signal_status": "confirmed", "candidate_lane": "mainline"},
        {"code": "000001", "name": "候选一", "candidate_lane": "trend"},
        {"code": "000002", "name": "候选二", "candidate_lane": "accum"},
    ]
    details = {"selected_for_ai": ["000002", "000001"]}

    rows = step3_review_symbols(
        symbols,
        step2_details=details,
        trade_mode=resolve_market_trade_mode("NEUTRAL"),
    )

    assert [row["code"] for row in rows] == ["000003", "000002", "000001"]
    assert [row["input_order"] for row in rows] == [0, 1, 2]


def test_confirmed_candidates_reach_step3_so_the_springboard_camp_can_fill() -> None:
    """`selected_for_ai` 全是当日 pending 触发；不并入 confirmed 会让起跳板恒空。"""
    symbols = [
        {"code": "000001", "signal_status": "pending"},
        {"code": "000002", "signal_status": "pending"},
        {"code": "000009", "signal_status": "confirmed"},
    ]
    rows = step3_review_symbols(
        symbols,
        step2_details={"selected_for_ai": ["000001", "000002"]},
        trade_mode=resolve_market_trade_mode("NEUTRAL"),
    )

    kept, _blocked = filter_confirmed_step3_codes([row["code"] for row in rows], symbols)
    assert kept == ["000009"], "送审名单里必须存在能过跨日确认硬门槛的标的"


def test_confirmed_review_additions_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("STEP3_CONFIRMED_REVIEW_CAP", "2")
    symbols = [{"code": f"00001{i}", "signal_status": "confirmed"} for i in range(5)]

    rows = step3_review_symbols(
        symbols,
        step2_details={"selected_for_ai": []},
        trade_mode=resolve_market_trade_mode("NEUTRAL"),
    )

    assert [row["code"] for row in rows] == ["000010", "000011"]


def test_default_step3_cap_keeps_three_validated_and_two_fresh() -> None:
    from workflows.step3_runtime_config import Step3RuntimeConfig
    from workflows.step3_selection import normalize_step3_candidates, select_step3_candidates

    symbols = [
        *[
            {"code": f"00000{i}", "signal_status": "confirmed", "track": "Accum", "priority_score": 10 - i}
            for i in range(1, 4)
        ],
        *[
            {"code": f"00000{i}", "signal_status": "pending", "track": "Trend", "priority_score": 100 - i}
            for i in range(4, 9)
        ],
    ]
    rows = step3_review_symbols(
        symbols,
        step2_details={"selected_for_ai": [f"00000{i}" for i in range(4, 9)]},
        trade_mode=resolve_market_trade_mode("NEUTRAL"),
    )
    candidates = normalize_step3_candidates(rows)

    selected = select_step3_candidates(candidates, "NEUTRAL", Step3RuntimeConfig(enable_compression=False))

    assert selected["code"].tolist() == ["000001", "000002", "000003", "000004", "000005"]


def test_unconfirmed_step3_verdict_still_cannot_reach_execution() -> None:
    kept, blocked = filter_confirmed_step3_codes(
        ["000001", "000002"],
        [
            {"code": "000001", "signal_status": "pending"},
            {"code": "000002", "signal_status": "confirmed"},
        ],
    )

    assert kept == ["000002"]
    assert blocked == ["000001"]


def test_empty_step3_report_states_real_upstream_reason() -> None:
    report = _empty_step3_report("", [], input_count=0)

    assert "本轮未执行三阵营模型审判" in report
    assert "上游实际送入 Step3 的候选为 0" in report
    assert "候选均被 RAG" not in report
    assert "风险过高" not in report


def test_step3_title_uses_report_trade_date_instead_of_wall_clock() -> None:
    assert _step3_title({"trade_date": "2026-07-15"}) == "📄 批量研报 2026-07-15"
