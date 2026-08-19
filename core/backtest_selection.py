"""Candidate selection policy used by historical backtests."""

from __future__ import annotations

import pandas as pd

from core.ai_candidate_allocation import AiCandidateAllocationConfig, allocate_ai_candidates
from core.candidate_policy import (
    CandidatePolicyConfig,
    apply_loss_guard,
    candidate_score_value,
    cap_quality_candidates,
    is_tradeable_l4_trigger_combo,
    loss_guard_reason,
    trigger_sets_by_code,
)
from core.candidate_ranker import rank_l3_candidates
from core.candidate_tracks import (
    best_candidate_entry_map,
    candidate_entry_key,
    candidate_entry_sort_key,
    candidate_entry_track,
)
from core.sector_rotation import analyze_sector_rotation
from core.strategy_policy_governor import resolve_signal_weight_multiplier
from core.wyckoff_engine import FunnelConfig, FunnelResult

TRADEABLE_L4_SELECTION_MODES = {"tradeable_l4"}
STRICT_L4_SELECTION_MODES = {"quality_l4", "strict_l4"}
FORMAL_L4_SELECTION_MODES = {"all_formal_l4", "all_l4", "full_formal_l4", "full_l4"}
LEGACY_SELECTION_MODES = {"legacy_full_hits", "legacy_hits", "all_hits", "classic"}
LOSS_GUARD_ENTRY_KEYS = {
    "compression",
    "early_breakout",
    "evr",
    "lps",
    "spring",
    "sos",
    "trend_breakout",
    "trend_lane_pullback",
    "trend_pullback",
    "volatile_pullback",
}
SIGNAL_WEIGHT_ALIASES = {
    "trend_breakout": "sos",
    "trend_lane_pullback": "trend_pullback",
}


#: 触发分归一化后的刻度上限。与 candidate_entries 的 _lane_score / _candidate_entry
#: 同为 0~100，两条路径的分数才能在 _expand_tradeable_quality_pool 里安全 max()，
#: pure_*_min_score 这组阈值也才对所有触发类型一视同仁。
TRIGGER_SCORE_SCALE = 100.0


def combine_trigger_scores(
    triggers: dict[str, list[tuple[str, float]]],
    signal_weight_map: dict[str, float] | None = None,
    *,
    regime: str = "",
) -> dict[str, tuple[float, str]]:
    """合并同一标的的多触发器分数，归一化到 0~100 后取最大值。

    各 detector 返回的是**不同物理量**，直接比较是错的（实测 1983 笔）：

        _detect_spring → 收回幅度%   1.00 ~ 100.00（中位 8.05）
        _detect_sos    → 量比        3.05 ~  20.49（中位 4.69）
        _detect_evr    → 量比        1.86 ~   8.93（中位 2.79）
        _detect_lps    → 缩量比      0.58
        _detect_compression → 缩量比 0.61 ~ 0.72
        trend_pullback → 缩量比      0.25 ~ 0.60

    后果有两层：

    1. **排序错**：spring 天然高一到两个量级，但它 10 日均收 -3.93% 是六类最差，
       分数几乎最低的 compression 是 +1.44%（类型级 score-ret Spearman = -0.257）。
    2. **阈值错**：`pure_*_min_score` 默认 6.0 这一刀，只有 spring 的中位分(8.05)
       能过，trend_pullback(0.43) / compression(0.67) / lps(0.58) 被整类砍掉——
       而后两者恰是收益最好的。此前观察到"低分拦截拦掉的标的反而更好"
       （Welch t=-4.51）根因就在这里：阈值不是方向反了，是刻度不对。

    改为先在各触发类型内部做分位归一化，再乘 TRIGGER_SCORE_SCALE 对齐
    candidate_entries 的 0~100 刻度，最后乘治理权重取 max。分位是无量纲量，
    消除单位差异；类型内部的相对排序完全保留。

    该归一化只消除类型间量纲差异，不改变类型内排序。
    """
    reason_map: dict[str, list[str]] = {}
    score_map: dict[str, float] = {}
    for key, pairs in triggers.items():
        normalized = normalized_trigger_scores(pairs)
        for code, _score in pairs:
            code_s = str(code).strip()
            if not code_s:
                continue
            if code_s not in reason_map:
                reason_map[code_s] = []
            reason_map[code_s].append(key)
            score_map[code_s] = max(
                candidate_score_value(score_map.get(code_s)),
                normalized.get(code_s, 0.0) * signal_weight_multiplier(key, signal_weight_map, regime=regime),
            )
    return {code: (score_map.get(code, 0.0), "、".join(reasons)) for code, reasons in reason_map.items()}


def normalized_trigger_scores(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """把同一触发类型内的原始分数转成 0~TRIGGER_SCORE_SCALE，使其跨类型可比。

    两个必须保留的行为（均为实测踩到的回归）：

    1. **非法或非正分数仍归 0**，不参与分位计算。否则 rank(pct) 会让它拿到中位
       分位（实测 0.5 → 50 分），等于给"算不出分数"的候选一个中等评价。
    2. **只有一个有效候选时给满分**——该类型内它就是最高分，不该因样本少被压低。
       实测单候选情形仅占 2%，不影响整体分布。

    同分并列取平均分位，避免顺序抖动。
    """
    valid: dict[str, float] = {}
    zeros: list[str] = []
    for code, score in pairs or []:
        code_s = str(code).strip()
        if not code_s:
            continue
        value = candidate_score_value(score)
        if value > 0:
            valid[code_s] = value
        else:
            zeros.append(code_s)
    out: dict[str, float] = dict.fromkeys(zeros, 0.0)
    if not valid:
        return out
    if len(valid) == 1:
        out[next(iter(valid))] = TRIGGER_SCORE_SCALE
        return out
    ranks = pd.Series(valid).rank(pct=True, method="average")
    out.update({str(code): float(pct) * TRIGGER_SCORE_SCALE for code, pct in ranks.items()})
    return out


def dedup_order(codes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = str(raw).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def track_map_for_hits(codes: list[str], triggers: dict[str, list[tuple[str, float]]]) -> dict[str, str]:
    sos_hit_set = {str(code).strip() for code, _ in triggers.get("sos", [])}
    evr_hit_set = {str(code).strip() for code, _ in triggers.get("evr", [])}
    spring_hit_set = {str(code).strip() for code, _ in triggers.get("spring", [])}
    lps_hit_set = {str(code).strip() for code, _ in triggers.get("lps", [])}
    return {code: _track_for_code(code, sos_hit_set, evr_hit_set, spring_hit_set, lps_hit_set) for code in codes}


def _track_for_code(
    code: str, sos_hit_set: set[str], evr_hit_set: set[str], spring_hit_set: set[str], lps_hit_set: set[str]
) -> str:
    if code in sos_hit_set or code in evr_hit_set:
        return "Trend"
    if code in spring_hit_set or code in lps_hit_set:
        return "Accum"
    return "Trend"


def quota_ai_inputs(
    *,
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    regime: str,
    ai_allocation: AiCandidateAllocationConfig | None = None,
    signal_weight_map: dict[str, float] | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, float]]:
    sector_rotation = analyze_sector_rotation(
        day_df_map,
        sector_map,
        universe_symbols=list(day_df_map.keys()),
        focus_sectors=result.top_sectors,
    )
    l3_ranked_symbols, _ = rank_l3_candidates(
        l3_symbols=result.layer3_symbols,
        df_map=day_df_map,
        sector_map=sector_map,
        triggers=result.triggers,
        top_sectors=result.top_sectors,
        l2_channel_map=result.channel_map,
        sector_rotation_map=(sector_rotation or {}).get("state_map", {}) or {},
    )
    trend_sel, accum_sel, score_map = allocate_ai_candidates(
        result,
        l3_ranked_symbols or result.layer3_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=2,
        signal_weight_map=signal_weight_map,
        allocation_config=ai_allocation,
    )
    return dedup_order(trend_sel + accum_sel), trend_sel, accum_sel, score_map


def select_ai_input_codes(
    *,
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    regime: str,
    selection_mode: str,
    full_formal_l4_max: int = 25,
    candidate_policy: CandidatePolicyConfig | None = None,
    ai_allocation: AiCandidateAllocationConfig | None = None,
    signal_weight_map: dict[str, float] | None = None,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    merged_trigger_map = combine_trigger_scores(result.triggers, signal_weight_map, regime=regime)
    hit_score_map = {code: candidate_score_value(value[0]) for code, value in merged_trigger_map.items()}
    sorted_hit_codes = sorted(merged_trigger_map.keys(), key=lambda code: -hit_score_map.get(code, 0.0))
    l4_selection = _select_l4_mode_codes(
        result=result,
        sorted_hit_codes=sorted_hit_codes,
        hit_score_map=hit_score_map,
        day_df_map=day_df_map,
        regime=regime,
        selection_mode=selection_mode,
        full_formal_l4_max=full_formal_l4_max,
        candidate_policy=candidate_policy,
        signal_weight_map=signal_weight_map,
        sector_map=sector_map,
        ai_allocation=ai_allocation,
    )
    if l4_selection is not None:
        return l4_selection
    return _select_quota_mode(
        result,
        day_df_map,
        sector_map,
        regime,
        selection_mode,
        hit_score_map,
        candidate_policy,
        ai_allocation,
        signal_weight_map,
    )


def _select_l4_mode_codes(
    *,
    result: FunnelResult,
    sorted_hit_codes: list[str],
    hit_score_map: dict[str, float],
    day_df_map: dict[str, pd.DataFrame],
    regime: str,
    selection_mode: str,
    full_formal_l4_max: int,
    candidate_policy: CandidatePolicyConfig | None,
    signal_weight_map: dict[str, float] | None,
    sector_map: dict[str, str],
    ai_allocation: AiCandidateAllocationConfig | None,
) -> tuple[list[str], dict[str, float], dict[str, str]] | None:
    if selection_mode in TRADEABLE_L4_SELECTION_MODES and result.candidate_entries:
        selected, score_map, track_map = _select_candidate_entries(
            result, day_df_map, regime, candidate_policy, signal_weight_map
        )
        selected, score_map, track_map = _expand_tradeable_quality_pool(
            selected,
            score_map,
            track_map,
            sorted_hit_codes,
            hit_score_map,
            result,
        )
        selected = _apply_tradeable_loss_guard(
            selected, track_map, result, day_df_map, regime, score_map, candidate_policy
        )
        score_map = {code: score_map.get(code, 0.0) for code in selected}
        track_map = {code: track_map[code] for code in selected}
        return _cap_tradeable_selection(selected, score_map, track_map, sector_map, ai_allocation)
    if selection_mode in STRICT_L4_SELECTION_MODES or selection_mode in TRADEABLE_L4_SELECTION_MODES:
        trigger_sets = trigger_sets_by_code(result.triggers)
        selected_codes = [
            code for code in sorted_hit_codes if is_tradeable_l4_trigger_combo(trigger_sets.get(code, set()))
        ]
    elif selection_mode in FORMAL_L4_SELECTION_MODES or selection_mode in LEGACY_SELECTION_MODES:
        selected_codes = sorted_hit_codes if full_formal_l4_max <= 0 else sorted_hit_codes[:full_formal_l4_max]
    else:
        return None
    score_map = {code: hit_score_map.get(code, 0.0) for code in selected_codes}
    track_map = track_map_for_hits(selected_codes, result.triggers)
    if selection_mode in TRADEABLE_L4_SELECTION_MODES:
        selected_codes = _apply_tradeable_loss_guard(
            selected_codes, track_map, result, day_df_map, regime, hit_score_map, candidate_policy
        )
        score_map = {code: score_map.get(code, 0.0) for code in selected_codes}
        track_map = {code: track_map[code] for code in selected_codes}
        return _cap_tradeable_selection(selected_codes, score_map, track_map, sector_map, ai_allocation)
    return selected_codes, score_map, track_map


def _expand_tradeable_quality_pool(
    selected_codes: list[str],
    score_map: dict[str, float],
    track_map: dict[str, str],
    sorted_hit_codes: list[str],
    hit_score_map: dict[str, float],
    result: FunnelResult,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    trigger_sets = trigger_sets_by_code(result.triggers)
    formal_codes = [code for code in sorted_hit_codes if is_tradeable_l4_trigger_combo(trigger_sets.get(code, set()))]
    merged = dedup_order(selected_codes + formal_codes)
    formal_tracks = track_map_for_hits(formal_codes, result.triggers)
    merged_scores = dict(score_map)
    merged_tracks = dict(track_map)
    for code in formal_codes:
        merged_scores[code] = max(merged_scores.get(code, 0.0), hit_score_map.get(code, 0.0))
        merged_tracks.setdefault(code, formal_tracks[code])
    return merged, merged_scores, merged_tracks


def _cap_tradeable_selection(
    selected_codes: list[str],
    score_map: dict[str, float],
    track_map: dict[str, str],
    sector_map: dict[str, str],
    ai_allocation: AiCandidateAllocationConfig | None,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    config = ai_allocation or AiCandidateAllocationConfig()
    selected, _cap_dropped, _sector_dropped = cap_quality_candidates(
        selected_codes,
        score_map,
        sector_map,
        total_cap=config.total_cap,
        max_per_sector=config.max_per_sector,
        rank_by_score=True,
    )
    return (
        selected,
        {code: score_map.get(code, 0.0) for code in selected},
        {code: track_map[code] for code in selected},
    )


def _apply_tradeable_loss_guard(
    selected_codes: list[str],
    track_map: dict[str, str],
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    regime: str,
    hit_score_map: dict[str, float],
    candidate_policy: CandidatePolicyConfig | None,
) -> list[str]:
    trend_sel = [code for code in selected_codes if track_map.get(code) == "Trend"]
    accum_sel = [code for code in selected_codes if track_map.get(code) == "Accum"]
    kept, _trend_kept, _accum_kept, _ = apply_loss_guard(
        selected_codes,
        trend_sel,
        accum_sel,
        regime=regime,
        code_to_trigger_keys=trigger_sets_by_code(result.triggers),
        code_to_total_score=hit_score_map,
        channel_map=result.channel_map,
        df_map=day_df_map,
        config=candidate_policy,
    )
    return kept


def _select_candidate_entries(
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    regime: str,
    candidate_policy: CandidatePolicyConfig | None,
    signal_weight_map: dict[str, float] | None,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    best_entries = best_candidate_entry_map(
        [
            item
            for item in result.candidate_entries or []
            if not candidate_entry_loss_guard(
                item,
                result=result,
                day_df_map=day_df_map,
                regime=regime,
                candidate_policy=candidate_policy,
                signal_weight_map=signal_weight_map,
            )
        ],
    )
    entries = _rank_tradeable_entries(list(best_entries.values()), signal_weight_map, regime)
    selected_codes = dedup_order([str(item.get("code", "")).strip() for item in entries])
    score_map, track_map = _candidate_entry_maps(entries, signal_weight_map, regime=regime)
    return selected_codes, score_map, track_map


def _rank_tradeable_entries(
    entries: list[dict[str, object]], signal_weight_map: dict[str, float] | None, regime: str
) -> list[dict[str, object]]:
    ranked = sorted(entries, key=lambda item: weighted_candidate_entry_sort_key(item, signal_weight_map, regime=regime))
    launchpads = [item for item in ranked if candidate_entry_key(item) == "launchpad"]
    confirmed = [item for item in ranked if candidate_entry_key(item) != "launchpad"]
    if str(regime or "").strip().upper() == "CAUTION":
        return confirmed
    if not launchpads:
        return confirmed
    if not confirmed:
        return launchpads[:1]
    return [confirmed[0], launchpads[0], *confirmed[1:]]


def _candidate_entry_maps(
    entries: list[dict[str, object]], signal_weight_map: dict[str, float] | None, *, regime: str
) -> tuple[dict[str, float], dict[str, str]]:
    score_map: dict[str, float] = {}
    track_map: dict[str, str] = {}
    for item in entries:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        score = weighted_candidate_entry_score(item, signal_weight_map, regime=regime)
        if code not in score_map or score > score_map[code]:
            score_map[code] = score
            track_map[code] = candidate_entry_track(item)
    return score_map, track_map


def candidate_entry_loss_guard(
    item: dict[str, object],
    *,
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    regime: str,
    candidate_policy: CandidatePolicyConfig | None = None,
    signal_weight_map: dict[str, float] | None = None,
) -> str:
    code = str(item.get("code", "")).strip()
    if not code:
        return "empty_code"
    exit_signal = str((result.exit_signals.get(code, {}) or {}).get("signal", "")).strip()
    if exit_signal in {"stop_loss", "distribution_warning", "upthrust_warning"}:
        return f"exit_signal:{exit_signal}"
    entry_type = candidate_entry_key(item, LOSS_GUARD_ENTRY_KEYS)
    return loss_guard_reason(
        code,
        regime,
        [entry_type],
        weighted_candidate_entry_score(item, signal_weight_map, regime=regime),
        str(result.channel_map.get(code, "") or ""),
        day_df_map,
        config=candidate_policy,
    )


def _select_quota_mode(
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    regime: str,
    selection_mode: str,
    hit_score_map: dict[str, float],
    candidate_policy: CandidatePolicyConfig | None,
    ai_allocation: AiCandidateAllocationConfig | None,
    signal_weight_map: dict[str, float] | None,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    selected_codes, trend_sel, accum_sel, score_map = quota_ai_inputs(
        result=result,
        day_df_map=day_df_map,
        sector_map=sector_map,
        regime=regime,
        ai_allocation=ai_allocation,
        signal_weight_map=signal_weight_map,
    )
    if selection_mode in TRADEABLE_L4_SELECTION_MODES:
        selected_codes, trend_sel, accum_sel, _ = apply_loss_guard(
            selected_codes,
            trend_sel,
            accum_sel,
            regime=regime,
            code_to_trigger_keys=trigger_sets_by_code(result.triggers),
            code_to_total_score=hit_score_map,
            channel_map=result.channel_map,
            df_map=day_df_map,
            config=candidate_policy,
        )
    selected_codes = _apply_min_score(selected_codes, score_map)
    track_map = dict.fromkeys(trend_sel, "Trend")
    track_map.update(dict.fromkeys(accum_sel, "Accum"))
    return selected_codes, score_map, track_map


def _apply_min_score(selected_codes: list[str], score_map: dict[str, float]) -> list[str]:
    min_score = float(getattr(FunnelConfig, "min_funnel_score", 0.15) or 0)
    if min_score > 0 and score_map:
        return [code for code in selected_codes if candidate_score_value(score_map.get(code)) >= min_score]
    return selected_codes


def weighted_candidate_entry_sort_key(
    item: dict[str, object],
    signal_weight_map: dict[str, float] | None,
    *,
    regime: str = "",
) -> tuple[int, float, str]:
    priority, _score, code = candidate_entry_sort_key(item)
    return priority, -weighted_candidate_entry_score(item, signal_weight_map, regime=regime), code


def weighted_candidate_entry_score(
    item: dict[str, object],
    signal_weight_map: dict[str, float] | None,
    *,
    regime: str = "",
) -> float:
    return candidate_score_value(item.get("score")) * _candidate_entry_multiplier(
        item, signal_weight_map, regime=regime
    )


def _candidate_entry_multiplier(
    item: dict[str, object],
    signal_weight_map: dict[str, float] | None,
    *,
    regime: str,
) -> float:
    key = candidate_entry_key(item, fields=("signal_key", "entry_type", "lane"))
    return signal_weight_multiplier(
        key,
        signal_weight_map,
        regime=regime,
        lane=item.get("candidate_lane") or item.get("lane"),
        entry_type=item.get("entry_type"),
    )


def signal_weight_multiplier(
    signal_type: object,
    signal_weight_map: dict[str, float] | None,
    *,
    regime: str = "",
    lane: object = "",
    entry_type: object = "",
) -> float:
    for alias in _signal_aliases(signal_type):
        value = resolve_signal_weight_multiplier(
            signal_weight_map,
            alias,
            regime=regime,
            lane=lane,
            entry_type=entry_type,
        )
        if value != 1.0:
            return value
    return 1.0


def _signal_aliases(signal_type: object) -> tuple[object, object]:
    return signal_type, SIGNAL_WEIGHT_ALIASES.get(str(signal_type or "").strip().lower(), "")
