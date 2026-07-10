from datetime import datetime
from zoneinfo import ZoneInfo

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

    assert "PROBE`：默认禁止" in lines
    assert "ATTACK`：禁止" in lines
