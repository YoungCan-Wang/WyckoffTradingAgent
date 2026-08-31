from datetime import datetime
from zoneinfo import ZoneInfo

from core.market_trade_mode import PREMARKET_DATA_GAP
from workflows import premarket_risk_inputs as inputs
from workflows.premarket_risk_inputs import PremarketRiskConfig


def _config() -> PremarketRiskConfig:
    return PremarketRiskConfig(
        a50_crash_pct=-2.0,
        a50_risk_off_pct=-1.0,
        vix_crash_pct=15.0,
        vix_crash_close=25.0,
        vix_risk_off_pct=8.0,
        vix_ready_hour_et=17,
        vix_poll_interval_seconds=1,
        vix_max_attempts=2,
    )


def test_judge_regime_black_swan_on_a50_crash():
    regime, reasons = inputs.judge_regime(
        {"pct_chg": "-2.4"},
        {"close": "18", "pct_chg": "1.2"},
        _config(),
    )

    assert regime == "BLACK_SWAN"
    assert "A50跌幅 -2.40%" in reasons[0]


def test_judge_regime_caution_when_vix_spikes_below_absolute_crash_level():
    regime, reasons = inputs.judge_regime(
        {"pct_chg": "0.2"},
        {"close": "22.5", "pct_chg": "16.0"},
        _config(),
    )

    assert regime == "CAUTION"
    assert "按 CAUTION 处理" in reasons[0]


def test_judge_regime_risk_off_on_moderate_a50_or_vix_stress():
    regime, reasons = inputs.judge_regime(
        {"pct_chg": "-1.2"},
        {"close": "18", "pct_chg": "8.5"},
        _config(),
    )

    assert regime == "RISK_OFF"
    assert any("A50跌幅" in reason for reason in reasons)
    assert any("VIX涨幅" in reason for reason in reasons)


def test_judge_regime_missing_benign_input_reports_data_gap():
    """取数失败返回 DATA_GAP，不再与「盘前明确看不清」的 UNKNOWN 混用。

    此前两者共用 UNKNOWN，使外部数据源抖动冒充风险事件（实测 A50 缺失率 13.3%、
    VIX 10.0%）。更荒谬的是不一致：盘前任务压根没跑会回落 benchmark 从而放行，
    而任务跑了但拉不到数据反而禁买。下游 resolve_effective_market_regime 现在把
    DATA_GAP 与「字段为空」同等对待。
    """
    regime, reasons = inputs.judge_regime(
        {"pct_chg": "0.2"},
        {"close": None, "pct_chg": None},
        _config(),
    )

    assert regime == PREMARKET_DATA_GAP
    assert "VIX 数据缺失" in reasons[0]


def test_data_gap_falls_back_to_benchmark_but_keeps_real_risk():
    from workflows.step4_market import resolve_effective_market_regime

    # 取数失败：回落收盘态，不再无故禁买。
    assert resolve_effective_market_regime("BEAR_REBOUND", PREMARKET_DATA_GAP) == "BEAR_REBOUND"
    # 盘前 UNKNOWN 也一并回落：实测挡错的两天都是 a50_pct_chg 缺失伪装成 UNKNOWN。
    assert resolve_effective_market_regime("BEAR_REBOUND", "UNKNOWN") == "BEAR_REBOUND"
    # BLACK_SWAN 是唯一保留的收紧通道。
    assert resolve_effective_market_regime("BEAR_REBOUND", "BLACK_SWAN") == "BLACK_SWAN"
    # 回落不等于放行：收盘态本身禁买时仍然禁买。
    assert resolve_effective_market_regime("CRASH", PREMARKET_DATA_GAP) == "CRASH"


def test_hard_block_survives_missing_input():
    """A50 触发硬阈值时即使 VIX 缺失也必须给出 BLACK_SWAN，不能退化为 DATA_GAP。"""
    regime, _ = inputs.judge_regime({"pct_chg": "-3.0"}, {"close": None, "pct_chg": None}, _config())
    assert regime != PREMARKET_DATA_GAP


def test_judge_regime_keeps_observed_hard_block_when_other_input_is_missing():
    regime, _reasons = inputs.judge_regime(
        {"pct_chg": "-1.2"},
        {"close": None, "pct_chg": None},
        _config(),
    )

    assert regime == "RISK_OFF"


def test_ensure_vix_fresh_uses_previous_us_trade_date_before_ready_hour():
    now = datetime(2026, 6, 22, 16, 0, tzinfo=ZoneInfo("America/New_York"))

    trade_date = inputs.ensure_vix_fresh("2026-06-19", "test", _config(), now=now)

    assert trade_date.isoformat() == "2026-06-19"


def test_ensure_vix_fresh_rejects_stale_date_after_ready_hour():
    now = datetime(2026, 6, 22, 18, 0, tzinfo=ZoneInfo("America/New_York"))

    try:
        inputs.ensure_vix_fresh("2026-06-19", "test", _config(), now=now)
    except RuntimeError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("expected stale VIX date to be rejected")


def test_ensure_vix_fresh_accepts_previous_close_after_nyse_holiday():
    memorial_day = datetime(2026, 5, 25, 18, 0, tzinfo=ZoneInfo("America/New_York"))

    trade_date = inputs.ensure_vix_fresh("2026-05-22", "test", _config(), now=memorial_day)

    assert trade_date.isoformat() == "2026-05-22"


def test_us_equity_holiday_calendar_matches_known_good_fridays():
    expected = {
        2025: "2025-04-18",
        2026: "2026-04-03",
        2027: "2027-03-26",
        2028: "2028-04-14",
    }

    for year, good_friday in expected.items():
        assert good_friday in {day.isoformat() for day in inputs._us_equity_holidays(year)}


def test_fetch_vix_until_ready_returns_timeout_fallback(monkeypatch):
    config = _config()
    logs: list[str] = []
    monkeypatch.setattr(inputs, "fetch_vix", lambda _config: {"ok": False, "error": "not ready"})
    monkeypatch.setattr(inputs.time, "sleep", lambda _seconds: None)

    result = inputs.fetch_vix_until_ready(config=config, log=logs.append)

    assert result["source"] == "timeout_fallback"
    assert "exceeded max attempts" in str(result["error"])
    assert any("继续轮询" in line for line in logs)


def test_build_action_matrix_risk_off_blocks_new_buy_actions():
    lines = "\n".join(inputs.build_action_matrix("RISK_OFF"))

    assert "PROBE（试探建仓）`：禁止" in lines
    assert "ATTACK（进攻建仓/浮盈加仓）`：禁止" in lines


def test_action_matrix_only_uses_executable_actions():
    for regime in ("UNKNOWN", "NORMAL", "CAUTION", "RISK_OFF", "BLACK_SWAN"):
        lines = "\n".join(inputs.build_action_matrix(regime))
        assert "LIGHT_ADD" not in lines
        assert "FULL_ATTACK" not in lines
        for action in ("EXIT", "TRIM", "HOLD", "PROBE", "ATTACK"):
            assert action in lines


def test_invalid_action_matrix_regime_fails_closed():
    lines = "\n".join(inputs.build_action_matrix("typo"))

    assert "UNKNOWN（数据待确认）" in lines
    assert "PROBE（试探建仓）`：禁止" in lines
