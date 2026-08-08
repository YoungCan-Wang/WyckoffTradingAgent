"""Shared candidate selection guardrails for live funnel and backtests."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from core.signal_confirmation import compute_support_level, score_springboard_abc

logger = logging.getLogger(__name__)

STRUCTURAL_L4_TRIGGERS = {"spring", "lps", "compression", "compress", "trend_pullback", "volatile_pullback"}
NAKED_RIGHT_SIDE_TRIGGERS = {"sos", "evr"}
TREND_CANDIDATE_TRIGGERS = {
    "main_force_entry",
    "trend_breakout",
    "trend_lane_pullback",
    "sector_strength",
    "wyckoff_structure",
}
DEFENSIVE_REGIMES = {
    "RISK_OFF",
    "BEAR_REBOUND",
    "PANIC_REPAIR",
    "PANIC_REPAIR_CONFIRMED",
    "CRASH",
    "BLACK_SWAN",
}
WEAK_PULLBACK_REGIMES = DEFENSIVE_REGIMES | {"RISK_ON"}


@dataclass(frozen=True)
class CandidatePolicyConfig:
    loss_guard_enabled: bool = True
    alpha_block_risk_on_early_breakout: bool = True
    alpha_risk_on_early_breakout_min_score: float = 70.0
    mix_trendpb_min_score: float = 12.0
    pure_lps_observe_only: bool = True
    pure_lps_min_score: float = 6.0
    pure_trendpb_observe_only: bool = True
    pure_trendpb_min_score: float = 10.0
    pure_sos_min_score: float = 6.0
    pure_sos_observe_only: bool = True
    pure_spring_observe_only: bool = True
    pure_evr_observe_only: bool = True
    pure_evr_min_score_default: float = 3.0
    pure_evr_min_score_hot: float = 5.0
    weak_confirmation_min_abc: int = 2
    pure_sos_min_abc: int = 3
    risk_on_pre5_ret: float = 35.0
    risk_on_range_pos: float = 85.0
    risk_on_vol_ratio: float = 1.8
    defensive_high_range_pos: float = 78.0
    defensive_high_20d_ret: float = 18.0
    # NEUTRAL 放宽高位中继误杀，主升段常见 20 日涨幅 >35%。
    neutral_high_range_pos: float = 95.0
    neutral_high_20d_ret: float = 45.0
    max_structure_stop_pct: float = 12.0


DEFAULT_CANDIDATE_POLICY_CONFIG = CandidatePolicyConfig()


def trigger_sets_by_code(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for trigger, pairs in (triggers or {}).items():
        key = str(trigger).strip().lower()
        if not key:
            continue
        for code, _score in pairs or []:
            code_s = str(code).strip()
            if code_s:
                out.setdefault(code_s, set()).add(key)
    return out


def is_tradeable_l4_trigger_combo(trigger_keys: Iterable[str]) -> bool:
    keys = _normalize_keys(trigger_keys)
    if not keys:
        return False
    if keys & STRUCTURAL_L4_TRIGGERS:
        return True
    return not keys <= NAKED_RIGHT_SIDE_TRIGGERS


def rerank_selected_codes(codes: list[str], score_map: dict[str, float]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for code in codes:
        code_s = str(code).strip()
        if code_s and code_s not in seen:
            deduped.append(code_s)
            seen.add(code_s)
    return sorted(deduped, key=lambda c: (-candidate_score_value(score_map.get(c)), c))


def cap_quality_candidates(
    codes: list[str],
    score_map: dict[str, float],
    sector_map: dict[str, str] | None,
    *,
    total_cap: int,
    max_per_sector: int,
    rank_by_score: bool = True,
) -> tuple[list[str], list[str], list[str]]:
    """Rank qualified candidates, then apply one global and sector cap."""
    ranked = rerank_selected_codes(codes, score_map) if rank_by_score else list(dict.fromkeys(codes))
    if total_cap <= 0:
        return [], ranked, []
    selected: list[str] = []
    cap_dropped: list[str] = []
    sector_dropped: list[str] = []
    sector_counts: dict[str, int] = {}
    for code in ranked:
        sector = str((sector_map or {}).get(code) or "").strip()
        if sector and max_per_sector > 0 and sector_counts.get(sector, 0) >= max_per_sector:
            sector_dropped.append(code)
            continue
        if len(selected) >= total_cap:
            cap_dropped.append(code)
            continue
        selected.append(code)
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
    return selected, cap_dropped, sector_dropped


def candidate_score_value(raw: object) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _normalize_keys(trigger_keys: Iterable[str]) -> set[str]:
    return {str(k).strip().lower() for k in trigger_keys if str(k).strip()}


def _channel_tags(raw: str) -> set[str]:
    return {x.strip() for x in str(raw or "").split("+") if x.strip()}


def _is_pure_momentum_channel(channel: str) -> bool:
    tags = _channel_tags(channel)
    if not tags or "点火破局" in tags:
        return False
    return bool(tags <= {"主升通道", "趋势延续", "加速突破"})


def _recent_overheat(df: pd.DataFrame | None, config: CandidatePolicyConfig) -> bool:
    if df is None or df.empty or len(df) < 21:
        return False
    work = _numeric_ohlcv(df)
    if work is None:
        return False
    high20, low20 = float(work["high"].max()), float(work["low"].min())
    close = float(work.iloc[-1]["close"])
    pre5_ret = (close / float(work.iloc[-6]["close"]) - 1.0) * 100.0
    range_pos = (close - low20) / (high20 - low20) * 100.0 if high20 > low20 else 0.0
    vol20 = float(work["volume"].tail(20).mean())
    vol_ratio = float(work["volume"].tail(5).mean()) / vol20 if vol20 > 0 else 0.0
    return (
        pre5_ret >= config.risk_on_pre5_ret
        and range_pos >= config.risk_on_range_pos
        and vol_ratio >= config.risk_on_vol_ratio
    )


def _recent_position_stats(df: pd.DataFrame | None) -> dict[str, float] | None:
    if df is None or df.empty or len(df) < 21:
        return None
    work = _numeric_ohlcv(df)
    if work is None:
        return None
    high20, low20 = float(work["high"].max()), float(work["low"].min())
    close = float(work.iloc[-1]["close"])
    base = float(work.iloc[0]["close"])
    range_pos = (close - low20) / (high20 - low20) * 100.0 if high20 > low20 else 0.0
    ret20 = (close / base - 1.0) * 100.0 if base > 0 else 0.0
    return {"range_pos": range_pos, "ret20": ret20}


def _late_stage_high_reason(
    regime_norm: str,
    keys: set[str],
    df: pd.DataFrame | None,
    config: CandidatePolicyConfig,
) -> str:
    if not keys or "spring" in keys or "volatile_pullback" in keys:
        return ""
    stats = _recent_position_stats(df)
    if not stats:
        return ""
    defensive = regime_norm in DEFENSIVE_REGIMES
    range_cut = config.defensive_high_range_pos if defensive else config.neutral_high_range_pos
    ret_cut = config.defensive_high_20d_ret if defensive else config.neutral_high_20d_ret
    if stats["range_pos"] >= range_cut and stats["ret20"] >= ret_cut:
        return f"{regime_norm}20日高位追涨"
    return ""


def _numeric_ohlcv(df: pd.DataFrame) -> pd.DataFrame | None:
    work = df.copy()
    for col in ("close", "high", "low", "volume"):
        if col not in work.columns:
            return None
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.tail(21).dropna(subset=["close", "high", "low", "volume"])
    return work if len(work) >= 21 and float(work.iloc[-1]["close"]) > 0 else None


def loss_guard_reason(
    code: str,
    regime: str,
    trigger_keys: Iterable[str],
    trigger_score: float,
    channel: str,
    df_map: dict[str, pd.DataFrame],
    *,
    config: CandidatePolicyConfig | None = None,
    mainline_codes: set[str] | None = None,
) -> str:
    policy = config or DEFAULT_CANDIDATE_POLICY_CONFIG
    if not policy.loss_guard_enabled:
        return ""
    keys = _normalize_keys(trigger_keys)
    regime_norm = str(regime or "NEUTRAL").strip().upper() or "NEUTRAL"
    if keys == {"early_breakout"} and regime_norm == "RISK_ON":
        if policy.alpha_block_risk_on_early_breakout and trigger_score < policy.alpha_risk_on_early_breakout_min_score:
            return "RISK_ON低分早期突破"
    high_reason = _late_stage_high_reason(regime_norm, keys, df_map.get(code), policy)
    if high_reason:
        return high_reason
    weak_reason = _weak_confirmation_reason(keys, df_map.get(code), policy)
    if weak_reason:
        return weak_reason
    stop_reason = _structure_stop_reason(keys, df_map.get(code), policy)
    if stop_reason:
        return stop_reason
    is_mainline = bool(mainline_codes and code in mainline_codes)
    if keys == {"spring"} and policy.pure_spring_observe_only and not is_mainline:
        # 跨周期回测（2026-08-08，run 31237549718：bear_2022 / bull_2020 /
        # recent_6m 三周期、696 条 spring 成交）：spring 均收 -3.93%、胜率 22.4%，
        # 对照非 spring -0.24%／33.4%，Welch t=-6.70（合并）；分周期 bear_2022
        # t=-4.82、bull_2020 t=-6.57 均显著。三个周期方向一致为负，是唯一在全部
        # 周期都一致为负的信号，与市场环境无关。
        return "单Spring仅观察"
    if "lps" in keys and not (keys & {"sos", "evr", "spring"}):
        return _pure_lps_reason(regime_norm, trigger_score, policy, is_mainline)
    if keys == {"trend_pullback"}:
        return _pure_trend_pullback_reason(regime_norm, trigger_score, policy, is_mainline)
    if "trend_pullback" in keys and regime_norm in WEAK_PULLBACK_REGIMES:
        if trigger_score < policy.mix_trendpb_min_score:
            return f"{regime_norm}弱趋势回踩"
    if keys and keys <= NAKED_RIGHT_SIDE_TRIGGERS:
        reason = _naked_right_side_reason(
            regime_norm, keys, trigger_score, channel, df_map.get(code), policy, is_mainline
        )
        if reason:
            return reason
    return ""


def _structure_stop_reason(
    keys: set[str],
    df: pd.DataFrame | None,
    config: CandidatePolicyConfig,
) -> str:
    if not keys or df is None or df.empty or config.max_structure_stop_pct <= 0:
        return ""
    if "close" not in df.columns:
        return ""
    close_series = pd.to_numeric(df["close"], errors="coerce").dropna()
    close = candidate_score_value(close_series.iloc[-1]) if not close_series.empty else 0.0
    if close <= 0:
        return ""
    levels = []
    for signal_type in sorted(keys):
        try:
            support = candidate_score_value(compute_support_level(df, signal_type))
        except Exception:
            continue
        if 0 < support < close:
            levels.append(support)
    if not levels:
        return ""
    risk_pct = (close - max(levels)) / close * 100.0
    return "结构止损超出风险上限" if risk_pct > config.max_structure_stop_pct else ""


def _weak_confirmation_reason(keys: set[str], df: pd.DataFrame | None, config: CandidatePolicyConfig) -> str:
    if not keys:
        return ""
    if not (keys <= NAKED_RIGHT_SIDE_TRIGGERS or keys & TREND_CANDIDATE_TRIGGERS):
        return ""
    if df is None:
        # 调用方本就没有接入K线数据（如部分回测/诊断的轻量路径），无法评判，交给其余分支处理。
        return ""
    # df 存在但历史长度不足/字段缺失时按最保守值(0)处理，绝不能因为"算不出来"而放行——
    # 历史上 legacy_layered 策略版本未计算 springboard，就是靠这个空子把大量弱确认
    # SOS/EVR 放进了正式候选，是信号胜率被拖累的主因之一。
    met_count = _springboard_met_count(df, keys)
    if met_count >= config.weak_confirmation_min_abc:
        return ""
    if keys <= NAKED_RIGHT_SIDE_TRIGGERS:
        return "右侧信号ABC不足"
    return "趋势候选ABC不足"


def _springboard_met_count(df: pd.DataFrame, keys: set[str]) -> int:
    if df.empty or not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
        return 0
    if len(df) < 60:
        return 0
    counts = []
    for signal_type in sorted(keys):
        try:
            counts.append(int(score_springboard_abc(df, signal_type).get("met_count") or 0))
        except Exception:
            logger.warning("score_springboard_abc failed for signal_type=%s", signal_type, exc_info=True)
            continue
    return max(counts) if counts else 0


def _pure_lps_reason(
    regime_norm: str, trigger_score: float, config: CandidatePolicyConfig, is_mainline: bool = False
) -> str:
    if config.pure_lps_observe_only and not is_mainline:
        return "单LPS仅观察"
    if trigger_score < config.pure_lps_min_score:
        return "低分LPS"
    if regime_norm in DEFENSIVE_REGIMES | {"RISK_ON"}:
        return f"{regime_norm}禁用LPS"
    return ""


def _pure_trend_pullback_reason(
    regime_norm: str, trigger_score: float, config: CandidatePolicyConfig, is_mainline: bool = False
) -> str:
    if config.pure_trendpb_observe_only and not is_mainline:
        return "单TrendPB仅观察"
    if trigger_score < config.pure_trendpb_min_score:
        return "低分TrendPB"
    if regime_norm in WEAK_PULLBACK_REGIMES:
        return f"{regime_norm}禁用TrendPB"
    return ""


def _naked_right_side_reason(
    regime_norm: str,
    keys: set[str],
    trigger_score: float,
    channel: str,
    df: pd.DataFrame | None,
    config: CandidatePolicyConfig,
    is_mainline: bool = False,
) -> str:
    if regime_norm == "BEAR_REBOUND" and _is_pure_momentum_channel(channel):
        return f"{regime_norm}纯趋势追涨"
    if keys == {"evr"} and config.pure_evr_observe_only and not is_mainline:
        return "单EVR仅观察"
    if keys == {"sos"} and config.pure_sos_observe_only and not is_mainline:
        # 标准回放（2026-08-07，snapshot 2025-11-03..2026-07-20，162 交易日，两次独立
        # ledger 去重后 493 条纯 SOS）：10 日中位 -3.20%、胜率 40.0%，对照非纯 SOS
        # 中位 -1.27%、胜率 44.3%。均值受少数极端日主导（剔最差 5 日即转正），但中位
        # 与胜率两个抗尾部口径在 5/10 日、两次 ledger 上方向一致。
        # ABC 门槛松紧无法改善：met=2 与 met=3 的差异在所有周期 |t|<0.5。
        return "单SOS仅观察"
    if "sos" in keys and trigger_score < config.pure_sos_min_score:
        return "低分SOS"
    if keys == {"sos"} and df is not None and _springboard_met_count(df, keys) < config.pure_sos_min_abc:
        # pure_sos_observe_only=False 时才会走到这里。met=3 未被证明优于 met=2
        # （标准回放 |t|<0.5），保留 3 只是保守默认值，不代表已验证。
        return "纯SOS确认强度不足"
    evr_min_score = (
        config.pure_evr_min_score_hot
        if regime_norm in {"RISK_ON", "BEAR_REBOUND"}
        else config.pure_evr_min_score_default
    )
    if keys == {"evr"} and trigger_score < evr_min_score:
        return "低分EVR"
    if regime_norm in {"RISK_ON", "BEAR_REBOUND"} and _recent_overheat(df, config):
        return f"{regime_norm}短期过热"
    return ""


def apply_loss_guard(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    *,
    regime: str,
    code_to_trigger_keys: dict[str, Iterable[str]],
    code_to_total_score: dict[str, float],
    channel_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    config: CandidatePolicyConfig | None = None,
    mainline_codes: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    kept: list[str] = []
    dropped: dict[str, int] = {}
    for code in selected_for_ai:
        reason = loss_guard_reason(
            code,
            regime,
            code_to_trigger_keys.get(code, []),
            candidate_score_value(code_to_total_score.get(code)),
            str(channel_map.get(code, "") or ""),
            df_map,
            config=config,
            mainline_codes=mainline_codes,
        )
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(code)
    kept_set = set(kept)
    return kept, [c for c in trend_selected if c in kept_set], [c for c in accum_selected if c in kept_set], dropped
