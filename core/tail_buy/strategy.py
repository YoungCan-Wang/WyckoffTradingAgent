"""
尾盘买入策略核心（规则层 + LLM 合并层）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import pandas as pd

from core._price_math import ret_pct as _ret_pct
from core.candidate_report_semantics import candidate_reason_payload, optional_candidate_score
from core.cn_boards import cn_board
from core.intraday_analysis import (
    compute_effort_vs_result,
    compute_smart_money_score,
    compute_spring_quality,
    compute_vol_price_corr,
    ensure_intraday_df,
    infer_session_vwap,
)
from core.limit_move import classify_limit_move
from core.strategy_policy_display import (
    policy_execution_mode_label,
    policy_formal_dynamic_label,
    policy_next_action_label,
)
from core.strategy_policy_governor import resolve_signal_weight_multiplier
from core.tail_buy.guardrails import tail_candidate_veto_reasons, tail_entry_veto_reasons, tail_hard_veto_reasons
from core.tail_buy.models import (
    DECISION_BUY,
    DECISION_SKIP,
    DECISION_WATCH,
    VALID_DECISIONS,
    TailBuyCandidate,
    normalize_cn_code,
    normalize_regime,
    normalize_status,
    safe_float,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TailBuyStrategyConfig:
    confirmed_only_buy: bool = True
    support_breach_tolerance_pct: float = 0.3
    blowoff_high_ret_pct: float = 5.0
    blowoff_drop_from_high_pct: float = 2.2
    blowoff_close_pos_max: float = 0.58
    blowoff_tail_volume_share: float = 0.45
    chase_day_ret_pct: float = 10.0
    chase_high_ret_pct: float = 12.0
    weak_naked_day_ret_pct: float = 0.8
    weak_naked_tail30_ret_pct: float = 0.3
    naked_support_extension_pct: float = 18.0
    daily_trap_gate_enabled: bool = True
    daily_trap_ma20_extension_pct: float = 18.0
    daily_trap_upper_shadow_pct: float = 4.0
    daily_trap_volume_ratio: float = 1.8


DEFAULT_TAIL_BUY_STRATEGY_CONFIG = TailBuyStrategyConfig()

# 创业板/科创板日常涨跌幅上限为主板的 2 倍（20% vs 10%），"日内冲高/追高"这类
# 直接以百分比涨幅为门槛的判断，理应按同样比例放宽，否则这些板块的正常波动
# 会被系统性误判为"追高/冲高滞涨"，导致该买的票被尾盘策略拦在门外。
_REGISTRATION_BOARD_RET_SCALE = 2.0
_RET_SCALED_FIELDS = (
    "blowoff_high_ret_pct",
    "chase_day_ret_pct",
    "chase_high_ret_pct",
    "naked_support_extension_pct",
    "daily_trap_ma20_extension_pct",
)


def board_scaled_strategy_config(config: TailBuyStrategyConfig | None, code: str) -> TailBuyStrategyConfig:
    """按股票所属板块（主板/北交所 vs 创业板/科创板）缩放涨跌幅类阈值。"""
    base = _strategy_config(config)
    if cn_board(code) not in {"chinext", "star"}:
        return base
    updates = {field: getattr(base, field) * _REGISTRATION_BOARD_RET_SCALE for field in _RET_SCALED_FIELDS}
    return replace(base, **updates)


def _strategy_config(config: TailBuyStrategyConfig | None) -> TailBuyStrategyConfig:
    return config or DEFAULT_TAIL_BUY_STRATEGY_CONFIG


def _apply_unconfirmed_buy_gate(
    candidate: TailBuyCandidate,
    config: TailBuyStrategyConfig | None = None,
) -> TailBuyCandidate:
    if not _strategy_config(config).confirmed_only_buy or normalize_status(candidate.status) == "confirmed":
        return candidate
    if candidate.rule_decision == DECISION_BUY:
        candidate.rule_decision = DECISION_WATCH
        candidate.rule_reasons.append("未二次确认，尾盘只观察不买入")
    if candidate.final_decision == DECISION_BUY:
        candidate.final_decision = DECISION_WATCH
        candidate.llm_reason = (
            f"{candidate.llm_reason}；未二次确认，降级观察" if candidate.llm_reason else "未二次确认，降级观察"
        )
    if candidate.final_decision == DECISION_WATCH:
        candidate.priority_score = _priority_score(candidate.rule_score + 3.0)
    return candidate


def _priority_score(raw: float) -> float:
    return min(safe_float(raw, 0.0), 100.0)


def _normalize_signal_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if len(text) >= 10:
        return text[:10]
    return text


def pick_tail_candidates(
    rows: list[dict[str, Any]],
    *,
    cutoff_date: str,
    statuses: tuple[str, ...] = ("pending", "confirmed"),
) -> list[TailBuyCandidate]:
    """从 signal_pending 原始行中过滤候选：
    - signal_date >= cutoff_date
    - status in statuses
    - 同代码只保留更优记录（confirmed > pending；日期更新优先；分数更高优先）
    """
    allowed = {str(x).strip().lower() for x in statuses}
    cutoff = _normalize_signal_date(cutoff_date)
    by_code: dict[str, TailBuyCandidate] = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        signal_date = _normalize_signal_date(row.get("signal_date"))
        if signal_date < cutoff:
            continue
        status = normalize_status(row.get("status"))
        if status not in allowed:
            continue
        code = normalize_cn_code(row.get("code"))
        if not code:
            continue
        candidate = TailBuyCandidate(
            code=code,
            name=str(row.get("name", "") or code).strip() or code,
            signal_date=signal_date,
            status=status,
            signal_type=str(row.get("signal_type", "") or "").strip() or "unknown",
            signal_score=safe_float(row.get("signal_score"), 0.0),
            market_regime=normalize_regime(row.get("regime") or row.get("market_regime")),
            candidate_lane=str(row.get("candidate_lane", "") or "").strip(),
            entry_type=str(row.get("entry_type", "") or "").strip(),
            signal_key=str(row.get("signal_key", "") or "").strip(),
            candidate_status=str(row.get("candidate_status", "") or "").strip(),
            candidate_reasons=candidate_reason_payload(row.get("candidate_reasons")),
            candidate_theme=str(row.get("candidate_theme", "") or "").strip(),
            candidate_phase=str(row.get("candidate_phase", "") or "").strip(),
            candidate_role=str(row.get("candidate_role", "") or "").strip(),
            mainline_score=optional_candidate_score(row.get("mainline_score")),
            theme_score=optional_candidate_score(row.get("theme_score")),
            stock_role_score=optional_candidate_score(row.get("stock_role_score")),
            snap={k: v for k, v in row.items() if k.startswith("snap_")},
        )
        old = by_code.get(code)
        if old is None:
            by_code[code] = candidate
            continue
        old_rank = (old.signal_date, 1 if old.status == "confirmed" else 0, old.signal_score)
        new_rank = (candidate.signal_date, 1 if candidate.status == "confirmed" else 0, candidate.signal_score)
        if new_rank > old_rank:
            by_code[code] = candidate

    out = list(by_code.values())
    out.sort(key=lambda x: (x.status != "confirmed", -x.signal_score, x.code))
    return out


def _support_guard_features(
    *,
    last_close: float,
    day_low: float,
    daily_context: dict[str, Any] | None,
    config: TailBuyStrategyConfig,
) -> dict[str, Any]:
    support = safe_float((daily_context or {}).get("support_level"), 0.0)
    if support <= 0:
        return {
            "support_level": 0.0,
            "close_vs_support_pct": 0.0,
            "day_low_vs_support_pct": 0.0,
            "close_below_support": False,
            "day_low_breached_support": False,
        }
    tolerance_pct = max(config.support_breach_tolerance_pct, 0.0)
    support_floor = support * (1.0 - tolerance_pct / 100.0)
    return {
        "support_level": support,
        "close_vs_support_pct": (last_close / support - 1.0) * 100.0,
        "day_low_vs_support_pct": (day_low / support - 1.0) * 100.0,
        "close_below_support": bool(last_close < support_floor),
        "day_low_breached_support": bool(day_low < support_floor),
    }


def _is_tail_blowoff_reversal(features: dict[str, Any], config: TailBuyStrategyConfig) -> bool:
    high_ret = safe_float(features.get("intraday_high_ret_pct"), 0.0)
    drop = safe_float(features.get("drop_from_high_pct"), 0.0)
    close_pos = safe_float(features.get("close_pos"), 0.0)
    tail_share = safe_float(features.get("tail30_volume_share"), 0.0)
    return (
        high_ret >= config.blowoff_high_ret_pct
        and drop <= -abs(config.blowoff_drop_from_high_pct)
        and close_pos <= config.blowoff_close_pos_max
        and tail_share >= config.blowoff_tail_volume_share
    )


def _daily_trap_features(daily_history: pd.DataFrame | None, config: TailBuyStrategyConfig) -> dict[str, Any]:
    if not config.daily_trap_gate_enabled or daily_history is None or daily_history.empty:
        return {}
    work = _daily_ohlcv(daily_history)
    if len(work) < 20:
        return {}
    close = work["close"]
    last = work.iloc[-1]
    ma20 = safe_float(close.tail(20).mean(), 0.0)
    vol_ref = safe_float(work["volume"].iloc[:-1].tail(20).mean(), 0.0)
    daily = _daily_candle_metrics(last, ma20, vol_ref)
    reasons = _daily_trap_reasons(daily, config)
    return {
        "daily_close_vs_ma20_pct": daily["close_vs_ma20_pct"],
        "daily_upper_shadow_pct": daily["upper_shadow_pct"],
        "daily_volume_ratio": daily["volume_ratio"],
        "daily_close_pos": daily["close_pos"],
        "daily_trap_pressure": bool(reasons),
        "daily_trap_reason": "；".join(reasons),
    }


def _limit_up_features(
    candidate: TailBuyCandidate,
    daily_history: pd.DataFrame | None,
    day_high: float,
    day_low: float,
    last_close: float,
) -> dict[str, Any]:
    """基于日K倒数第二行（昨收）+ 当日分钟线高低点，判断是否触及/收于涨停。

    涨停时无法按现价挂单买入，若不识别会与普通可挂单信号混在同一个 BUY 分类里，
    误导为"现在可执行"。
    """
    work = _daily_ohlcv(daily_history) if daily_history is not None else pd.DataFrame()
    if len(work) < 2:
        return {}
    prev_close = safe_float(work["close"].iloc[-2], 0.0)
    if prev_close <= 0:
        return {}
    state = classify_limit_move(
        code=candidate.code,
        name=candidate.name,
        prev_close=prev_close,
        open_=day_high,
        high=day_high,
        low=day_low,
        close=last_close,
    )
    if state is None:
        return {}
    return {
        "limit_up_touched": bool(state.touched_limit_up),
        "limit_up_closed": bool(state.closed_limit_up),
    }


def _daily_ohlcv(daily_history: pd.DataFrame) -> pd.DataFrame:
    cols = ("open", "high", "low", "close", "volume")
    if not set(cols).issubset(daily_history.columns):
        return pd.DataFrame(columns=cols)
    work = daily_history[list(cols)].copy()
    for col in cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    return work.dropna(subset=list(cols)).tail(40)


def _daily_candle_metrics(last: pd.Series, ma20: float, vol_ref: float) -> dict[str, float]:
    close = safe_float(last.get("close"), 0.0)
    high = safe_float(last.get("high"), close)
    low = safe_float(last.get("low"), close)
    open_ = safe_float(last.get("open"), close)
    day_range = max(high - low, 1e-8)
    return {
        "close_vs_ma20_pct": (close / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0,
        "upper_shadow_pct": (high - max(open_, close)) / close * 100.0 if close > 0 else 0.0,
        "volume_ratio": safe_float(last.get("volume"), 0.0) / vol_ref if vol_ref > 0 else 0.0,
        "close_pos": max(0.0, min(1.0, (close - low) / day_range)),
    }


def _daily_trap_reasons(daily: dict[str, float], config: TailBuyStrategyConfig) -> list[str]:
    reasons: list[str] = []
    if daily["close_vs_ma20_pct"] >= config.daily_trap_ma20_extension_pct and daily["close_pos"] < 0.7:
        reasons.append(f"日线远离MA20({daily['close_vs_ma20_pct']:.1f}%)且收位不强")
    if (
        daily["upper_shadow_pct"] >= config.daily_trap_upper_shadow_pct
        and daily["volume_ratio"] >= config.daily_trap_volume_ratio
        and daily["close_pos"] < 0.65
    ):
        reasons.append(f"日线放量上影({daily['volume_ratio']:.1f}x)")
    return reasons


def _candidate_context_features(candidate: TailBuyCandidate, df: pd.DataFrame) -> dict[str, Any]:
    trade_date = _last_intraday_date(df)
    signal_date = _parse_date(candidate.signal_date)
    age_days = (trade_date - signal_date).days if trade_date and signal_date else 0
    return {
        "signal_age_days": max(age_days, 0),
        "candidate_status": candidate.status,
        "candidate_lane": candidate.candidate_lane,
        "entry_type": candidate.entry_type,
    }


def _last_intraday_date(df: pd.DataFrame) -> date | None:
    if df.empty or "datetime" not in df.columns:
        return None
    ts = pd.to_datetime(df["datetime"], errors="coerce").dropna()
    if ts.empty:
        return None
    return ts.iloc[-1].date()


def _latest_intraday_session(df: pd.DataFrame) -> pd.DataFrame:
    trade_date = _last_intraday_date(df)
    if trade_date is None:
        return df
    return df[df["datetime"].dt.date == trade_date].reset_index(drop=True)


def _parse_date(raw: Any) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _tail_volume_share(volume: pd.Series, total_volume: float, lookback: int) -> float:
    return float(volume.tail(min(lookback, len(volume))).sum()) / total_volume if total_volume > 0 else 0.0


def _reclaim_vwap(close: pd.Series, *, last_close: float, vwap: float) -> bool:
    history_window = min(90, max(len(close) - 1, 1))
    history_before_tail = close.iloc[: -min(20, len(close))] if len(close) > 20 else close.iloc[:-1]
    if history_before_tail.empty:
        history_before_tail = close.iloc[:-1]
    min_before_tail = safe_float(history_before_tail.tail(history_window).min(), last_close)
    return bool(last_close >= vwap * 1.001 and min_before_tail < vwap * 0.998)


def _strong_hold_vwap_ratio(
    close: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    amount: pd.Series,
    volume_scale: float,
) -> float:
    if len(close) < 60:
        return 0.0
    scaled_volume = (volume.fillna(0.0) * max(volume_scale, 1e-9)).clip(lower=0.0)
    cum_vol = scaled_volume.cumsum()
    dynamic_vwap = amount.fillna(0.0).cumsum() / cum_vol.replace(0.0, pd.NA)
    valid = dynamic_vwap.dropna()
    if valid.empty:
        return 0.0
    start = min(max(len(close) // 6, 15), 40)
    close_tail = close.iloc[start:]
    low_tail = low.iloc[start:]
    vwap_tail = dynamic_vwap.iloc[start:]
    if close_tail.empty:
        return 0.0
    close_above = close_tail >= vwap_tail * 0.998
    low_breach = low_tail < vwap_tail * 0.993
    return float(close_above.mean() - low_breach.mean() * 0.5)


def _breakout_tail(high: pd.Series, *, last_close: float, day_high: float) -> bool:
    if len(high) <= 35:
        return bool(last_close >= day_high * 0.98)
    prev_peak = safe_float(high.iloc[:-30].max(), day_high)
    return bool(last_close >= prev_peak * 0.998)


def _slope_pct(close: pd.Series, lookback: int) -> float:
    if len(close) < lookback:
        return 0.0
    base = safe_float(close.iloc[-lookback], 0.0)
    return 0.0 if base <= 0 else (safe_float(close.iloc[-1], 0.0) / base - 1.0) * 100.0


def _base_tail_features(df: pd.DataFrame) -> dict[str, Any]:
    close = df["close"].ffill()
    high = df["high"].fillna(close)
    low = df["low"].fillna(close)
    volume = df["volume"].fillna(0.0)
    amount = df["amount"].fillna(close * volume)
    first_open = safe_float(df["open"].iloc[0] if "open" in df.columns else close.iloc[0], close.iloc[0])
    last_close = safe_float(close.iloc[-1], 0.0)
    day_high = safe_float(high.max(), last_close)
    day_low = safe_float(low.min(), last_close)
    total_volume = float(volume.sum())
    vwap, vwap_volume_scale = infer_session_vwap(close, total_volume, float(amount.sum()))
    day_range = max(day_high - day_low, 1e-8)
    close_pos = max(0.0, min(1.0, (last_close - day_low) / day_range))
    day_ret_pct = ((last_close / first_open - 1.0) * 100.0) if first_open > 0 else 0.0
    drop_from_high_pct = (last_close / day_high - 1.0) * 100.0 if day_high > 0 else 0.0
    hold_vwap_ratio = _strong_hold_vwap_ratio(close, low, volume, amount, vwap_volume_scale)
    strong_hold_vwap = (
        hold_vwap_ratio >= 0.82 and close_pos >= 0.72 and day_ret_pct >= 0.5 and drop_from_high_pct >= -1.8
    )
    return {
        "bars": int(len(df)),
        "last_close": last_close,
        "first_open": first_open,
        "day_high": day_high,
        "day_low": day_low,
        "vwap": vwap,
        "vwap_volume_scale": vwap_volume_scale,
        "close_pos": close_pos,
        "day_ret_pct": day_ret_pct,
        "intraday_high_ret_pct": ((day_high / first_open - 1.0) * 100.0) if first_open > 0 else 0.0,
        "last30_ret_pct": _ret_pct(close, 30),
        "last15_ret_pct": _ret_pct(close, 15),
        "tail30_volume_share": _tail_volume_share(volume, total_volume, 30),
        "tail15_volume_share": _tail_volume_share(volume, total_volume, 15),
        "drop_from_high_pct": drop_from_high_pct,
        "dist_vwap_pct": (last_close / vwap - 1.0) * 100.0 if vwap > 0 else 0.0,
        "hold_vwap_ratio": hold_vwap_ratio,
        "strong_hold_vwap": bool(strong_hold_vwap),
        "reclaim_vwap": _reclaim_vwap(close, last_close=last_close, vwap=vwap),
        "breakout_tail": _breakout_tail(high, last_close=last_close, day_high=day_high),
        "slope_10_pct": _slope_pct(close, 10),
    }


def compute_tail_features(
    df_1m: pd.DataFrame,
    daily_context: dict[str, Any] | None = None,
    *,
    config: TailBuyStrategyConfig | None = None,
) -> dict[str, Any]:
    df = ensure_intraday_df(df_1m)
    df = _latest_intraday_session(df)
    if df.empty:
        return {"bars": 0}

    policy = _strategy_config(config)
    features = _base_tail_features(df)
    features.update(
        {
            "vol_price_corr": compute_vol_price_corr(df),
            "effort_vs_result": compute_effort_vs_result(df),
            "smart_money_score": compute_smart_money_score(df),
            "spring_quality": compute_spring_quality(df, daily_context) if daily_context else None,
        }
    )
    features.update(
        _support_guard_features(
            last_close=safe_float(features.get("last_close"), 0.0),
            day_low=safe_float(features.get("day_low"), 0.0),
            daily_context=daily_context,
            config=policy,
        )
    )
    features["tail_blowoff_reversal"] = _is_tail_blowoff_reversal(features, policy)
    return features


_SIGNAL_TYPE_STYLE: dict[str, str] = {
    "sos": "trend",
    "jac": "trend",
    "trend_breakout": "trend",
    "main_force_entry": "trend",
    "sector_strength": "trend",
    "wyckoff_structure": "trend",
    "mainline": "trend",
    "spring": "pullback",
    "lps": "pullback",
    "trend_pullback": "pullback",
    "trend_lane_pullback": "pullback",
    "rec_deep_pullback": "pullback",
    "evr": "hybrid",
    "compression": "hybrid",
    "rec_momentum_continuation": "trend",
}


def _normalize_signal_score(signal_score: float, signal_type: str) -> float:
    """按信号类型归一化 score 到 0-10 统一量纲。"""
    st = signal_type.strip().lower()
    raw = max(signal_score, 0.0)
    if st == "lps":
        # LPS score 是缩量比，越小越好（0.2=极干, 0.65=阈值边界）
        normalized = max(0.0, (0.65 - raw) / 0.65) * 10.0
    elif st in ("sos", "jac"):
        # SOS score 是放量倍数（2.5-8+），线性映射到 0-10
        normalized = min((raw - 2.0) / 4.0, 1.0) * 10.0
    elif st == "evr":
        # EVR score 是放量倍数（1.3-5+），线性映射
        normalized = min((raw - 1.0) / 3.0, 1.0) * 10.0
    elif st == "spring":
        # Spring score 是回升幅度%（0-10+），天然接近 0-10
        normalized = raw
    elif st in {
        "mainline",
        "main_force_entry",
        "trend_breakout",
        "trend_lane_pullback",
        "sector_strength",
        "wyckoff_structure",
        "rec_deep_pullback",
        "rec_momentum_continuation",
    }:
        normalized = raw / 10.0 if raw > 10.0 else raw
    else:
        normalized = raw
    return min(max(normalized, 0.0), 10.0)


def _tail_style_bias(signal_type: str, style: str) -> tuple[str, float, float]:
    st_lower = signal_type.strip().lower()
    style_norm = str(style or "").strip().lower()
    if not style_norm or style_norm == "auto":
        style_norm = _SIGNAL_TYPE_STYLE.get(st_lower, "hybrid")
    if style_norm == "trend":
        return st_lower, 1.2, 0.8
    if style_norm in {"pullback", "reclaim"}:
        return st_lower, 0.8, 1.2
    return st_lower, 1.0, 1.0


def _score_signal_context(signal_score: float, st_lower: str, status: str, reasons: list[str]) -> float:
    score = 35.0
    sig_boost = _normalize_signal_score(signal_score, st_lower) * 1.6
    if sig_boost > 0:
        score += sig_boost
        reasons.append(f"漏斗信号加分({st_lower or '?'}) +{sig_boost:.1f}")
    if str(status).lower() == "confirmed":
        score += 6.0
        reasons.append("确认信号加分 +6.0")
    return score


def _score_tail_position(features: dict[str, Any], trend_bias: float, reasons: list[str]) -> float:
    score = 0.0
    dist_vwap_pct = safe_float(features.get("dist_vwap_pct"), 0.0)
    if dist_vwap_pct >= 0.8:
        score += 16.0 * trend_bias
        reasons.append("尾盘在VWAP上方且有距离")
    elif dist_vwap_pct >= 0.0:
        score += 8.0 * trend_bias
        reasons.append("尾盘站上VWAP")
    else:
        score -= 12.0
        reasons.append("尾盘跌回VWAP下方")

    close_pos = safe_float(features.get("close_pos"), 0.0)
    if close_pos >= 0.82:
        score += 14.0 * trend_bias
        reasons.append("收在日内高位区")
    elif close_pos >= 0.66:
        score += 8.0
        reasons.append("收位中高")
    elif close_pos < 0.45:
        score -= 12.0
        reasons.append("收位偏低")
    return score


def _score_tail_momentum(
    features: dict[str, Any],
    *,
    trend_bias: float,
    pullback_bias: float,
    reasons: list[str],
) -> float:
    score = 0.0
    last30_ret_pct = safe_float(features.get("last30_ret_pct"), 0.0)
    if last30_ret_pct >= 1.0:
        score += 12.0 * trend_bias
        reasons.append("尾盘30分钟明显走强")
    elif last30_ret_pct >= 0.3:
        score += 6.0
        reasons.append("尾盘30分钟温和走强")
    elif last30_ret_pct <= -0.8:
        score -= 12.0
        reasons.append("尾盘30分钟转弱")

    last15_ret_pct = safe_float(features.get("last15_ret_pct"), 0.0)
    if last15_ret_pct <= -0.5:
        score -= 8.0
        reasons.append("最后15分钟回落偏大")
    elif last15_ret_pct >= 0.4:
        score += 4.0
        reasons.append("最后15分钟维持抬升")
    return score + _score_tail_volume_and_breakout(features, trend_bias, pullback_bias, reasons)


def _score_tail_volume_and_breakout(
    features: dict[str, Any],
    trend_bias: float,
    pullback_bias: float,
    reasons: list[str],
) -> float:
    score = 0.0
    tail30_share = safe_float(features.get("tail30_volume_share"), 0.0)
    if 0.14 <= tail30_share <= 0.45:
        score += 8.0
        reasons.append("尾段量能结构健康")
    elif tail30_share < 0.08:
        score -= 6.0
        reasons.append("尾段量能偏弱")
    elif tail30_share > 0.6:
        score -= 4.0
        reasons.append("尾段放量过猛，波动风险上升")
    if bool(features.get("reclaim_vwap")):
        score += 10.0 * pullback_bias
        reasons.append("出现回踩后再站上VWAP")
    if bool(features.get("strong_hold_vwap")):
        score += 10.0 * trend_bias
        reasons.append("全天强势守VWAP")
    if bool(features.get("breakout_tail")):
        score += 7.0 * trend_bias
        reasons.append("尾盘刷新前高/关键位")
    if safe_float(features.get("drop_from_high_pct"), 0.0) <= -2.2:
        score -= 10.0
        reasons.append("收盘距日高回撤过大")
    return score + _score_slope(features)


def _score_slope(features: dict[str, Any]) -> float:
    slope_10 = safe_float(features.get("slope_10_pct"), 0.0)
    if slope_10 >= 0.7:
        return 4.0
    if slope_10 <= -0.5:
        return -4.0
    return 0.0


def _score_tail_indicators(features: dict[str, Any], reasons: list[str]) -> float:
    score = 0.0
    vpc = safe_float(features.get("vol_price_corr"), 0.0)
    if vpc > 0.3:
        score += 8.0
        reasons.append("量价正相关（涨时放量跌时缩量）")
    elif vpc < -0.3:
        score -= 8.0
        reasons.append("量价背离（涨时缩量跌时放量）")
    score += _score_effort_and_money(features, reasons)
    score += _score_spring_quality(features, reasons)
    return score


def _score_effort_and_money(features: dict[str, Any], reasons: list[str]) -> float:
    score = 0.0
    evr = safe_float(features.get("effort_vs_result"), 0.0)
    if evr > 30:
        score += 6.0
        reasons.append("放量承接（高Effort低波动=吸筹）")
    elif evr < -30:
        score -= 6.0
        reasons.append("缩量大波动（虚假波动风险）")
    sms = safe_float(features.get("smart_money_score"), 0.0)
    if sms > 1.0:
        score += 5.0
        reasons.append("尾盘聪明钱流入（量价齐升）")
    elif sms < -1.0:
        score -= 5.0
        reasons.append("尾盘聪明钱撤退（放量下跌）")
    return score


def _score_spring_quality(features: dict[str, Any], reasons: list[str]) -> float:
    spring_q = features.get("spring_quality")
    if spring_q is None:
        return 0.0
    if spring_q >= 70:
        reasons.append(f"分钟线Spring验证通过（{spring_q:.0f}分，快速收回支撑）")
        return 10.0
    if spring_q >= 50:
        reasons.append(f"分钟线Spring部分确认（{spring_q:.0f}分）")
        return 5.0
    if spring_q <= 20:
        reasons.append(f"分钟线Spring失败（{spring_q:.0f}分，跌破未收回）")
        return -5.0
    return 0.0


def _decision_from_score(
    score: float,
    status: str,
    reasons: list[str],
    config: TailBuyStrategyConfig,
) -> tuple[float, str, list[str]]:
    score = max(0.0, min(100.0, score))
    if score >= 72:
        decision = DECISION_BUY
    elif score >= 52:
        decision = DECISION_WATCH
    else:
        decision = DECISION_SKIP
    if decision == DECISION_BUY and config.confirmed_only_buy and normalize_status(status) != "confirmed":
        decision = DECISION_WATCH
        reasons.append("未二次确认，尾盘只观察不买入")
    return score, decision, reasons


def _soft_buy_gate_reasons(features: dict[str, Any], signal_type: str, config: TailBuyStrategyConfig) -> list[str]:
    if not features or int(safe_float(features.get("bars"), 0.0)) <= 0:
        return []
    reasons: list[str] = []
    if _is_intraday_chase(features, config):
        reasons.append("日内涨幅过大，尾盘不追高")
    if _is_extended_from_support(features, signal_type, config):
        reasons.append("裸SOS/EVR已远离确认支撑，等待回踩")
    if _is_weak_naked_momentum(features, signal_type, config):
        reasons.append("裸SOS/EVR尾盘动能不足，只观察")
    if bool(features.get("daily_trap_pressure")):
        reasons.append(str(features.get("daily_trap_reason") or "日线诱多压力，尾盘不追"))
    return reasons


def _is_intraday_chase(features: dict[str, Any], config: TailBuyStrategyConfig) -> bool:
    day_ret = safe_float(features.get("day_ret_pct"), 0.0)
    high_ret = safe_float(features.get("intraday_high_ret_pct"), 0.0)
    return day_ret >= config.chase_day_ret_pct or high_ret >= config.chase_high_ret_pct


def _is_weak_naked_momentum(features: dict[str, Any], signal_type: str, config: TailBuyStrategyConfig) -> bool:
    st_lower = str(signal_type or "").strip().lower()
    if st_lower not in {"sos", "evr"}:
        return False
    if (
        bool(features.get("breakout_tail"))
        or bool(features.get("reclaim_vwap"))
        or bool(features.get("strong_hold_vwap"))
    ):
        return False
    return (
        safe_float(features.get("day_ret_pct"), 0.0) < config.weak_naked_day_ret_pct
        and safe_float(features.get("last30_ret_pct"), 0.0) < config.weak_naked_tail30_ret_pct
    )


def _is_extended_from_support(features: dict[str, Any], signal_type: str, config: TailBuyStrategyConfig) -> bool:
    st_lower = str(signal_type or "").strip().lower()
    if st_lower not in {"sos", "evr"}:
        return False
    support = safe_float(features.get("support_level"), 0.0)
    last_close = safe_float(features.get("last_close"), 0.0)
    if support <= 0.0 or last_close <= 0.0:
        return False
    return (last_close / support - 1.0) * 100.0 >= config.naked_support_extension_pct


def _apply_soft_buy_gates(
    score: float,
    decision: str,
    reasons: list[str],
    features: dict[str, Any],
    signal_type: str,
    config: TailBuyStrategyConfig,
) -> tuple[float, str, list[str]]:
    if decision != DECISION_BUY:
        return score, decision, reasons
    soft_reasons = _soft_buy_gate_reasons(features, signal_type, config)
    if not soft_reasons:
        return score, decision, reasons
    reasons.extend(x for x in soft_reasons if x not in reasons)
    return min(score, 68.0), DECISION_WATCH, reasons


def score_tail_features(
    features: dict[str, Any],
    *,
    signal_score: float = 0.0,
    signal_type: str = "",
    status: str = "pending",
    style: str = "hybrid",
    market_regime: str = "",
    entry_guard: bool = False,
    config: TailBuyStrategyConfig | None = None,
) -> tuple[float, str, list[str]]:
    """
    规则评分：输出 (分数, BUY/WATCH/SKIP, 理由列表)。
    style 支持 trend / pullback / hybrid；传空则按 signal_type 自动选择。
    """
    bars = int(safe_float(features.get("bars"), 0))
    if bars < 60:
        return 5.0, DECISION_SKIP, ["分时数据不足（<60根1m）"]

    hard_veto_reasons = tail_hard_veto_reasons(features)
    if entry_guard:
        hard_veto_reasons.extend(tail_entry_veto_reasons(features, signal_type, market_regime))
    if hard_veto_reasons:
        return 20.0, DECISION_SKIP, hard_veto_reasons

    reasons: list[str] = []
    st_lower, trend_bias, pullback_bias = _tail_style_bias(signal_type, style)
    score = _score_signal_context(signal_score, st_lower, status, reasons)
    score += _score_tail_position(features, trend_bias, reasons)
    score += _score_tail_momentum(features, trend_bias=trend_bias, pullback_bias=pullback_bias, reasons=reasons)
    score += _score_tail_indicators(features, reasons)
    policy = _strategy_config(config)
    score, decision, reasons = _decision_from_score(score, status, reasons, policy)
    return _apply_soft_buy_gates(score, decision, reasons, features, signal_type, policy)


def _build_daily_context(snap: dict[str, Any]) -> dict[str, Any] | None:
    support = safe_float(snap.get("snap_support"), 0.0)
    if support <= 0:
        return None
    return {"support_level": support, "snap_ma20": safe_float(snap.get("snap_ma20"), 0.0)}


def evaluate_rule_decision(
    candidate: TailBuyCandidate,
    df_1m: pd.DataFrame,
    *,
    style: str = "auto",
    config: TailBuyStrategyConfig | None = None,
    daily_history: pd.DataFrame | None = None,
) -> TailBuyCandidate:
    daily_context = _build_daily_context(candidate.snap) if candidate.snap else None
    policy = board_scaled_strategy_config(config, candidate.code)
    features = compute_tail_features(df_1m, daily_context, config=policy)
    features.update(_daily_trap_features(daily_history, policy))
    features.update(_candidate_context_features(candidate, _latest_intraday_session(ensure_intraday_df(df_1m))))
    features.update(
        _limit_up_features(
            candidate,
            daily_history,
            features.get("day_high", 0.0),
            features.get("day_low", 0.0),
            features.get("last_close", 0.0),
        )
    )
    if candidate.market_regime:
        features["market_regime"] = candidate.market_regime
    score, decision, reasons = score_tail_features(
        features,
        signal_score=candidate.signal_score,
        signal_type=candidate.signal_type,
        status=candidate.status,
        style=style,
        market_regime=candidate.market_regime,
        entry_guard=True,
        config=policy,
    )
    candidate.features = features
    candidate.rule_score = score
    candidate.rule_decision = decision
    candidate.rule_reasons = reasons
    candidate.final_decision = decision
    candidate.priority_score = score
    candidate.summary_5m = build_5m_summary(df_1m, max_bars=12)
    return _apply_unconfirmed_buy_gate(candidate, config)


def build_5m_summary(df_1m: pd.DataFrame, *, max_bars: int = 12) -> str:
    df = ensure_intraday_df(df_1m)
    df = _latest_intraday_session(df)
    if df.empty:
        return "NO_DATA"
    x = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]
    resampled = x.resample("5min", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    resampled = resampled.dropna(subset=["close"]).tail(max(1, int(max_bars)))
    rows: list[str] = []
    for idx, row in resampled.iterrows():
        hhmm = idx.strftime("%H:%M")
        rows.append(
            f"{hhmm} O{safe_float(row['open']):.2f} "
            f"H{safe_float(row['high']):.2f} "
            f"L{safe_float(row['low']):.2f} "
            f"C{safe_float(row['close']):.2f} "
            f"V{int(max(safe_float(row['volume']), 0.0))}"
        )
    return "\n".join(rows)


def build_llm_prompt(
    candidate: TailBuyCandidate,
    *,
    style: str = "hybrid",
    depth_info: dict | None = None,
) -> tuple[str, str]:
    f = candidate.features or {}
    style_desc = {
        "trend": "偏趋势（尾盘点火）",
        "pullback": "偏回踩再起",
        "hybrid": "混合型（尾盘走强 + 回踩再起）",
    }.get(str(style).lower(), "混合型（尾盘走强 + 回踩再起）")
    system_prompt = (
        "你是A股尾盘买入策略二判助手。"
        "你只能在 BUY/WATCH/SKIP 中选择一个结论，且必须返回 JSON。"
        "candidate_theme/candidate_phase/candidate_role 是程序确定字段，只能原样使用，不得重判或编造。"
        "主线核心只提高同等条件下的优先级，不能覆盖规则硬闸；confirmed 也不自动等于 BUY。"
        "若 day_low_breached_support=true、tail_blowoff_reversal=true、缺少支撑锚点或防守水温单EVR，必须选择 SKIP。"
        "若 daily_trap_pressure=true，不能选择 BUY，只能 WATCH 或 SKIP。"
        "禁止输出投资建议免责声明，禁止输出 markdown。"
    )
    user_prompt = (
        f"策略风格: {style_desc}\n"
        f"股票: {candidate.code} {candidate.name}\n"
        f"主线语义: theme={candidate.candidate_theme or '-'}, phase={candidate.candidate_phase or '-'}, "
        f"role={candidate.candidate_role or '-'}\n"
        f"主线评分: mainline={candidate.mainline_score}, theme={candidate.theme_score}, "
        f"role={candidate.stock_role_score}\n"
        f"信号: status={candidate.status}, type={candidate.signal_type}, lane={candidate.candidate_lane or '-'}, "
        f"entry={candidate.entry_type or '-'}, regime={candidate.market_regime or '-'}, "
        f"signal_score={candidate.signal_score:.2f}\n"
        f"规则一判: {candidate.rule_decision}, rule_score={candidate.rule_score:.1f}\n"
        "规则特征:\n"
        f"- close_pos={safe_float(f.get('close_pos')):.3f}\n"
        f"- dist_vwap_pct={safe_float(f.get('dist_vwap_pct')):.3f}\n"
        f"- last30_ret_pct={safe_float(f.get('last30_ret_pct')):.3f}\n"
        f"- last15_ret_pct={safe_float(f.get('last15_ret_pct')):.3f}\n"
        f"- tail30_volume_share={safe_float(f.get('tail30_volume_share')):.3f}\n"
        f"- support_level={safe_float(f.get('support_level')):.3f}\n"
        f"- day_low_vs_support_pct={safe_float(f.get('day_low_vs_support_pct')):.3f}\n"
        f"- day_low_breached_support={bool(f.get('day_low_breached_support'))}\n"
        f"- intraday_high_ret_pct={safe_float(f.get('intraday_high_ret_pct')):.3f}\n"
        f"- tail_blowoff_reversal={bool(f.get('tail_blowoff_reversal'))}\n"
        f"- daily_trap_pressure={bool(f.get('daily_trap_pressure'))}\n"
        f"- daily_close_vs_ma20_pct={safe_float(f.get('daily_close_vs_ma20_pct')):.3f}\n"
        f"- daily_upper_shadow_pct={safe_float(f.get('daily_upper_shadow_pct')):.3f}\n"
        f"- daily_volume_ratio={safe_float(f.get('daily_volume_ratio')):.3f}\n"
        f"- daily_trap_reason={str(f.get('daily_trap_reason') or '-')[:80]}\n"
        f"- strong_hold_vwap={bool(f.get('strong_hold_vwap'))}, hold_vwap_ratio={safe_float(f.get('hold_vwap_ratio')):.3f}\n"
        f"- reclaim_vwap={bool(f.get('reclaim_vwap'))}\n"
        f"- breakout_tail={bool(f.get('breakout_tail'))}\n"
        f"- drop_from_high_pct={safe_float(f.get('drop_from_high_pct')):.3f}\n"
        "最近5m摘要:\n"
        f"{candidate.summary_5m or 'NO_DATA'}\n"
    )
    if depth_info:
        user_prompt += (
            f"\n[五档] 委比: {depth_info.get('weibi', 0):.1f}% | "
            f"买盘总量: {depth_info.get('bid_total', 0)}手 | "
            f"卖盘总量: {depth_info.get('ask_total', 0)}手\n"
        )
    user_prompt += '\n请输出严格 JSON：{"decision":"BUY|WATCH|SKIP","reason":"<=80字","risk":"<=40字","confidence":0.0}'
    return system_prompt, user_prompt


def parse_llm_decision(raw_text: str) -> dict[str, Any] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
    except Exception:
        logger.debug("Direct JSON parse failed, trying regex extraction", exc_info=True)
    if parsed is None:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return None
    decision = str(parsed.get("decision", "") or "").strip().upper()
    if decision not in VALID_DECISIONS:
        return None
    reason = str(parsed.get("reason", "") or "").strip()
    risk = str(parsed.get("risk", "") or "").strip()
    confidence = parsed.get("confidence")
    try:
        conf_value = float(confidence)
    except Exception:
        conf_value = None
    if conf_value is not None:
        conf_value = max(0.0, min(1.0, conf_value))
    return {
        "decision": decision,
        "reason": reason,
        "risk": risk,
        "confidence": conf_value,
    }


def select_llm_overlay_candidates(
    candidates: list[TailBuyCandidate],
    *,
    max_llm_symbols: int,
    min_rule_score: float = 60.0,
    allowed_rule_decisions: tuple[str, ...] = (DECISION_BUY, DECISION_WATCH),
) -> list[TailBuyCandidate]:
    """
    在 Python 规则层先收紧 LLM 二判候选：
    - 仅保留无 fetch_error 的标的
    - 仅保留规则结论在 allowed_rule_decisions 内的标的
    - 仅保留 rule_score >= min_rule_score 的标的
    - 最后按 rule_score 倒序截断到 max_llm_symbols
    """
    limit = max(int(max_llm_symbols), 0)
    if limit <= 0 or not candidates:
        return []

    allowed = {str(x or "").strip().upper() for x in (allowed_rule_decisions or ()) if str(x or "").strip()}
    if not allowed:
        return []

    floor = max(safe_float(min_rule_score, 0.0), 0.0)
    selected = [
        item
        for item in candidates
        if not item.fetch_error
        and str(item.rule_decision or "").strip().upper() in allowed
        and safe_float(item.rule_score, 0.0) >= floor
    ]
    selected.sort(key=lambda x: (-x.rule_score, x.code))
    return selected[:limit]


def apply_policy_weight_adjustments(
    candidates: list[TailBuyCandidate],
    signal_weights: dict[str, float] | None,
    *,
    min_buy_score: float = 72.0,
    policy_meta: dict[str, Any] | None = None,
) -> list[TailBuyCandidate]:
    if not candidates or not signal_weights:
        return candidates
    floor = max(safe_float(min_buy_score, 72.0), 0.0)
    meta = policy_meta or {}
    execution_policy = str(meta.get("execution_policy") or "").strip()
    next_action = str(meta.get("next_action") or "").strip()
    has_formal_dynamic = "formal_dynamic_allowed" in meta or "formal_dynamic_block_reason" in meta or bool(next_action)
    for item in candidates:
        multiplier = _policy_multiplier_for_signal(item, signal_weights)
        if multiplier == 1.0:
            continue
        old_score = safe_float(item.rule_score, 0.0)
        new_score = _priority_score(old_score * multiplier)
        item.rule_score = new_score
        item.features.update(
            {
                "policy_weight_signal": str(item.signal_type or "").strip().lower(),
                "policy_weight_multiplier": multiplier,
                "policy_weight_old_score": old_score,
                "policy_weight_new_score": new_score,
                "policy_weight_source": str(meta.get("source") or ""),
                "policy_weight_report_date": str(meta.get("report_date") or ""),
                "policy_weight_horizon": str(meta.get("horizon") or ""),
                "policy_weight_age_days": meta.get("age_days"),
                "policy_weight_execution_policy": execution_policy,
                "policy_weight_execution_policy_label": (
                    policy_execution_mode_label(execution_policy) if execution_policy else ""
                ),
                "policy_weight_execution_scope": str(meta.get("execution_scope") or ""),
                "policy_weight_active_scope": str(meta.get("active_scope") or ""),
                "policy_weight_next_action": next_action,
                "policy_weight_next_action_label": policy_next_action_label(next_action) if next_action else "",
                "policy_weight_formal_dynamic_allowed": meta.get("formal_dynamic_allowed"),
                "policy_weight_formal_dynamic_label": policy_formal_dynamic_label(meta) if has_formal_dynamic else "",
                "policy_weight_tail_buy_weights_active": meta.get("tail_buy_weights_active"),
                "policy_weight_funnel_shadow_weights_active": meta.get("funnel_shadow_weights_active"),
                "policy_weight_funnel_formal_weights_active": meta.get("funnel_formal_weights_active"),
                "policy_weight_formal_dynamic_block_reason": str(meta.get("formal_dynamic_block_reason") or ""),
            }
        )
        if item.priority_score > 0:
            item.priority_score = _priority_score(item.priority_score * multiplier)
        item.rule_reasons.append(
            f"归因治理调权({item.signal_type}) x{multiplier:.2f}: {old_score:.1f}->{new_score:.1f}"
        )
        if multiplier < 1.0 and item.rule_decision == DECISION_BUY and new_score < floor:
            item.rule_decision = DECISION_WATCH
            if item.final_decision == DECISION_BUY:
                item.final_decision = DECISION_WATCH
            item.priority_score = _priority_score(new_score + 3.0)
            item.rule_reasons.append("调权后低于买入线，尾盘只观察")
    return candidates


def _policy_multiplier_for_signal(item: TailBuyCandidate, signal_weights: dict[str, float]) -> float:
    signal = str(item.signal_type or item.signal_key or "").strip().lower()
    if not signal:
        return 1.0
    return resolve_signal_weight_multiplier(
        signal_weights,
        signal,
        regime=item.market_regime,
        lane=item.candidate_lane,
        entry_type=item.entry_type,
    )


def merge_rule_and_llm(
    candidates: list[TailBuyCandidate],
    llm_result_by_code: dict[str, dict[str, Any]] | None = None,
    *,
    config: TailBuyStrategyConfig | None = None,
) -> list[TailBuyCandidate]:
    llm_result_by_code = llm_result_by_code or {}
    policy = _strategy_config(config)
    decision_bonus = {
        DECISION_BUY: 12.0,
        DECISION_WATCH: 3.0,
        DECISION_SKIP: -20.0,
    }
    out: list[TailBuyCandidate] = []
    for item in candidates or []:
        code = normalize_cn_code(item.code)
        veto_reasons = tail_candidate_veto_reasons(item)
        if veto_reasons:
            item.llm_decision = None
            item.final_decision = DECISION_SKIP
            item.priority_score = min(item.rule_score, 20.0) - 20.0
            item.rule_reasons = veto_reasons
            out.append(item)
            continue
        llm = llm_result_by_code.get(code) or {}
        llm_decision = str(llm.get("decision", "") or "").strip().upper()
        if llm_decision in VALID_DECISIONS:
            item.llm_decision = llm_decision
            item.llm_model_used = str(llm.get("model_used", "") or "").strip()
            reason = str(llm.get("reason", "") or "").strip()
            risk = str(llm.get("risk", "") or "").strip()
            if risk:
                reason = f"{reason}；风险:{risk}" if reason else f"风险:{risk}"
            item.llm_reason = reason
            conf = llm.get("confidence")
            conf_val: float | None
            try:
                conf_val = float(conf) if conf is not None else None
                if conf_val is not None and math.isnan(conf_val):
                    conf_val = None
            except Exception:
                conf_val = None
            item.llm_confidence = conf_val
            item.final_decision = llm_decision
            item.priority_score = _priority_score(item.rule_score + decision_bonus.get(llm_decision, 0.0))
        else:
            item.final_decision = item.rule_decision
            item.priority_score = _priority_score(item.rule_score + decision_bonus.get(item.rule_decision, 0.0))
        item = _apply_final_buy_soft_gates(item, policy)
        item = _apply_unconfirmed_buy_gate(item, policy)
        out.append(item)
    out.sort(key=lambda x: (-x.priority_score, -x.rule_score, x.code))
    return out


def _apply_final_buy_soft_gates(candidate: TailBuyCandidate, config: TailBuyStrategyConfig) -> TailBuyCandidate:
    if candidate.final_decision != DECISION_BUY:
        return candidate
    reasons = _soft_buy_gate_reasons(candidate.features or {}, candidate.signal_type, config)
    if not reasons:
        return candidate
    candidate.final_decision = DECISION_WATCH
    candidate.priority_score = _priority_score(candidate.rule_score + 3.0)
    candidate.rule_reasons.extend(x for x in reasons if x not in candidate.rule_reasons)
    suffix = "；".join(reasons)
    candidate.llm_reason = f"{candidate.llm_reason}；{suffix}" if candidate.llm_reason else suffix
    return candidate
