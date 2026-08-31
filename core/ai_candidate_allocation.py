"""AI candidate allocation policy for Wyckoff funnel outputs."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.candidate_tracks import candidate_entry_sort_key, candidate_entry_track
from core.strategy_policy_governor import resolve_signal_weight_multiplier

TREND_CHANNEL_TAGS = {"主升通道", "趋势延续", "点火破局", "加速突破"}
ACCUM_CHANNEL_TAGS = {"潜伏通道", "吸筹通道", "地量蓄势", "暗中护盘"}
HIT_KEYS = ("sos", "spring", "lps", "evr", "compression", "trend_pullback")
BLOCKED_EXIT_SIGNALS = {"stop_loss", "distribution_warning", "upthrust_warning"}
DEFAULT_AI_QUOTA_BY_FAMILY: dict[str, tuple[int, int]] = {
    # RISK_ON 保留研究/shadow 候选。联动实测该档超额 +6.07pct（3/3 为正），
    # 买入闸门已于 #280 放开。
    "RISK_ON": (5, 1),
    # BEAR_REBOUND 由 (0,0) 改为 (5,1)，与其买入闸门放开对齐。
    # 此前不一致：#280 依联动实测（超额 +4.08pct、4/4 为正）把它移出禁买名单，
    # 但配额仍是 0——候选进了池子也拿不到复核席位。2026-08-17 那天池内 4 只候选
    # （大北农/南方精工/宿迁联盛/新相微）次日全涨 9.7%~10.1%，而 AI 正式推荐 0 只。
    "BEAR_REBOUND": (5, 1),
    # PANIC_REPAIR 单独成族并保持 0：其超额为 -4.09pct，与 BEAR_REBOUND 方向相反，
    # 此前两者共用一个配额家族会让证据互相污染。
    "PANIC_REPAIR": (0, 0),
    "RISK_OFF": (0, 0),
    # NEUTRAL: 已于 #280 并入禁买；配额保留供 shadow 对照，正式推荐与下单由闸门拦住。
    # 注意 #280 引的「超额 -4.35pct」测的是 all 宽候选池，不是闸门管辖的正式推荐；
    # 2026-08-31 按 formal_l4 口径重测，绝对收益 H=5/10/20 为 -2.43/-6.19/-5.72%，
    # 禁买结论不变（详见 core/market_trade_mode._explicitly_allowed_regimes 文档串）。
    "NEUTRAL": (5, 1),
}


@dataclass(frozen=True)
class AllocationPolicy:
    total_cap: int
    trend_quota: int
    accum_quota: int
    max_trend_l3_fill: int
    max_accum_l3_fill: int


@dataclass(frozen=True)
class AiCandidateAllocationConfig:
    total_cap: int = 8
    max_per_sector: int = 2
    max_trend_l3_fill: int = 0
    max_accum_l3_fill: int = 0
    quota_by_family: Mapping[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_AI_QUOTA_BY_FAMILY))

    def requested_quota(self, quota_family: str) -> tuple[int, int]:
        trend, accum = self.quota_by_family.get(quota_family, DEFAULT_AI_QUOTA_BY_FAMILY["NEUTRAL"])
        return max(int(trend), 0), max(int(accum), 0)


@dataclass
class CandidatePools:
    trend: list[tuple[str, float, bool]] = field(default_factory=list)
    accum: list[tuple[str, float, bool]] = field(default_factory=list)


@dataclass
class SelectionState:
    selected_seen: set[str] = field(default_factory=set)
    trend_selected: list[str] = field(default_factory=list)
    accum_selected: list[str] = field(default_factory=list)
    sector_counts: dict[str, int] = field(default_factory=dict)
    trend_l3_fill_used: int = 0
    accum_l3_fill_used: int = 0


def fit_ai_candidate_quotas(total_cap: int, trend_quota: int, accum_quota: int) -> tuple[int, int]:
    """Fit requested Trend/Accum quotas into a global total cap."""
    total_cap_local = max(int(total_cap), 0)
    trend_quota_local = max(int(trend_quota), 0)
    accum_quota_local = max(int(accum_quota), 0)
    if total_cap_local <= 0:
        return (0, 0)

    requested_total = trend_quota_local + accum_quota_local
    if requested_total <= total_cap_local:
        return (trend_quota_local, accum_quota_local)
    if requested_total <= 0:
        return (0, 0)

    trend_eff = min(max(int(round(total_cap_local * (trend_quota_local / requested_total))), 0), trend_quota_local)
    accum_eff = min(accum_quota_local, max(total_cap_local - trend_eff, 0))
    return _fill_quota_remainder(total_cap_local, trend_quota_local, accum_quota_local, trend_eff, accum_eff)


def resolve_ai_candidate_policy(
    regime: str,
    override_total_cap: int = -1,
    *,
    config: AiCandidateAllocationConfig | None = None,
) -> dict[str, int | str]:
    """
    Central source of truth for AI allocation defaults.

    CRASH / PANIC_REPAIR / BEAR_REBOUND / BLACK_SWAN all share the defensive quota family
    instead of silently falling back to NEUTRAL.
    """
    allocation_config = config or AiCandidateAllocationConfig()
    total_cap = allocation_config.total_cap if override_total_cap < 0 else max(int(override_total_cap), 0)
    regime_norm = str(regime or "").strip().upper()
    quota_family = _quota_family(regime_norm)
    requested_trend, requested_accum = allocation_config.requested_quota(quota_family)
    trend_quota, accum_quota = fit_ai_candidate_quotas(total_cap, requested_trend, requested_accum)
    return {
        "regime": regime_norm or "NEUTRAL",
        "quota_family": quota_family,
        "total_cap": total_cap,
        "max_per_sector": allocation_config.max_per_sector,
        "requested_trend_quota": requested_trend,
        "requested_accum_quota": requested_accum,
        "trend_quota": trend_quota,
        "accum_quota": accum_quota,
        "max_trend_l3_fill": allocation_config.max_trend_l3_fill,
        "max_accum_l3_fill": allocation_config.max_accum_l3_fill,
    }


def allocate_ai_candidates(
    result: Any,
    l3_ranked_symbols: list[str],
    regime: str,
    override_total_cap: int = -1,
    sector_map: dict[str, str] | None = None,
    max_per_sector: int = 2,
    policy_override: dict[str, int | str] | None = None,
    signal_weight_map: dict[str, float] | None = None,
    allocation_config: AiCandidateAllocationConfig | None = None,
) -> tuple[list[str], list[str], dict[str, float]]:
    """Return (trend_selected, accum_selected, score_map) for the AI review stage."""
    raw_policy = policy_override or resolve_ai_candidate_policy(
        regime,
        override_total_cap=override_total_cap,
        config=allocation_config,
    )
    policy = _allocation_policy(raw_policy)
    hit_sets = _hit_sets(_result_map(result, "triggers"))
    pools = _candidate_pools(result, l3_ranked_symbols, hit_sets, signal_weight_map or {}, regime)
    score_map = _score_map(pools)
    if policy.total_cap <= 0:
        return [], [], score_map
    selected = _select_by_quota(pools, policy, sector_map, max_per_sector)
    return selected.trend_selected, selected.accum_selected, score_map


def _candidate_pools(
    result: Any,
    l3_ranked_symbols: list[str],
    hit_sets: dict[str, set[str]],
    signal_weight_map: dict[str, float],
    regime: str,
) -> CandidatePools:
    score_candidate = _build_candidate_priority_scorer(result, hit_sets, signal_weight_map, regime)
    sos_hit_set = hit_sets["sos"]
    pools = _entry_candidate_pools(result, sos_hit_set)
    _add_markup_candidates(pools, result, hit_sets, score_candidate)
    _add_trigger_trend_candidates(pools, result, score_candidate)
    _add_l3_trend_candidates(pools, result, l3_ranked_symbols, hit_sets, score_candidate)
    _add_accum_trigger_candidates(pools, result, score_candidate, sos_hit_set)
    _add_l3_accum_candidates(pools, result, l3_ranked_symbols, score_candidate, sos_hit_set)
    pools.trend.sort(key=lambda item: (-item[1], item[2]))
    pools.accum.sort(key=lambda item: (-item[1], item[2]))
    return pools


def _entry_candidate_pools(result: Any, sos_hit_set: set[str]) -> CandidatePools:
    pools = CandidatePools()
    for item in sorted(_result_list(result, "candidate_entries"), key=candidate_entry_sort_key):
        code = str(item.get("code", "")).strip()
        if not code or _is_blocked_exit(result, code, sos_hit_set):
            continue
        target = pools.accum if candidate_entry_track(item) == "Accum" else pools.trend
        target.append((code, float(item.get("score", 0.0) or 0.0), False))
    return pools


def _add_markup_candidates(
    pools: CandidatePools,
    result: Any,
    hit_sets: dict[str, set[str]],
    score_candidate: Callable[[str, bool], float],
) -> None:
    codes = [c for c in _result_list(result, "markup_symbols") if _is_trend_track(result, c) or c in hit_sets["sos"]]
    for code in _dedup_order(codes):
        pools.trend.append((code, score_candidate(code, True), False))


def _add_trigger_trend_candidates(
    pools: CandidatePools,
    result: Any,
    score_candidate: Callable[[str, bool], float],
) -> None:
    triggers = _result_map(result, "triggers")
    codes = (
        _trigger_codes_by_score(triggers, "sos")
        + _trigger_codes_by_score(triggers, "trend_pullback")
        + _trigger_codes_by_score(triggers, "evr")
    )
    _append_scored_codes(pools.trend, _dedup_order(codes), lambda code: score_candidate(code, True))


def _add_l3_trend_candidates(
    pools: CandidatePools,
    result: Any,
    l3_ranked_symbols: list[str],
    hit_sets: dict[str, set[str]],
    score_candidate: Callable[[str, bool], float],
) -> None:
    triggers = _result_map(result, "triggers")
    sorted_codes = _dedup_order(
        [c for c, _score in sorted(_all_trigger_rows(triggers), key=lambda item: -_score(item))]
    )
    existing = _candidate_codes(pools.trend)
    for code in sorted_codes + l3_ranked_symbols:
        if code in existing or not _is_trend_track(result, code) or _is_blocked_exit(result, code, hit_sets["sos"]):
            continue
        if code in _result_set(result, "markup_symbols") or code in hit_sets["sos"]:
            pools.trend.append((code, score_candidate(code, True), False))
            existing.add(code)


def _add_accum_trigger_candidates(
    pools: CandidatePools,
    result: Any,
    score_candidate: Callable[[str, bool], float],
    sos_hit_set: set[str],
) -> None:
    triggers = _result_map(result, "triggers")
    spring_lps = sorted(triggers.get("spring", []) + triggers.get("lps", []), key=lambda item: -_score(item))
    codes = [str(code).strip() for code, _score_value in spring_lps if str(code).strip()]
    codes += _trigger_codes_by_score(triggers, "compression", reverse=False)
    clean = [code for code in _dedup_order(codes) if not _is_blocked_exit(result, code, sos_hit_set)]
    _append_scored_codes(pools.accum, clean, lambda code: score_candidate(code, False))


def _add_l3_accum_candidates(
    pools: CandidatePools,
    result: Any,
    l3_ranked_symbols: list[str],
    score_candidate: Callable[[str, bool], float],
    sos_hit_set: set[str],
) -> None:
    existing = _candidate_codes(pools.accum)
    for code in _dedup_order(l3_ranked_symbols):
        if code in existing or not _is_accum_track(result, code) or _is_blocked_exit(result, code, sos_hit_set):
            continue
        if _stage_name(result, code) == "Accum_C":
            pools.accum.append((code, score_candidate(code, False), False))
            existing.add(code)


def _select_by_quota(
    pools: CandidatePools,
    policy: AllocationPolicy,
    sector_map: dict[str, str] | None,
    max_per_sector: int,
) -> SelectionState:
    state = SelectionState()
    trend_candidates, accum_candidates = _track_candidate_codes(pools, policy)
    trend_fill_map = _track_fill_map(pools.trend)
    accum_fill_map = _track_fill_map(pools.accum)
    trend_idx = accum_idx = 0

    while _can_select_more(state, policy, trend_idx, accum_idx, trend_candidates, accum_candidates):
        progressed = False
        if len(state.trend_selected) < policy.trend_quota and trend_idx < len(trend_candidates):
            progressed, trend_idx = _advance_track(
                "Trend",
                trend_candidates,
                trend_idx,
                state,
                policy,
                trend_fill_map,
                accum_fill_map,
                sector_map,
                max_per_sector,
            )
        if len(state.selected_seen) >= policy.total_cap:
            break
        if len(state.accum_selected) < policy.accum_quota and accum_idx < len(accum_candidates):
            added, accum_idx = _advance_track(
                "Accum",
                accum_candidates,
                accum_idx,
                state,
                policy,
                trend_fill_map,
                accum_fill_map,
                sector_map,
                max_per_sector,
            )
            progressed = added or progressed
        if not progressed:
            break
    _backfill_remaining_slots(
        state,
        policy,
        trend_candidates,
        accum_candidates,
        trend_idx,
        accum_idx,
        trend_fill_map,
        accum_fill_map,
        sector_map,
        max_per_sector,
    )
    return state


def _track_candidate_codes(pools: CandidatePools, policy: AllocationPolicy) -> tuple[list[str], list[str]]:
    owner = _best_track_by_code(pools, policy)
    trend_candidates = [
        code for code in _dedup_order([c for c, _s, _fill in pools.trend]) if owner.get(code) == "Trend"
    ]
    accum_candidates = [
        code for code in _dedup_order([c for c, _s, _fill in pools.accum]) if owner.get(code) == "Accum"
    ]
    return trend_candidates, accum_candidates


def _best_track_by_code(pools: CandidatePools, policy: AllocationPolicy) -> dict[str, str]:
    best: dict[str, tuple[tuple[float, int, int], str]] = {}
    if policy.trend_quota > 0:
        _record_track_owner(best, "Trend", pools.trend)
    if policy.accum_quota > 0:
        _record_track_owner(best, "Accum", pools.accum)
    return {code: track for code, (_key, track) in best.items()}


def _record_track_owner(
    best: dict[str, tuple[tuple[float, int, int], str]],
    track: str,
    candidates: list[tuple[str, float, bool]],
) -> None:
    track_priority = 1 if track == "Trend" else 0
    for order, (code, score, _is_fill) in enumerate(candidates):
        key = (float(score or 0.0), track_priority, -order)
        if code and (code not in best or key > best[code][0]):
            best[code] = (key, track)


def _track_fill_map(candidates: list[tuple[str, float, bool]]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for code, _score, is_fill in candidates:
        if code not in out or not is_fill:
            out[code] = bool(is_fill)
    return out


def _backfill_remaining_slots(
    state: SelectionState,
    policy: AllocationPolicy,
    trend_candidates: list[str],
    accum_candidates: list[str],
    trend_idx: int,
    accum_idx: int,
    trend_fill_map: dict[str, bool],
    accum_fill_map: dict[str, bool],
    sector_map: dict[str, str] | None,
    max_per_sector: int,
) -> None:
    while len(state.selected_seen) < policy.total_cap:
        progressed = False
        if policy.trend_quota > 0 and trend_idx < len(trend_candidates):
            added, trend_idx = _advance_track(
                "Trend",
                trend_candidates,
                trend_idx,
                state,
                policy,
                trend_fill_map,
                accum_fill_map,
                sector_map,
                max_per_sector,
                ignore_track_quota=True,
            )
            progressed = added or progressed
        if len(state.selected_seen) >= policy.total_cap:
            break
        if policy.accum_quota > 0 and accum_idx < len(accum_candidates):
            added, accum_idx = _advance_track(
                "Accum",
                accum_candidates,
                accum_idx,
                state,
                policy,
                trend_fill_map,
                accum_fill_map,
                sector_map,
                max_per_sector,
                ignore_track_quota=True,
            )
            progressed = added or progressed
        if not progressed:
            break


def _advance_track(
    track_name: str,
    candidates: list[str],
    idx: int,
    state: SelectionState,
    policy: AllocationPolicy,
    trend_fill_map: dict[str, bool],
    accum_fill_map: dict[str, bool],
    sector_map: dict[str, str] | None,
    max_per_sector: int,
    *,
    ignore_track_quota: bool = False,
) -> tuple[bool, int]:
    while idx < len(candidates):
        code = candidates[idx]
        idx += 1
        if code in state.selected_seen:
            continue
        if _try_add_candidate(
            code,
            track_name,
            state,
            policy,
            trend_fill_map,
            accum_fill_map,
            sector_map,
            max_per_sector,
            ignore_track_quota=ignore_track_quota,
        ):
            return True, idx
    return False, idx


def _try_add_candidate(
    code: str,
    track_name: str,
    state: SelectionState,
    policy: AllocationPolicy,
    trend_fill_map: dict[str, bool],
    accum_fill_map: dict[str, bool],
    sector_map: dict[str, str] | None,
    max_per_sector: int,
    *,
    ignore_track_quota: bool = False,
) -> bool:
    if len(state.selected_seen) >= policy.total_cap:
        return False
    sector = _blocked_sector(code, state.sector_counts, sector_map, max_per_sector)
    if sector is None:
        return False
    if not _append_track_selection(
        code,
        track_name,
        state,
        policy,
        trend_fill_map,
        accum_fill_map,
        ignore_track_quota=ignore_track_quota,
    ):
        return False
    if sector:
        state.sector_counts[sector] = state.sector_counts.get(sector, 0) + 1
    state.selected_seen.add(code)
    return True


def _append_track_selection(
    code: str,
    track_name: str,
    state: SelectionState,
    policy: AllocationPolicy,
    trend_fill_map: dict[str, bool],
    accum_fill_map: dict[str, bool],
    *,
    ignore_track_quota: bool = False,
) -> bool:
    if track_name == "Trend":
        if trend_fill_map.get(code, False) and state.trend_l3_fill_used >= policy.max_trend_l3_fill:
            return False
        if not ignore_track_quota and len(state.trend_selected) >= policy.trend_quota:
            return False
        state.trend_selected.append(code)
        state.trend_l3_fill_used += int(trend_fill_map.get(code, False))
        return True
    if accum_fill_map.get(code, False) and state.accum_l3_fill_used >= policy.max_accum_l3_fill:
        return False
    if not ignore_track_quota and len(state.accum_selected) >= policy.accum_quota:
        return False
    state.accum_selected.append(code)
    state.accum_l3_fill_used += int(accum_fill_map.get(code, False))
    return True


def _build_candidate_priority_scorer(
    result: Any,
    hit_sets: dict[str, set[str]],
    signal_weight_map: dict[str, float],
    regime: str,
) -> Callable[[str, bool], float]:
    markup_set = _result_set(result, "markup_symbols")
    spring_hits = hit_sets.get("spring", set())
    lps_hits = hit_sets.get("lps", set())
    evr_hits = hit_sets.get("evr", set())
    compression_hits = hit_sets.get("compression", set())
    trend_pb_hits = hit_sets.get("trend_pullback", set())
    sos_hits = hit_sets.get("sos", set())
    other_hits = spring_hits | lps_hits | evr_hits | compression_hits | trend_pb_hits
    l3_score_map = _result_map(result, "layer3_score_map")

    def score(code: str, is_trend_side: bool) -> float:
        value = _stage_score(_stage_name(result, code), is_trend_side)
        value += 100.0 if code in markup_set else 0.0
        value += _trigger_score(
            code,
            sos_hits,
            other_hits,
            spring_hits,
            lps_hits,
            evr_hits,
            compression_hits,
            trend_pb_hits,
            signal_weight_map,
            regime,
        )
        value += _track_alignment_bonus(code, is_trend_side, hit_sets, signal_weight_map, regime)
        value += _layer3_rank_bonus(code, l3_score_map)
        return value + _exit_penalty(result, code)

    return score


def _layer3_rank_bonus(code: str, l3_score_map: dict[str, float]) -> float:
    try:
        score = float(l3_score_map.get(code, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return min(max(score, 0.0), 1.2) * 8.0


def _trigger_score(
    code: str,
    sos_hits: set[str],
    other_hits: set[str],
    spring_hits: set[str],
    lps_hits: set[str],
    evr_hits: set[str],
    compression_hits: set[str],
    trend_pb_hits: set[str],
    weights: dict[str, float],
    regime: str,
) -> float:
    value = (50.0 if code in other_hits else 15.0) * _signal_weight(weights, "sos", regime) if code in sos_hits else 0.0
    # spring 权重 2026-08-08 由 45.0 下调至 12.0（并列末位）。
    #
    # 依据：跨周期回测（run 31237549718 / 31248741109，bear_2022 / bull_2020 /
    # recent_6m，696 条 spring 成交）显示 spring 是六类触发器里 10 日收益最差的
    # 一类，且是唯一在三个周期方向全部为负的：
    #     spring -3.93% / 胜率 22.4%   对照非 spring -0.24% / 33.4%
    #     合并 Welch t=-6.70；分周期 bear_2022 t=-4.82、bull_2020 t=-6.57 均显著
    # 而原权重 45.0 让它排在六类第二高，形成"最差信号得次高分"的错配。
    #
    # 只下调 spring 一项、不重排整表：compression(+1.44%) 与 lps(-3.19%) 的样本
    # 分别只有 20 / 10 笔，按它们重排等于拟合噪声。取 12.0 是对齐当前末位
    # (evr) 的量级，不是拟合出的精确值。spring 仍可通过共振（sos+spring 等）
    # 进入候选，只是不再单靠自身排到前面。
    #
    # 注意本函数在回测 tradeable_l4 口径下不被触达（_select_candidate_entries
    # 提前 return），故该调整暂时只影响实盘。回测侧的根因是 detector 原始分数
    # 单位混用问题由回测侧的类内分位归一化独立处理。
    value += 12.0 * _signal_weight(weights, "spring", regime) if code in spring_hits else 0.0
    value += 30.0 * _signal_weight(weights, "lps", regime) if code in lps_hits else 0.0
    value += 12.0 * _signal_weight(weights, "evr", regime) if code in evr_hits else 0.0
    value += 22.0 * _signal_weight(weights, "compression", regime) if code in compression_hits else 0.0
    value += 34.0 * _signal_weight(weights, "trend_pullback", regime) if code in trend_pb_hits else 0.0
    return value


def _track_alignment_bonus(
    code: str,
    is_trend_side: bool,
    hit_sets: dict[str, set[str]],
    weights: dict[str, float],
    regime: str,
) -> float:
    """赛道对齐奖励：只对候选实际命中的信号类型取权重最大值。

    此前会无条件对整条赛道的全部信号类型取全局权重 max()，导致一个仅由
    已 DECAYED 信号（如 evr）命中的候选，因为同赛道里另一个健康信号（如
    sos）的全局权重高，而拿到未降权的满额奖励——精细降权被稀释成了粗粒度。
    """
    trend_keys = ("sos", "evr", "trend_pullback")
    accum_keys = ("spring", "lps", "compression")
    keys = trend_keys if is_trend_side else accum_keys
    hit_weights = [_signal_weight(weights, key, regime) for key in keys if code in hit_sets[key]]
    return 10.0 * max(hit_weights) if hit_weights else 0.0


def _stage_score(stage_name: str, is_trend_side: bool) -> float:
    if stage_name == "Accum_C":
        return 5.0 if is_trend_side else 15.0
    if stage_name == "Accum_B":
        return 3.0 if is_trend_side else 8.0
    if stage_name == "Accum_A":
        return 0.0 if is_trend_side else 3.0
    return 0.0


def _exit_penalty(result: Any, code: str) -> float:
    signal = _exit_signal(result, code)
    if signal == "stop_loss":
        return -100.0
    if signal in {"distribution_warning", "upthrust_warning"}:
        return -20.0
    return 0.0


def _can_select_more(
    state: SelectionState,
    policy: AllocationPolicy,
    trend_idx: int,
    accum_idx: int,
    trend_candidates: list[str],
    accum_candidates: list[str],
) -> bool:
    return (
        len(state.selected_seen) < policy.total_cap
        and (len(state.trend_selected) < policy.trend_quota or len(state.accum_selected) < policy.accum_quota)
        and (trend_idx < len(trend_candidates) or accum_idx < len(accum_candidates))
    )


def _allocation_policy(raw: dict[str, int | str]) -> AllocationPolicy:
    return AllocationPolicy(
        total_cap=int(raw["total_cap"]),
        trend_quota=int(raw["trend_quota"]),
        accum_quota=int(raw["accum_quota"]),
        max_trend_l3_fill=int(raw["max_trend_l3_fill"]),
        max_accum_l3_fill=int(raw["max_accum_l3_fill"]),
    )


def _quota_family(regime_norm: str) -> str:
    if regime_norm == "RISK_ON":
        return "RISK_ON"
    # BEAR_REBOUND 与 PANIC_REPAIR 拆开：实测超额 +4.08pct vs -4.09pct，方向相反，
    # 共用一个配额家族会让两者的证据互相污染。
    if regime_norm == "BEAR_REBOUND":
        return "BEAR_REBOUND"
    if regime_norm == "PANIC_REPAIR":
        return "PANIC_REPAIR"
    if regime_norm in {"RISK_OFF", "CRASH", "BLACK_SWAN"}:
        return "RISK_OFF"
    return "NEUTRAL"


def _fill_quota_remainder(
    total_cap: int,
    trend_quota: int,
    accum_quota: int,
    trend_eff: int,
    accum_eff: int,
) -> tuple[int, int]:
    remaining = max(total_cap - trend_eff - accum_eff, 0)
    if remaining > 0 and trend_eff < trend_quota:
        take = min(remaining, trend_quota - trend_eff)
        trend_eff += take
        remaining -= take
    if remaining > 0 and accum_eff < accum_quota:
        accum_eff += min(remaining, accum_quota - accum_eff)
    return trend_eff, accum_eff


def _trigger_codes_by_score(
    triggers: dict[str, list[tuple[str, float]]],
    signal_type: str,
    *,
    reverse: bool = True,
) -> list[str]:
    return [
        str(code).strip()
        for code, _score_value in sorted(triggers.get(signal_type, []) or [], key=_score, reverse=reverse)
        if str(code).strip()
    ]


def _append_scored_codes(
    candidates: list[tuple[str, float, bool]],
    codes: list[str],
    score_fn: Callable[[str], float],
) -> None:
    existing = _candidate_codes(candidates)
    for code in codes:
        code_s = str(code).strip()
        if code_s and code_s not in existing:
            candidates.append((code_s, score_fn(code_s), False))
            existing.add(code_s)


def _score_map(pools: CandidatePools) -> dict[str, float]:
    out: dict[str, float] = {}
    for code, score, _is_fill in pools.trend + pools.accum:
        out[code] = max(out.get(code, -9999.0), score)
    return out


def _hit_sets(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, set[str]]:
    return {
        key: {str(code).strip() for code, _score_value in triggers.get(key, []) if str(code).strip()}
        for key in HIT_KEYS
    }


def _is_trend_track(result: Any, code: str) -> bool:
    return bool(_channel_tags(result, code) & TREND_CHANNEL_TAGS)


def _is_accum_track(result: Any, code: str) -> bool:
    return bool(_channel_tags(result, code) & ACCUM_CHANNEL_TAGS)


def _channel_tags(result: Any, code: str) -> set[str]:
    raw = str(_result_map(result, "channel_map").get(code, "")).strip()
    return {x.strip() for x in raw.split("+") if x.strip()} if raw else set()


def _is_blocked_exit(result: Any, code: str, sos_hit_set: set[str]) -> bool:
    signal = _exit_signal(result, code)
    return signal in BLOCKED_EXIT_SIGNALS and not (signal == "stop_loss" and code in sos_hit_set)


def _exit_signal(result: Any, code: str) -> str:
    exit_row = _result_map(result, "exit_signals").get(code, {}) or {}
    return str(exit_row.get("signal", "")).strip()


def _stage_name(result: Any, code: str) -> str:
    return str(_result_map(result, "stage_map").get(code, "")).strip()


def _blocked_sector(
    code: str,
    sector_counts: dict[str, int],
    sector_map: dict[str, str] | None,
    max_per_sector: int,
) -> str | None:
    if not sector_map or max_per_sector <= 0:
        return ""
    sector = sector_map.get(code, "").strip()
    return None if sector and sector_counts.get(sector, 0) >= max_per_sector else sector


def _dedup_order(codes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = str(raw).strip()
        if code and code not in seen:
            out.append(code)
            seen.add(code)
    return out


def _candidate_codes(candidates: list[tuple[str, float, bool]]) -> set[str]:
    return {code for code, _score_value, _is_fill in candidates}


def _all_trigger_rows(triggers: dict[str, list[tuple[str, float]]]) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for values in triggers.values():
        rows.extend(values)
    return rows


def _signal_weight(weights: dict[str, float], signal_type: str, regime: str) -> float:
    return max(resolve_signal_weight_multiplier(weights, signal_type, regime=regime), 0.0)


def _score(item: tuple[str, float]) -> float:
    return float(item[1] if item[1] is not None else 0.0)


def _result_map(result: Any, name: str) -> dict:
    value = getattr(result, name, {}) or {}
    return value if isinstance(value, dict) else {}


def _result_list(result: Any, name: str) -> list:
    value = getattr(result, name, []) or []
    return value if isinstance(value, list) else list(value)


def _result_set(result: Any, name: str) -> set[str]:
    return {str(value).strip() for value in _result_list(result, name) if str(value).strip()}
