"""Runtime config loader for benchmark regime analysis."""

from __future__ import annotations

import os

from tools.market_regime import MarketRegimeConfig

TRUE_TEXTS = {"1", "true", "yes", "on"}


def market_regime_config_from_env() -> MarketRegimeConfig:
    return MarketRegimeConfig(
        breadth_ma_window=_int_env("FUNNEL_BREADTH_MA_WINDOW", 20, min_value=1),
        breadth_risk_off_threshold=_float_env("FUNNEL_BREADTH_RISK_OFF_PCT", 20.0),
        breadth_risk_on_threshold=_float_env("FUNNEL_BREADTH_RISK_ON_PCT", 60.0),
        breadth_risk_on_min_delta=_float_env("FUNNEL_BREADTH_RISK_ON_DELTA", 0.0),
        breadth_cliff_drop_pct=_float_env("FUNNEL_BREADTH_CLIFF_DROP_PCT", -10.0),
        daily_breadth_repair_threshold=_float_env("FUNNEL_DAILY_BREADTH_REPAIR_PCT", 60.0),
        daily_breadth_weak_threshold=_float_env("FUNNEL_DAILY_BREADTH_WEAK_PCT", 35.0),
        smallcap_bench_code=os.getenv("FUNNEL_SMALLCAP_BENCH_CODE", "399006").strip() or "399006",
        crash_main_day_drop_pct=_float_env("FUNNEL_CRASH_MAIN_DAY_DROP_PCT", -1.3),
        crash_small_day_drop_pct=_float_env("FUNNEL_CRASH_SMALL_DAY_DROP_PCT", -2.5),
        crash_breadth_ratio_pct=_float_env("FUNNEL_CRASH_BREADTH_RATIO_PCT", 15.0),
        crash_breadth_delta_pct=_float_env("FUNNEL_CRASH_BREADTH_DELTA_PCT", -20.0),
        panic_repair_min_avg_amount_wan=_float_env("FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN", 7000.0),
        risk_off_min_avg_amount_wan=_float_env("FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN", 8000.0),
        # 恢复 10000/12000。#295 曾依「全市场分档实测门槛越高净超额越差」下调到 8000，
        # 但该测试**未叠加禁买闸门**：CRASH 与深度 RISK_OFF 都在 STEP4_BUY_BLOCK_REGIMES
        # 里，这些档位下压根不下单，门槛因此不参与任何成交决策——它只影响候选池规模。
        # 修复回测小盘基准后（#299）重跑对照：门槛 8000 与 12000/10000 两组的成交笔数、
        # 胜率、总收益、夏普、回撤、VaR95、Trend/Accum 分层全部一字不差（103 笔 / 33.01% /
        # +5.72% / 0.714 / -7.25% / -8.881%）。即该改动在逻辑与实测上均无效。
        risk_off_deep_min_avg_amount_wan=_float_env("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", 10000.0),
        crash_min_avg_amount_wan=_float_env("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", 12000.0),
        panic_repair_enabled=_bool_env("FUNNEL_PANIC_REPAIR_ENABLE", True),
        panic_repair_main_rebound_pct=_float_env("FUNNEL_PANIC_REPAIR_MAIN_REBOUND_PCT", 0.8),
        panic_repair_small_rebound_pct=_float_env("FUNNEL_PANIC_REPAIR_SMALL_REBOUND_PCT", 1.5),
        panic_repair_confirm_main_pct=_float_env("FUNNEL_PANIC_REPAIR_CONFIRM_MAIN_PCT", 0.0),
        panic_repair_confirm_breadth_pct=_float_env("FUNNEL_PANIC_REPAIR_CONFIRM_BREADTH_PCT", 50.0),
        evr_policy=os.getenv("FUNNEL_EVR_POLICY", "all_regimes").strip().lower() or "all_regimes",
        pv_llm_provider=os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip().lower() or "gemini",
    ).normalized()


def _int_env(name: str, default: int, *, min_value: int) -> int:
    try:
        value = int(float(os.getenv(name, str(default))))
    except ValueError:
        value = default
    return max(value, min_value)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUE_TEXTS
