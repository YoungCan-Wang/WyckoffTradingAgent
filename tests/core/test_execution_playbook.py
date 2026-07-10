from datetime import date

from core.execution_playbook import (
    funnel_playbook_lines,
    oms_playbook_lines,
    step3_playbook_lines,
    tail_buy_playbook_lines,
)
from core.tail_buy.models import DECISION_BUY, TailBuyCandidate
from core.tail_buy.reporting import build_tail_buy_markdown
from workflows.funnel_render import _top_summary_lines
from workflows.step4_ticket import render_trade_ticket


def test_funnel_playbook_blocks_risk_on_new_buys() -> None:
    text = "\n".join(funnel_playbook_lines("RISK_ON", selected_count=3))
    assert "今日执行纪律" in text
    assert "禁止" in text
    assert "5 日" in text
    assert "-12%" in text


def test_funnel_playbook_allows_neutral_mainline() -> None:
    text = "\n".join(funnel_playbook_lines("NEUTRAL", selected_count=4))
    assert "可执行买入" in text or "允许" in text
    assert "主线" in text


def test_top_summary_includes_playbook() -> None:
    class _Ctx:
        regime = "NEUTRAL"
        benchmark_context = {"regime": "NEUTRAL"}
        unique_hit_count = 2
        mainline_tradeable = ["000001"]
        theme_candidate_map = {}

    lines = _top_summary_lines(_Ctx(), selected_count=2, money_line="资金中性")
    joined = "\n".join(lines)
    assert "今日执行纪律" in joined
    assert "今日结论" in joined


def test_tail_guardrail_blocks_risk_on_new_buys() -> None:
    from core.tail_buy.guardrails import tail_entry_veto_reasons

    reasons = tail_entry_veto_reasons({"support_level": 10.0}, "mainline", "RISK_ON")
    assert any("禁止新开仓" in r for r in reasons)
    holding = tail_entry_veto_reasons({"support_level": 10.0}, "holding", "RISK_ON")
    assert not any("禁止新开仓" in r for r in holding)


def test_hold_trade_days_uses_trading_calendar_not_weekends() -> None:
    import pandas as pd

    from workflows.tail_buy_holdings import _hold_trade_days

    # 周五买入后跨周末到下周三：自然日约 5，交易日序列含买入日共 4 根。
    hist = pd.DataFrame(
        {
            "date": [
                "2026-07-03",  # Fri buy
                "2026-07-06",  # Mon
                "2026-07-07",  # Tue
                "2026-07-08",  # Wed as_of
            ]
        }
    )
    days = _hold_trade_days("2026-07-03", hist, as_of=date(2026, 7, 8))
    assert days == 4
    # 无日K时不得用自然日硬推。
    assert _hold_trade_days("2026-07-03", None, as_of=date(2026, 7, 8)) is None


def test_universe_rps_not_local_candidate_rank() -> None:
    import pandas as pd

    from core.wyckoff_engine import FunnelConfig, _universe_rps_slow_map

    cfg = FunnelConfig()
    # 只有 5 只时样本不足，不得产出伪 RPS。
    tiny = {
        f"{i:06d}": pd.DataFrame(
            {
                "close": [10.0 + j * 0.01 * (i + 1) for j in range(130)],
            }
        )
        for i in range(5)
    }
    assert _universe_rps_slow_map(tiny, cfg) == {}


def test_tail_buy_markdown_includes_playbook() -> None:
    item = TailBuyCandidate(
        code="000001",
        name="测试",
        signal_date="2026-07-09",
        status="confirmed",
        signal_type="mainline",
        signal_score=1.0,
        rule_decision=DECISION_BUY,
        rule_score=80.0,
        rule_reasons=["缩量承接"],
        final_decision=DECISION_BUY,
        priority_score=80.0,
    )
    md = build_tail_buy_markdown(
        now_text="2026-07-10 14:40",
        target_signal_date="2026-07-09",
        market_reminder="NEUTRAL",
        candidates=[item],
        llm_total=0,
        llm_success=0,
        elapsed_seconds=1.0,
    )
    assert "执行纪律" in md
    assert "BUY=今日可执行" in md


def test_oms_ticket_includes_playbook() -> None:
    report = render_trade_ticket("NEUTRAL 可执行", 100_000, 50_000, 50_000, [], atr_period=14)
    assert "执行纪律" in report
    assert "EXIT/TRIM" in report
    assert "5 日" in report


def test_step3_and_oms_playbook_helpers() -> None:
    assert "起跳板" in "\n".join(step3_playbook_lines("NEUTRAL"))
    assert "PROBE/ATTACK" in "\n".join(oms_playbook_lines("view"))
    assert "明日" in "\n".join(tail_buy_playbook_lines(report_mode="post_close_review"))
