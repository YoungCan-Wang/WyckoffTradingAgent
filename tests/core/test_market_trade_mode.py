from core.market_trade_mode import resolve_market_trade_mode


def test_missing_regime_fails_closed() -> None:
    mode = resolve_market_trade_mode(None)

    assert mode.regime == "UNKNOWN"
    assert mode.mode == "observe_only"
    assert mode.allow_recommendation_write is False


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


def test_confirmed_repair_opens_probe_only_mode() -> None:
    mode = resolve_market_trade_mode("PANIC_REPAIR_CONFIRMED")

    assert mode.mode == "repair_probe"
    assert mode.allow_ai_review is True
    assert mode.allow_recommendation_write is True
    assert mode.allow_full_l4 is False
    assert mode.allow_theme_promotion is False


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


def test_write_gate_follows_oms_block_list(monkeypatch) -> None:
    """两侧闸门必须同源：买不到的水温不许写成正式推荐。

    回归 2026-08 生产事故：运维把 NEUTRAL 加进 STEP4_BUY_BLOCK_REGIMES 后，只有 OMS
    照办，resolve_market_trade_mode 压根不读这个变量，于是 recommendation_tracking
    里写进 16 行没有 :market_blocked 后缀的正式推荐（20260720×9 / 20260721×4 /
    20260723×3），报告显示「可执行买入」而实际一股也买不到。
    """
    from workflows.step4_order_config import step4_order_config_from_env

    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN")
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND")
    blocked = step4_order_config_from_env().buy_block_regimes

    assert "NEUTRAL" in blocked
    neutral = resolve_market_trade_mode("NEUTRAL")
    assert neutral.mode == "execution_blocked"
    assert neutral.allow_recommendation_write is False
    # 仍要能攒对照样本，否则禁买期就没有数据支撑下一次决策。
    assert neutral.allow_ai_review is True
    assert neutral.allow_full_l4 is False
    assert neutral.allow_theme_promotion is False

    for regime in ("RISK_ON", "BEAR_REBOUND", "CAUTION", "PANIC_REPAIR", "RISK_OFF", "CRASH", "UNKNOWN"):
        mode = resolve_market_trade_mode(regime)
        assert mode.allow_recommendation_write is (regime not in blocked), regime


def test_neutral_write_and_buy_reopen_together(monkeypatch) -> None:
    """回退路径：ALLOW 里加 NEUTRAL 必须同时打开两侧，不能再错位。"""
    from workflows.step4_order_config import step4_order_config_from_env

    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN")
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "BEAR_REBOUND,NEUTRAL")

    assert "NEUTRAL" not in step4_order_config_from_env().buy_block_regimes
    mode = resolve_market_trade_mode("NEUTRAL")
    assert mode.mode == "mainline_active"
    assert mode.allow_recommendation_write is True


def test_hard_defense_regimes_ignore_allow_list(monkeypatch) -> None:
    """硬防守档不可被 env 打开——新增的同源分支不能给它们开后门。"""
    monkeypatch.setenv("STEP4_BUY_ALLOW_REGIMES", "RISK_OFF,CRASH,BLACK_SWAN,UNKNOWN")

    for regime in ("RISK_OFF", "CRASH", "BLACK_SWAN", "UNKNOWN"):
        mode = resolve_market_trade_mode(regime)
        assert mode.mode == "observe_only", regime
        assert mode.allow_recommendation_write is False, regime
        assert mode.allow_ai_review is False, regime


def test_steady_bull_rebound_does_not_trigger_panic_repair() -> None:
    reasons = _repair_reasons(
        MainBenchmarkMetrics(today_pct=0.4408, prev_pct=0.5031),
        SmallcapMetrics(today_pct=-1.888, prev_pct=2.9884),
        MarketRegimeConfig().normalized(),
    )

    assert reasons == []


def test_defensive_continuous_rebound_without_panic_stays_out_of_repair() -> None:
    reasons = _repair_reasons(
        MainBenchmarkMetrics(today_pct=0.45, prev_pct=0.5),
        SmallcapMetrics(today_pct=-0.2, prev_pct=0.1),
        MarketRegimeConfig().normalized(),
    )

    assert reasons == []


def test_risk_on_structure_with_weak_breadth_becomes_caution() -> None:
    cfg = MarketRegimeConfig().normalized()

    assert _apply_caution_regime("RISK_ON", 33.9, cfg) == "CAUTION"
    assert _apply_caution_regime("RISK_ON", 60.0, cfg) == "RISK_ON"
    assert _apply_caution_regime("RISK_OFF", 33.9, cfg) == "RISK_OFF"
