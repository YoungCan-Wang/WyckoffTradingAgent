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
        # 深度 RISK_OFF 与 CRASH 的流动性门槛由 10000/12000 统一下调到 8000（与 RISK_OFF 齐平）。
        # 实测（466 个交易日 / 全市场 20 日均额分档，T+5 净超额已扣 0.202% 往返成本）：
        #   门槛 4000 万  日均候选 4801  净超额 -0.24
        #   门槛 8000 万  日均候选 3751  净超额 -0.31
        #   门槛 12000 万 日均候选 2999  净超额 -0.35
        # 单调递减——抬高门槛没有选股优势。被 CRASH 档砍掉的边缘区间 11000~12000 万
        # 单独看是 -0.20pct，反而优于 12000 档整体，即砍掉的恰是相对不那么差的一批。
        # 2026-08-20 复盘中 73/200 只涨停股被该档拦掉，其中神州细胞/南模生物差不到 55 万。
        #
        # 不降到默认 4000 的理由：该测试用统一 0.202% 成本，未建模低流动性票的滑点差异，
        # 而门槛的真实价值在滑点保护而非选股。保留 8000 以挡住 4000~8000 那批真正难成交的标的。
        risk_off_deep_min_avg_amount_wan=_float_env("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", 8000.0),
        crash_min_avg_amount_wan=_float_env("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", 8000.0),
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
