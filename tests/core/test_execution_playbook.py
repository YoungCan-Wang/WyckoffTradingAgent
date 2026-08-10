from datetime import date

from core.execution_playbook import funnel_playbook_lines, oms_playbook_lines, step3_playbook_lines
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


def test_hold_trade_days_uses_trading_calendar_not_weekends() -> None:
    import pandas as pd

    from workflows.holding_diagnosis_core import _hold_trade_days

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


def test_oms_ticket_includes_playbook() -> None:
    report = render_trade_ticket("NEUTRAL 可执行", 100_000, 50_000, 50_000, [], atr_period=14)
    assert "执行纪律" in report
    assert "EXIT/TRIM" in report
    assert "5 日" in report
    assert report.count("市场视图") == 1


def test_step3_and_oms_playbook_helpers() -> None:
    assert "起跳板" in "\n".join(step3_playbook_lines("NEUTRAL"))
    assert "PROBE/ATTACK" in "\n".join(oms_playbook_lines())


def test_trade_ticket_shows_decision_model():
    """Step4 的模型选型直接决定这张工单的内容，必须与工单同屏可见。

    Step4 的 LLM 输出经 complete_step4_decisions → WyckoffOrderEngine 变成实际
    订单，不像 Step3 只是研报。因此"这批单子是哪个模型决定的"必须能在工单上直接
    看到，而不是只留在 CI 日志里。
    """
    from workflows.step4_ticket import render_trade_ticket

    report = render_trade_ticket(
        market_view="",
        total_equity=100000.0,
        free_cash_before=50000.0,
        free_cash_after=50000.0,
        tickets=[],
        atr_period=14,
        model_label="deepseek:deepseek-v4-flash",
    )

    assert "deepseek-v4-flash" in report
    assert "决策模型" in report


def test_trade_ticket_omits_model_line_when_unknown():
    """缺 provider/model 时省略该行，不让展示字段中断下单主流程。"""
    from workflows.step4_ticket import render_trade_ticket

    report = render_trade_ticket(
        market_view="",
        total_equity=100000.0,
        free_cash_before=50000.0,
        free_cash_after=50000.0,
        tickets=[],
        atr_period=14,
    )

    assert "决策模型" not in report


def test_step4_model_label_tolerates_missing_fields():
    from types import SimpleNamespace

    from workflows.step4_rebalancer import _step4_model_label

    assert _step4_model_label(SimpleNamespace()) == ""
    assert _step4_model_label(SimpleNamespace(provider="gemini", model="gemini-3-flash-preview")) == (
        "gemini:gemini-3-flash-preview"
    )
