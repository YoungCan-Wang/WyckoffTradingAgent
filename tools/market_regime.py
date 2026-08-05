"""
大盘水温 + 市场广度 + regime 分类工具。

分析大盘指数走势，计算市场广度，输出 regime 分类并动态调整阈值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from core.market_breadth import calc_market_breadth as calc_core_market_breadth
from core.wyckoff_engine import FunnelConfig
from tools.market_liquidity import calc_amount_distribution_health, calc_market_money_flow
from utils.safe import finite_float as _safe_float

_PV_OUTLOOK_FALLBACK: dict[str, str] = {
    "RISK_ON": "次日推演：短线处于过热禁追区，继续管理旧仓；等待过热降温并重新确认后再评估新仓。",
    "BEAR_REBOUND": "次日推演：熊市反抽只做强确认，若不能放量站稳MA200，控制仓位并回避追高。",
    "PANIC_REPAIR": "次日推演：当前仅为修复候选，继续观察；次日广度与价格未同时确认前禁止新仓。",
    "PANIC_REPAIR_CONFIRMED": "次日推演：修复已获广度与价格确认，只允许小额试探；若确认位失守立即退回观察。",
    "NEUTRAL": "次日推演：中性震荡为主，等待放量突破近高或放量跌破MA50后再确认方向。",
    "RISK_OFF": "次日推演：防守优先，若出现放量下压并失守MA50，继续收缩风险敞口；仅在缩量止跌后再评估试探。",
    "CRASH": "次日推演：防守优先，若出现放量下压并失守MA50，继续收缩风险敞口；仅在缩量止跌后再评估试探。",
}

_PV_SYSTEM_PROMPT = (
    "你是分析 A 股现货多头账户的 Wyckoff 量价分析师。根据以下大盘结构化数据，给出次日操作推演。\n"
    "要求：1-2句话，不超过80字；同时给出转强确认和转弱失效条件；只能使用观察、持有、减仓、回避或试探，"
    "禁止建议做空、卖空、融券或任何空头交易。"
)

_PV_FORBIDDEN_ACTIONS = ("做空", "卖空", "融券", "空头开仓", "反手空")


@dataclass(frozen=True)
class MarketRegimeConfig:
    breadth_ma_window: int = 20
    breadth_risk_off_threshold: float = 20.0
    breadth_risk_on_threshold: float = 60.0
    breadth_risk_on_min_delta: float = 0.0
    breadth_cliff_drop_pct: float = -10.0
    daily_breadth_repair_threshold: float = 60.0
    daily_breadth_weak_threshold: float = 35.0
    smallcap_bench_code: str = "399006"
    crash_main_day_drop_pct: float = -1.3
    crash_small_day_drop_pct: float = -2.5
    crash_breadth_ratio_pct: float = 15.0
    crash_breadth_delta_pct: float = -20.0
    panic_repair_min_avg_amount_wan: float = 7000.0
    risk_off_min_avg_amount_wan: float = 8000.0
    risk_off_deep_min_avg_amount_wan: float = 10000.0
    crash_min_avg_amount_wan: float = 12000.0
    panic_repair_enabled: bool = True
    panic_repair_main_rebound_pct: float = 0.8
    panic_repair_small_rebound_pct: float = 1.5
    panic_repair_confirm_main_pct: float = 0.0
    panic_repair_confirm_breadth_pct: float = 50.0
    evr_policy: str = "all_regimes"
    pv_llm_provider: str = "gemini"

    def normalized(self) -> MarketRegimeConfig:
        return MarketRegimeConfig(
            breadth_ma_window=max(int(self.breadth_ma_window), 1),
            breadth_risk_off_threshold=float(self.breadth_risk_off_threshold),
            breadth_risk_on_threshold=float(self.breadth_risk_on_threshold),
            breadth_risk_on_min_delta=float(self.breadth_risk_on_min_delta),
            breadth_cliff_drop_pct=float(self.breadth_cliff_drop_pct),
            daily_breadth_repair_threshold=float(self.daily_breadth_repair_threshold),
            daily_breadth_weak_threshold=float(self.daily_breadth_weak_threshold),
            smallcap_bench_code=str(self.smallcap_bench_code or "399006").strip() or "399006",
            crash_main_day_drop_pct=float(self.crash_main_day_drop_pct),
            crash_small_day_drop_pct=float(self.crash_small_day_drop_pct),
            crash_breadth_ratio_pct=float(self.crash_breadth_ratio_pct),
            crash_breadth_delta_pct=float(self.crash_breadth_delta_pct),
            panic_repair_min_avg_amount_wan=float(self.panic_repair_min_avg_amount_wan),
            risk_off_min_avg_amount_wan=float(self.risk_off_min_avg_amount_wan),
            risk_off_deep_min_avg_amount_wan=float(self.risk_off_deep_min_avg_amount_wan),
            crash_min_avg_amount_wan=float(self.crash_min_avg_amount_wan),
            panic_repair_enabled=bool(self.panic_repair_enabled),
            panic_repair_main_rebound_pct=float(self.panic_repair_main_rebound_pct),
            panic_repair_small_rebound_pct=float(self.panic_repair_small_rebound_pct),
            panic_repair_confirm_main_pct=float(self.panic_repair_confirm_main_pct),
            panic_repair_confirm_breadth_pct=float(self.panic_repair_confirm_breadth_pct),
            evr_policy=str(self.evr_policy or "all_regimes").strip().lower() or "all_regimes",
            pv_llm_provider=str(self.pv_llm_provider or "gemini").strip().lower() or "gemini",
        )


DEFAULT_MARKET_REGIME_CONFIG = MarketRegimeConfig()


@dataclass(frozen=True)
class MainBenchmarkMetrics:
    close: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    ma50_slope_5d: float | None = None
    recent3_list: list[float] | None = None
    recent3_cum: float | None = None
    today_pct: float | None = None
    prev_pct: float | None = None
    prev2_pct: float | None = None
    vol_ma5: float | None = None
    vol_ma20: float | None = None
    vol_ratio_5_20: float | None = None
    volume_state: str = "未知"


@dataclass(frozen=True)
class SmallcapMetrics:
    close: float | None = None
    recent3_list: list[float] | None = None
    recent3_cum: float | None = None
    today_pct: float | None = None
    prev_pct: float | None = None
    prev2_pct: float | None = None


def _build_pv_user_message(
    regime: str,
    close: float | None,
    ma50: float | None,
    ma200: float | None,
    price_zone: str,
    vol_ratio_text: str,
    volume_state: str,
    recent3_cum: float | None,
) -> str:
    parts = [
        f"Regime: {regime}",
        f"收盘: {close}",
        f"MA50: {ma50}, MA200: {ma200}",
        f"价格区域: {price_zone}",
        f"5日/20日量比: {vol_ratio_text} ({volume_state})",
    ]
    if recent3_cum is not None:
        parts.append(f"近3日累计涨跌: {recent3_cum:+.2f}%")
    return "\n".join(parts)


def _generate_pv_outlook(
    *,
    regime: str,
    close: float | None,
    ma50: float | None,
    ma200: float | None,
    price_zone: str,
    vol_ratio_text: str,
    volume_state: str,
    recent3_cum: float | None,
    provider: str,
) -> str:
    fallback = _PV_OUTLOOK_FALLBACK.get(regime, "次日推演：结构信息不足，先观察量能与MA50得失再定方向。")
    try:
        from integrations.llm_client import call_llm, get_provider_credentials

        api_key, model, base_url = get_provider_credentials(provider)
        if not api_key:
            return fallback
        user_msg = _build_pv_user_message(
            regime,
            close,
            ma50,
            ma200,
            price_zone,
            vol_ratio_text,
            volume_state,
            recent3_cum,
        )
        raw = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=_PV_SYSTEM_PROMPT,
            user_message=user_msg,
            base_url=base_url or None,
            timeout=30,
            max_output_tokens=200,
        ).strip()
        return _sanitize_pv_outlook(raw, fallback)
    except Exception:
        return fallback


def _sanitize_pv_outlook(raw: str, fallback: str) -> str:
    text = str(raw or "").strip()
    if len(text) < 5 or any(action in text for action in _PV_FORBIDDEN_ACTIONS):
        return fallback
    if len(text) > 120:
        return fallback
    return text if text.startswith("次日推演") else f"次日推演：{text}"


def calc_market_breadth(
    df_map: dict[str, pd.DataFrame],
    ma_window: int = DEFAULT_MARKET_REGIME_CONFIG.breadth_ma_window,
) -> dict:
    return calc_core_market_breadth(df_map, ma_window=ma_window)


def _latest_trade_gap_days(df: pd.DataFrame | None) -> int:
    if df is None or df.empty or "date" not in df.columns:
        return 0
    dates = pd.to_datetime(df["date"], errors="coerce").dropna().sort_values()
    if len(dates) < 2:
        return 0
    return int((dates.iloc[-1].date() - dates.iloc[-2].date()).days)


def _resolve_holiday_grace_dynamic(
    cfg: FunnelConfig,
    regime: str,
    money_flow: dict,
    gap_days: int,
) -> dict:
    result = {
        "enabled": bool(cfg.exit_holiday_grace_dynamic_enabled),
        "gap_days": gap_days,
        "extended": False,
        "exit_holiday_grace_days": int(cfg.exit_holiday_grace_days),
        "reason": "",
    }
    if not cfg.exit_holiday_grace_dynamic_enabled or gap_days < 3:
        result["reason"] = "not_holiday_gap"
        return result
    score = float(money_flow.get("score") or 0.0)
    trend = str(money_flow.get("trend") or "neutral")
    if regime in {"CRASH", "RISK_OFF"} or trend == "retreat" or score < cfg.exit_holiday_grace_min_money_flow_score:
        result["reason"] = f"no_extend: regime={regime}, trend={trend}, score={score:.1f}"
        return result
    old_days = int(cfg.exit_holiday_grace_days)
    cfg.exit_holiday_grace_days = max(old_days, int(cfg.exit_holiday_grace_max_days))
    result.update(
        {
            "extended": cfg.exit_holiday_grace_days > old_days,
            "exit_holiday_grace_days": int(cfg.exit_holiday_grace_days),
            "reason": f"money_flow={trend}, score={score:.1f}",
        }
    )
    return result


def _split_pv_conditions(outlook: str) -> list[dict[str, str]]:
    text = str(outlook or "").replace("次日推演：", "").replace("次日推演:", "").strip()
    conditions: list[dict[str, str]] = []
    for part in re.split(r"[；;]", text):
        item = part.strip(" ，,。")
        if not item:
            continue
        match = re.match(r"若(.+?)[，,](.+)", item)
        conditions.append({"if": match.group(1).strip(), "then": match.group(2).strip()} if match else {"text": item})
    return conditions


def derive_market_pv_policy_shadow(
    *,
    outlook: str,
    regime: str,
    price_zone: str,
    volume_state: str,
    money_flow: dict,
    cfg: FunnelConfig,
    regime_config: MarketRegimeConfig | None = None,
) -> dict:
    runtime = (regime_config or DEFAULT_MARKET_REGIME_CONFIG).normalized()
    text = str(outlook or "")
    flow_trend = str((money_flow or {}).get("trend") or "neutral")
    defensive_words = ("防守", "收缩", "失守", "跌破", "回避追高", "冲高回落")
    offensive_words = ("偏强", "放量站稳", "继续修复", "突破", "主力进场")
    defensive = regime in {"CRASH", "RISK_OFF", "BEAR_REBOUND"} or flow_trend == "retreat"
    offensive = regime == "RISK_ON" and flow_trend == "entry"
    if any(word in text for word in defensive_words):
        defensive = True
    if any(word in text for word in offensive_words):
        offensive = True
    overrides: dict[str, float | bool] = {}
    candidate_policy: dict[str, int] = {}
    risk_bias = "neutral"
    if defensive:
        risk_bias = "defensive"
        overrides = {
            "min_avg_amount_wan": max(float(cfg.min_avg_amount_wan), runtime.risk_off_min_avg_amount_wan),
            "rps_fast_min": max(float(cfg.rps_fast_min), 80.0),
            "rps_slow_min": max(float(cfg.rps_slow_min), 75.0),
        }
        candidate_policy = {"ai_total_cap_delta": -2}
    elif offensive:
        risk_bias = "offensive"
        overrides = {
            "rps_fast_min": min(float(cfg.rps_fast_min), 70.0),
            "rps_slow_min": min(float(cfg.rps_slow_min), 60.0),
        }
        candidate_policy = {"ai_total_cap_delta": 1}
    return {
        "mode": "shadow",
        "source": "market_pv_outlook",
        "risk_bias": risk_bias,
        "regime": regime,
        "price_zone": price_zone,
        "volume_state": volume_state,
        "money_flow_trend": flow_trend,
        "conditions": _split_pv_conditions(text),
        "funnel_config_overrides": overrides,
        "candidate_policy_overrides": candidate_policy,
    }


def _base_tuned_context(cfg: FunnelConfig) -> dict:
    return {
        "min_avg_amount_wan": cfg.min_avg_amount_wan,
        "rs_min_long": cfg.rs_min_long,
        "rs_min_short": cfg.rs_min_short,
        "rps_fast_min": cfg.rps_fast_min,
        "rps_slow_min": cfg.rps_slow_min,
        "exit_holiday_grace_days": cfg.exit_holiday_grace_days,
    }


def _base_breadth_context(regime_config: MarketRegimeConfig) -> dict:
    return {
        "ratio_pct": None,
        "prev_ratio_pct": None,
        "delta_pct": None,
        "sample_size": 0,
        "ma_window": regime_config.breadth_ma_window,
    }


def _base_holiday_grace_context(cfg: FunnelConfig) -> dict:
    return {
        "enabled": bool(cfg.exit_holiday_grace_dynamic_enabled),
        "gap_days": 0,
        "extended": False,
        "exit_holiday_grace_days": int(cfg.exit_holiday_grace_days),
        "reason": "",
    }


def _base_benchmark_context(
    cfg: FunnelConfig,
    money_flow_context: dict,
    amount_distribution_context: dict,
    regime_config: MarketRegimeConfig,
) -> dict:
    return {
        "regime": "UNKNOWN",
        "structural_regime": "UNKNOWN",
        "main_code": "000001",
        "close": None,
        "ma50": None,
        "ma200": None,
        "ma50_slope_5d": None,
        "recent3_pct": [],
        "recent3_cum_pct": None,
        "smallcap_code": regime_config.smallcap_bench_code,
        "smallcap_close": None,
        "smallcap_recent3_pct": [],
        "smallcap_recent3_cum_pct": None,
        "smallcap_today_pct": None,
        "panic_triggered": False,
        "panic_reasons": [],
        "repair_triggered": False,
        "repair_reasons": [],
        "bear_rebound_triggered": False,
        "bear_rebound_reasons": [],
        "tuned": _base_tuned_context(cfg),
        "breadth": _base_breadth_context(regime_config),
        "money_flow": money_flow_context,
        "amount_distribution": amount_distribution_context,
        "holiday_grace_dynamic": _base_holiday_grace_context(cfg),
        "market_pv_policy_shadow": {},
    }


def _recent_pct_metrics(
    frame: pd.DataFrame,
) -> tuple[list[float], float | None, float | None, float | None, float | None]:
    recent = frame["pct_chg"].dropna().tail(3)
    recent_list = [float(x) for x in recent.tolist()]
    recent_cum = float(((recent / 100.0 + 1.0).prod() - 1.0) * 100.0) if not recent.empty else None
    today_pct = float(recent_list[-1]) if recent_list else None
    prev_pct = float(recent_list[-2]) if len(recent_list) >= 2 else None
    prev2_pct = float(recent_list[-3]) if len(recent_list) >= 3 else None
    return recent_list, recent_cum, today_pct, prev_pct, prev2_pct


def _classify_volume_state(ratio: float | None) -> str:
    if ratio is None:
        return "未知"
    if ratio >= 1.15:
        return "放量"
    if ratio <= 0.85:
        return "缩量"
    return "平量"


def _main_benchmark_metrics(bench_df: pd.DataFrame | None) -> MainBenchmarkMetrics:
    if bench_df is None or bench_df.empty:
        return MainBenchmarkMetrics(recent3_list=[])
    frame = bench_df.sort_values("date").copy()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["pct_chg"] = pd.to_numeric(frame["pct_chg"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame.get("volume"), errors="coerce")
    if len(frame) < 60:
        return MainBenchmarkMetrics(recent3_list=[])
    close = float(frame["close"].iloc[-1])
    rolling50 = frame["close"].rolling(50)
    ma50 = float(rolling50.mean().iloc[-1])
    ma200 = float(frame["close"].rolling(200).mean().iloc[-1])
    ma50_prev = rolling50.mean().shift(5).iloc[-1]
    recent3_list, recent3_cum, today_pct, prev_pct, prev2_pct = _recent_pct_metrics(frame)
    vol = frame["volume"].dropna()
    vol_ma5 = vol_ma20 = vol_ratio = None
    if len(vol) >= 20:
        vol_ma20 = float(vol.tail(20).mean())
        vol_ma5 = float(vol.tail(5).mean())
        vol_ratio = float(vol_ma5 / vol_ma20) if vol_ma20 > 0 else None
    return MainBenchmarkMetrics(
        close=close,
        ma50=ma50,
        ma200=ma200,
        ma50_slope_5d=None if pd.isna(ma50_prev) else float(ma50 - ma50_prev),
        recent3_list=recent3_list,
        recent3_cum=recent3_cum,
        today_pct=today_pct,
        prev_pct=prev_pct,
        prev2_pct=prev2_pct,
        vol_ma5=vol_ma5,
        vol_ma20=vol_ma20,
        vol_ratio_5_20=vol_ratio,
        volume_state=_classify_volume_state(vol_ratio),
    )


def _smallcap_metrics(smallcap_df: pd.DataFrame | None) -> SmallcapMetrics:
    if smallcap_df is None or smallcap_df.empty:
        return SmallcapMetrics(recent3_list=[])
    frame = smallcap_df.sort_values("date").copy()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["pct_chg"] = pd.to_numeric(frame["pct_chg"], errors="coerce")
    if len(frame) < 10:
        return SmallcapMetrics(recent3_list=[])
    recent3_list, recent3_cum, today_pct, prev_pct, prev2_pct = _recent_pct_metrics(frame)
    return SmallcapMetrics(
        close=float(frame["close"].iloc[-1]),
        recent3_list=recent3_list,
        recent3_cum=recent3_cum,
        today_pct=today_pct,
        prev_pct=prev_pct,
        prev2_pct=prev2_pct,
    )


def _structural_regime(metrics: MainBenchmarkMetrics) -> str:
    required = (
        metrics.ma200,
        metrics.ma50,
        metrics.ma50_slope_5d,
        metrics.close,
    )
    if any(value is None for value in required):
        return "UNKNOWN"
    if metrics.close < metrics.ma200 and metrics.ma50 < metrics.ma200 and metrics.ma50_slope_5d < 0:
        return "BEAR"
    if metrics.close > metrics.ma50 > metrics.ma200 and metrics.ma50_slope_5d > 0:
        return "BULL"
    return "TRANSITION"


def _trend_regime(metrics: MainBenchmarkMetrics) -> str:
    structure = _structural_regime(metrics)
    if structure == "BEAR":
        return "RISK_OFF"
    if structure == "BULL" and metrics.recent3_cum is not None and metrics.recent3_cum >= 0.0:
        return "RISK_ON"
    return "NEUTRAL"


def _breadth_values(breadth: dict | None) -> tuple[float | None, float | None, float | None, int]:
    if not breadth:
        return None, None, None, 0
    return (
        breadth.get("ratio_pct"),
        breadth.get("prev_ratio_pct"),
        breadth.get("delta_pct"),
        int(breadth.get("sample_size") or 0),
    )


def _daily_breadth_values(breadth: dict | None) -> tuple[float | None, float | None, float | None, float | None]:
    if not breadth:
        return None, None, None, None
    return (
        _safe_float(breadth.get("daily_up_ratio_pct")),
        _safe_float(breadth.get("daily_median_pct_chg")),
        _safe_float(breadth.get("prev_daily_up_ratio_pct")),
        _safe_float(breadth.get("prev_daily_median_pct_chg")),
    )


def _apply_daily_breadth_regime(
    regime: str,
    daily_up_ratio: float | None,
    daily_median_pct: float | None,
    regime_config: MarketRegimeConfig,
) -> tuple[str, list[str]]:
    if daily_up_ratio is None:
        return regime, []
    if regime == "RISK_ON" and daily_up_ratio <= regime_config.daily_breadth_weak_threshold:
        return "CAUTION", [f"daily_breadth_weak={daily_up_ratio:.2f}%"]
    return regime, []


def _apply_breadth_regime(
    regime: str,
    breadth_ratio: float | None,
    breadth_delta: float | None,
    regime_config: MarketRegimeConfig,
) -> str:
    if breadth_ratio is None:
        return regime
    if float(breadth_ratio) <= regime_config.breadth_risk_off_threshold:
        regime = "RISK_OFF"
    elif float(breadth_ratio) >= regime_config.breadth_risk_on_threshold and (
        breadth_delta is None or float(breadth_delta) >= regime_config.breadth_risk_on_min_delta
    ):
        regime = "RISK_ON"
    if breadth_delta is not None and float(breadth_delta) <= regime_config.breadth_cliff_drop_pct:
        regime = "RISK_OFF"
    return regime


def _apply_caution_regime(regime: str, breadth_ratio: float | None, regime_config: MarketRegimeConfig) -> str:
    if regime != "RISK_ON" or breadth_ratio is None:
        return regime
    if float(breadth_ratio) < regime_config.breadth_risk_on_threshold:
        return "CAUTION"
    return regime


def _panic_reasons(
    main: MainBenchmarkMetrics,
    small: SmallcapMetrics,
    breadth_ratio: float | None,
    breadth_delta: float | None,
    regime_config: MarketRegimeConfig,
) -> list[str]:
    reasons: list[str] = []
    if main.today_pct is not None and float(main.today_pct) <= regime_config.crash_main_day_drop_pct:
        reasons.append(f"main_day_drop={main.today_pct:.2f}%<=阈值{regime_config.crash_main_day_drop_pct:.2f}%")
    if small.today_pct is not None and float(small.today_pct) <= regime_config.crash_small_day_drop_pct:
        reasons.append(f"smallcap_day_drop={small.today_pct:.2f}%<=阈值{regime_config.crash_small_day_drop_pct:.2f}%")
    if breadth_ratio is not None and float(breadth_ratio) <= regime_config.crash_breadth_ratio_pct:
        reasons.append(f"breadth_ratio={float(breadth_ratio):.2f}%<=阈值{regime_config.crash_breadth_ratio_pct:.2f}%")
    if breadth_delta is not None and float(breadth_delta) <= regime_config.crash_breadth_delta_pct:
        reasons.append(f"breadth_delta={float(breadth_delta):.2f}%<=阈值{regime_config.crash_breadth_delta_pct:.2f}%")
    return reasons


def _money_flow_panic_reason(money_flow: dict | None) -> str:
    if not money_flow:
        return ""
    trend = str(money_flow.get("trend") or "").strip().lower()
    score = _safe_float(money_flow.get("score"))
    up_down_ratio = _safe_float(money_flow.get("up_down_amount_ratio"))
    if trend == "retreat" and score is not None and score <= -20.0:
        return f"money_flow_retreat={score:.1f}<=阈值-20.0"
    if up_down_ratio is not None and up_down_ratio <= 0.55 and score is not None and score <= -12.0:
        return f"up_down_amount_ratio={up_down_ratio:.2f}<=阈值0.55"
    return ""


def _confirmed_panic_reasons(raw_reasons: list[str], money_flow: dict | None) -> list[str]:
    price_reasons = [reason for reason in raw_reasons if reason.startswith(("main_day_drop", "smallcap_day_drop"))]
    breadth_reasons = [reason for reason in raw_reasons if reason.startswith(("breadth_ratio", "breadth_delta"))]
    money_reason = _money_flow_panic_reason(money_flow)
    confirmation_reasons = [*breadth_reasons]
    if money_reason:
        confirmation_reasons.append(money_reason)
    if price_reasons and confirmation_reasons:
        return price_reasons + confirmation_reasons
    return []


def _repair_reasons(
    main: MainBenchmarkMetrics,
    small: SmallcapMetrics,
    regime_config: MarketRegimeConfig,
) -> list[str]:
    prev_panic = (main.prev_pct is not None and float(main.prev_pct) <= regime_config.crash_main_day_drop_pct) or (
        small.prev_pct is not None and float(small.prev_pct) <= regime_config.crash_small_day_drop_pct
    )
    rebound_ok = (
        main.today_pct is not None and float(main.today_pct) >= regime_config.panic_repair_main_rebound_pct
    ) or (small.today_pct is not None and float(small.today_pct) >= regime_config.panic_repair_small_rebound_pct)
    panic_reversal = prev_panic and rebound_ok
    if not panic_reversal:
        return []
    return [
        f"prev_panic(main_prev={main.prev_pct}, small_prev={small.prev_pct})",
        f"rebound_ok(main_today={main.today_pct}, small_today={small.today_pct})",
    ]


def _candidate_breadth_reasons(
    daily_up_ratio: float | None,
    daily_median_pct: float | None,
    regime_config: MarketRegimeConfig,
) -> list[str]:
    if daily_up_ratio is None or daily_up_ratio < regime_config.daily_breadth_repair_threshold:
        return []
    if daily_median_pct is None or daily_median_pct <= 0:
        return []
    return [f"candidate_breadth={daily_up_ratio:.2f}%", f"candidate_median={daily_median_pct:+.2f}%"]


def _confirmed_repair_reasons(
    main: MainBenchmarkMetrics,
    small: SmallcapMetrics,
    daily_up_ratio: float | None,
    daily_median_pct: float | None,
    prev_daily_up_ratio: float | None,
    prev_daily_median_pct: float | None,
    regime_config: MarketRegimeConfig,
) -> list[str]:
    prior_main = MainBenchmarkMetrics(today_pct=main.prev_pct, prev_pct=main.prev2_pct)
    prior_small = SmallcapMetrics(today_pct=small.prev_pct, prev_pct=small.prev2_pct)
    prior_price = _repair_reasons(prior_main, prior_small, regime_config)
    prior_breadth = _candidate_breadth_reasons(prev_daily_up_ratio, prev_daily_median_pct, regime_config)
    price_confirmed = main.today_pct is not None and main.today_pct >= regime_config.panic_repair_confirm_main_pct
    breadth_confirmed = (
        daily_up_ratio is not None
        and daily_up_ratio >= regime_config.panic_repair_confirm_breadth_pct
        and daily_median_pct is not None
        and daily_median_pct >= 0
    )
    if not (prior_price and prior_breadth and price_confirmed and breadth_confirmed):
        return []
    return [
        "prior_repair_candidate_confirmed",
        f"confirm_price={main.today_pct:+.2f}%",
        f"confirm_breadth={daily_up_ratio:.2f}%",
    ]


def _apply_panic_repair_regime(
    regime: str,
    main: MainBenchmarkMetrics,
    small: SmallcapMetrics,
    breadth_ratio: float | None,
    breadth_delta: float | None,
    money_flow: dict | None,
    daily_up_ratio: float | None,
    daily_median_pct: float | None,
    prev_daily_up_ratio: float | None,
    prev_daily_median_pct: float | None,
    regime_config: MarketRegimeConfig,
) -> tuple[str, list[str], list[str]]:
    panic_reasons = _panic_reasons(main, small, breadth_ratio, breadth_delta, regime_config)
    confirmed_panic = _confirmed_panic_reasons(panic_reasons, money_flow)
    if confirmed_panic:
        return "CRASH", confirmed_panic, []
    if panic_reasons:
        regime = "RISK_OFF"
    if not regime_config.panic_repair_enabled:
        return regime, [], []
    confirmed = _confirmed_repair_reasons(
        main,
        small,
        daily_up_ratio,
        daily_median_pct,
        prev_daily_up_ratio,
        prev_daily_median_pct,
        regime_config,
    )
    if confirmed:
        return "PANIC_REPAIR_CONFIRMED", [], confirmed
    price_reasons = _repair_reasons(main, small, regime_config)
    breadth_reasons = _candidate_breadth_reasons(daily_up_ratio, daily_median_pct, regime_config)
    repair_reasons = [*price_reasons, *breadth_reasons] if price_reasons and breadth_reasons else []
    return ("PANIC_REPAIR", [], repair_reasons) if repair_reasons else (regime, [], [])


def _bull_structure(metrics: MainBenchmarkMetrics) -> bool:
    return (
        metrics.close is not None
        and metrics.ma50 is not None
        and metrics.ma200 is not None
        and metrics.ma50_slope_5d is not None
        and metrics.close > metrics.ma50 > metrics.ma200
        and metrics.ma50_slope_5d > 0
    )


def _apply_bear_rebound_regime(regime: str, metrics: MainBenchmarkMetrics) -> tuple[str, list[str]]:
    if regime != "RISK_ON" or _bull_structure(metrics):
        return regime, []
    reasons: list[str] = []
    if metrics.close is not None and metrics.ma200 is not None and metrics.close < metrics.ma200:
        reasons.append("close_below_ma200")
    if metrics.ma50 is not None and metrics.ma200 is not None and metrics.ma50 <= metrics.ma200:
        reasons.append("ma50_below_ma200")
    if metrics.ma50_slope_5d is not None and metrics.ma50_slope_5d <= 0:
        reasons.append("ma50_slope_non_positive")
    if not reasons:
        reasons.append("risk_on_without_bull_structure")
    return "BEAR_REBOUND", reasons


def _apply_evr_policy(cfg: FunnelConfig, regime: str, regime_config: MarketRegimeConfig) -> None:
    evr_policy = regime_config.evr_policy
    if evr_policy in {"cold_only", "risk_off", "risk_off_crash"}:
        cfg.enable_evr_trigger = regime in {"RISK_OFF", "CRASH"}
    elif evr_policy in {"off", "disabled", "disable", "0", "false", "no"}:
        cfg.enable_evr_trigger = False
    elif evr_policy in {"respect_cfg", "cfg", "config"}:
        cfg.enable_evr_trigger = bool(cfg.enable_evr_trigger)
    else:
        cfg.enable_evr_trigger = True


def _tune_cfg_for_regime(
    cfg: FunnelConfig,
    regime: str,
    recent3_cum: float | None,
    regime_config: MarketRegimeConfig,
) -> None:
    if regime == "CRASH":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, regime_config.crash_min_avg_amount_wan)
        cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
    elif regime in {"PANIC_REPAIR", "PANIC_REPAIR_CONFIRMED"}:
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, regime_config.panic_repair_min_avg_amount_wan)
        cfg.rs_min_long = max(cfg.rs_min_long, 1.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.2)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 75.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 65.0)
    elif regime == "RISK_OFF":
        _tune_risk_off_cfg(cfg, recent3_cum, regime_config)
    elif regime == "BEAR_REBOUND":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, regime_config.risk_off_min_avg_amount_wan)
        cfg.rs_min_long = max(cfg.rs_min_long, 3.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
    elif regime == "RISK_ON":
        cfg.rs_min_long = max(cfg.rs_min_long, 0.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.0)
        cfg.rps_fast_min = min(cfg.rps_fast_min, 70.0)
        cfg.rps_slow_min = min(cfg.rps_slow_min, 60.0)
    _apply_trigger_threshold_profile(cfg, regime)


def _apply_trigger_threshold_profile(cfg: FunnelConfig, regime: str) -> None:
    if not cfg.regime_trigger_profiles_enabled:
        return
    if regime == "RISK_ON":
        multiplier = cfg.regime_risk_on_volume_multiplier
    elif regime in {"CRASH", "RISK_OFF", "BEAR_REBOUND"}:
        multiplier = cfg.regime_defensive_volume_multiplier
    elif regime in {"PANIC_REPAIR", "PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY"}:
        multiplier = cfg.regime_repair_volume_multiplier
    else:
        return
    cfg.sos_vol_ratio *= multiplier
    cfg.spring_vol_ratio *= multiplier
    cfg.evr_vol_ratio *= multiplier


def _tune_risk_off_cfg(
    cfg: FunnelConfig,
    recent3_cum: float | None,
    regime_config: MarketRegimeConfig,
) -> None:
    cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, regime_config.risk_off_min_avg_amount_wan)
    cfg.rs_min_long = max(cfg.rs_min_long, 2.0)
    cfg.rs_min_short = max(cfg.rs_min_short, 0.5)
    cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
    cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
    if recent3_cum is not None and recent3_cum <= -4.0:
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, regime_config.risk_off_deep_min_avg_amount_wan)
        cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)


def _price_zone(metrics: MainBenchmarkMetrics) -> str:
    if metrics.close is None or metrics.ma50 is None or metrics.ma200 is None:
        return "结构待确认"
    if metrics.close > metrics.ma50 > metrics.ma200:
        return "多头上方"
    if metrics.close < metrics.ma50 < metrics.ma200:
        return "空头下方"
    if metrics.close >= metrics.ma50 and metrics.close <= metrics.ma200:
        return "反抽修复区"
    if metrics.close < metrics.ma50 and metrics.close >= metrics.ma200:
        return "高位回撤区"
    return "震荡博弈区"


def _final_tuned_context(cfg: FunnelConfig) -> dict:
    tuned = _base_tuned_context(cfg)
    tuned["enable_evr_trigger"] = bool(cfg.enable_evr_trigger)
    return tuned


def _final_breadth_context(
    breadth_ratio: float | None,
    breadth_prev: float | None,
    breadth_delta: float | None,
    breadth_sample: int,
    breadth: dict | None,
    regime_config: MarketRegimeConfig,
) -> dict:
    return {
        "ratio_pct": breadth_ratio,
        "prev_ratio_pct": breadth_prev,
        "delta_pct": breadth_delta,
        "sample_size": breadth_sample,
        "ma_window": regime_config.breadth_ma_window,
        "daily_sample_size": int((breadth or {}).get("daily_sample_size") or 0),
        "daily_up_count": int((breadth or {}).get("daily_up_count") or 0),
        "daily_down_count": int((breadth or {}).get("daily_down_count") or 0),
        "daily_flat_count": int((breadth or {}).get("daily_flat_count") or 0),
        "daily_up_ratio_pct": (breadth or {}).get("daily_up_ratio_pct"),
        "daily_median_pct_chg": (breadth or {}).get("daily_median_pct_chg"),
        "daily_average_pct_chg": (breadth or {}).get("daily_average_pct_chg"),
        "prev_daily_sample_size": int((breadth or {}).get("prev_daily_sample_size") or 0),
        "prev_daily_up_ratio_pct": (breadth or {}).get("prev_daily_up_ratio_pct"),
        "prev_daily_median_pct_chg": (breadth or {}).get("prev_daily_median_pct_chg"),
    }


def _market_pv_context(
    main: MainBenchmarkMetrics,
    regime: str,
    money_flow_context: dict,
    cfg: FunnelConfig,
    regime_config: MarketRegimeConfig,
) -> dict:
    price_zone = _price_zone(main)
    ratio_text = f"{main.vol_ratio_5_20:.2f}x" if main.vol_ratio_5_20 is not None else "未知"
    outlook = _generate_pv_outlook(
        regime=regime,
        close=main.close,
        ma50=main.ma50,
        ma200=main.ma200,
        price_zone=price_zone,
        vol_ratio_text=ratio_text,
        volume_state=main.volume_state,
        recent3_cum=main.recent3_cum,
        provider=regime_config.pv_llm_provider,
    )
    return {
        "market_pv_summary": f"沪深300近5日均量/20日均量={ratio_text}（{main.volume_state}），当前位于{price_zone}。",
        "market_pv_outlook": outlook,
        "market_pv_policy_shadow": derive_market_pv_policy_shadow(
            outlook=outlook,
            regime=regime,
            price_zone=price_zone,
            volume_state=main.volume_state,
            money_flow=money_flow_context,
            cfg=cfg,
            regime_config=regime_config,
        ),
    }


def _benchmark_result_context(
    *,
    main: MainBenchmarkMetrics,
    small: SmallcapMetrics,
    regime: str,
    structural_regime: str,
    cfg: FunnelConfig,
    breadth_context: dict,
    money_flow_context: dict,
    amount_distribution_context: dict,
    holiday_grace_dynamic: dict,
    panic_reasons: list[str],
    repair_reasons: list[str],
    bear_rebound_reasons: list[str],
    regime_config: MarketRegimeConfig,
) -> dict:
    repair_stage = (
        "candidate" if regime == "PANIC_REPAIR" else "confirmed" if regime == "PANIC_REPAIR_CONFIRMED" else ""
    )
    result = {
        "regime": regime,
        "structural_regime": structural_regime,
        "close": main.close,
        "ma50": main.ma50,
        "ma200": main.ma200,
        "ma50_slope_5d": main.ma50_slope_5d,
        "recent3_pct": main.recent3_list or [],
        "recent3_cum_pct": main.recent3_cum,
        "main_today_pct": main.today_pct,
        "main_vol_ma5": main.vol_ma5,
        "main_vol_ma20": main.vol_ma20,
        "main_vol_ratio_5_20": main.vol_ratio_5_20,
        "main_volume_state": main.volume_state,
        "smallcap_close": small.close,
        "smallcap_recent3_pct": small.recent3_list or [],
        "smallcap_recent3_cum_pct": small.recent3_cum,
        "smallcap_today_pct": small.today_pct,
        "panic_triggered": bool(panic_reasons),
        "panic_reasons": panic_reasons,
        "repair_triggered": bool(repair_reasons),
        "repair_stage": repair_stage,
        "repair_candidate_triggered": repair_stage == "candidate",
        "repair_confirmed": repair_stage == "confirmed",
        "repair_reasons": repair_reasons,
        "bear_rebound_triggered": bool(bear_rebound_reasons),
        "bear_rebound_reasons": bear_rebound_reasons,
        "tuned": _final_tuned_context(cfg),
        "breadth": breadth_context,
        "money_flow": money_flow_context,
        "amount_distribution": amount_distribution_context,
        "holiday_grace_dynamic": holiday_grace_dynamic,
    }
    result.update(_market_pv_context(main, regime, money_flow_context, cfg, regime_config))
    return result


def analyze_benchmark_and_tune_cfg(
    bench_df: pd.DataFrame | None,
    smallcap_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    breadth: dict | None = None,
    money_flow: dict | None = None,
    amount_distribution: dict | None = None,
    regime_config: MarketRegimeConfig | None = None,
) -> dict:
    """输出市场结构与短期水温，并在弱市收紧漏斗。"""
    runtime = (regime_config or DEFAULT_MARKET_REGIME_CONFIG).normalized()
    money_flow_context = money_flow or calc_market_money_flow({}, breadth)
    amount_distribution_context = amount_distribution or calc_amount_distribution_health({}, cfg.min_avg_amount_wan)
    context = _base_benchmark_context(cfg, money_flow_context, amount_distribution_context, runtime)
    main = _main_benchmark_metrics(bench_df)
    small = _smallcap_metrics(smallcap_df)
    structural_regime = _structural_regime(main)
    breadth_ratio, breadth_prev, breadth_delta, breadth_sample = _breadth_values(breadth)
    daily_up_ratio, daily_median_pct, prev_daily_up_ratio, prev_daily_median_pct = _daily_breadth_values(breadth)
    regime = _apply_breadth_regime(_trend_regime(main), breadth_ratio, breadth_delta, runtime)
    regime = _apply_caution_regime(regime, breadth_ratio, runtime)
    regime, panic_reasons, repair_reasons = _apply_panic_repair_regime(
        regime,
        main,
        small,
        breadth_ratio,
        breadth_delta,
        money_flow_context,
        daily_up_ratio,
        daily_median_pct,
        prev_daily_up_ratio,
        prev_daily_median_pct,
        runtime,
    )
    regime, _ = _apply_daily_breadth_regime(regime, daily_up_ratio, daily_median_pct, runtime)
    regime, bear_rebound_reasons = _apply_bear_rebound_regime(regime, main)
    _apply_evr_policy(cfg, regime, runtime)
    _tune_cfg_for_regime(cfg, regime, main.recent3_cum, runtime)
    holiday_grace_dynamic = _resolve_holiday_grace_dynamic(
        cfg, regime, money_flow_context, _latest_trade_gap_days(bench_df)
    )
    breadth_context = _final_breadth_context(
        breadth_ratio,
        breadth_prev,
        breadth_delta,
        breadth_sample,
        breadth,
        runtime,
    )
    context.update(
        _benchmark_result_context(
            main=main,
            small=small,
            regime=regime,
            structural_regime=structural_regime,
            cfg=cfg,
            breadth_context=breadth_context,
            money_flow_context=money_flow_context,
            amount_distribution_context=amount_distribution_context,
            holiday_grace_dynamic=holiday_grace_dynamic,
            panic_reasons=panic_reasons,
            repair_reasons=repair_reasons,
            bear_rebound_reasons=bear_rebound_reasons,
            regime_config=runtime,
        )
    )
    return context
