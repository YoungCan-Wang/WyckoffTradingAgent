from core.market_trade_mode import resolve_market_trade_mode
from tools.market_regime import (
    MainBenchmarkMetrics,
    MarketRegimeConfig,
    SmallcapMetrics,
    _apply_caution_regime,
    _repair_reasons,
)


def test_trade_mode_blocks_new_buy_in_risk_off_market() -> None:
    mode = resolve_market_trade_mode("RISK_OFF")

    assert mode.mode == "observe_only"
    assert mode.allow_ai_review is False
    assert mode.allow_recommendation_write is False
    assert mode.allow_bypass_review is False


def test_trade_mode_allows_repair_review_without_write() -> None:
    mode = resolve_market_trade_mode("bear_rebound")

    assert mode.mode == "repair_review"
    assert mode.allow_ai_review is True
    assert mode.allow_recommendation_write is False
    assert mode.allow_bypass_review is False


def test_trade_mode_keeps_neutral_mainline_active() -> None:
    mode = resolve_market_trade_mode("NEUTRAL")

    assert mode.mode == "mainline_active"
    assert mode.allow_ai_review is True
    assert mode.allow_full_l4 is True
    assert mode.allow_theme_promotion is True
    assert mode.allow_bypass_review is False


def test_trade_mode_caution_stays_confirmation_only() -> None:
    mode = resolve_market_trade_mode("CAUTION")

    assert mode.mode == "confirmation_only"
    assert mode.allow_ai_review is True
    assert mode.allow_full_l4 is False
    assert mode.allow_theme_promotion is False


def test_trade_mode_blocks_risk_on_execution_but_keeps_ai_shadow() -> None:
    mode = resolve_market_trade_mode("RISK_ON")

    assert mode.mode == "overheat_shadow"
    assert mode.allow_ai_review is True
    assert mode.allow_recommendation_write is False
    assert mode.allow_full_l4 is False
    assert mode.allow_theme_promotion is False
    assert mode.allow_bypass_review is False


def test_steady_bull_rebound_does_not_trigger_panic_repair() -> None:
    reasons = _repair_reasons(
        MainBenchmarkMetrics(today_pct=0.4408, prev_pct=0.5031),
        SmallcapMetrics(today_pct=-1.888, prev_pct=2.9884),
        MarketRegimeConfig().normalized(),
        base_regime="RISK_ON",
    )

    assert reasons == []


def test_defensive_continuous_rebound_can_trigger_repair_review() -> None:
    reasons = _repair_reasons(
        MainBenchmarkMetrics(today_pct=0.45, prev_pct=0.5),
        SmallcapMetrics(today_pct=-0.2, prev_pct=0.1),
        MarketRegimeConfig().normalized(),
        base_regime="RISK_OFF",
    )

    assert reasons == ["continuous_rebound_after_RISK_OFF(main_prev=0.5, main_today=0.45)"]


def test_risk_on_structure_with_weak_breadth_becomes_caution() -> None:
    cfg = MarketRegimeConfig().normalized()

    assert _apply_caution_regime("RISK_ON", 33.9, cfg) == "CAUTION"
    assert _apply_caution_regime("RISK_ON", 60.0, cfg) == "RISK_ON"
    assert _apply_caution_regime("RISK_OFF", 33.9, cfg) == "RISK_OFF"
