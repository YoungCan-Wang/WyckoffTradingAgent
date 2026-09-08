"""Theme radar helpers used by the Wyckoff funnel."""

from __future__ import annotations

import pandas as pd

from core.candidate_policy import candidate_score_value
from core.theme_radar import normalize_theme_name
from utils.safe import safe_float

DEFAULT_THEME_RADAR_MAX_AGE_DAYS = 14


def theme_candidate_map(snapshot: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in snapshot.get("strategic_candidates") or []:
        code = str(item.get("code", "") or "").strip()
        if code:
            out[code] = item
    return out


def theme_badge_map(candidate_map: dict[str, dict]) -> dict[str, str]:
    badges: dict[str, str] = {}
    for code, item in candidate_map.items():
        theme = str(item.get("theme", "") or "").strip()
        theme_score = safe_float(item.get("theme_score"))
        if theme:
            badges[code] = f"战略主线:{theme}({theme_score:.2f})"
    return badges


def theme_bonus_map(candidate_map: dict[str, dict], bonus_max: float) -> dict[str, float]:
    bonuses: dict[str, float] = {}
    if bonus_max <= 0:
        return bonuses
    for code, item in candidate_map.items():
        score = _theme_bonus_score(item)
        if score > 0:
            bonuses[code] = round(score * bonus_max, 4)
    return bonuses


def capital_migration_bonus_map(
    candidate_map: dict[str, dict],
    migration: dict | None,
    *,
    bonus_max: float,
    penalty_max: float,
) -> dict[str, float]:
    if not candidate_map or not migration:
        return {}
    inflow_scores = _migration_theme_scores(migration.get("inflow"), bonus_max)
    outflow_scores = _migration_theme_scores(migration.get("outflow"), penalty_max)
    adjustments: dict[str, float] = {}
    for code, item in candidate_map.items():
        theme = _normalized_theme(item.get("theme"))
        if not theme:
            continue
        adjustment = inflow_scores.get(theme, 0.0) - outflow_scores.get(theme, 0.0)
        if adjustment:
            adjustments[code] = round(adjustment, 4)
    return adjustments


def capital_migration_badge_map(candidate_map: dict[str, dict], bonus_map: dict[str, float]) -> dict[str, str]:
    badges: dict[str, str] = {}
    for code, adjustment in bonus_map.items():
        theme = str((candidate_map.get(code) or {}).get("theme") or "").strip()
        if not theme:
            continue
        label = "资金迁入" if adjustment > 0 else "资金撤出"
        badges[code] = f"{label}:{theme}({adjustment:+.1f})"
    return badges


def append_theme_reasons(code_to_reasons: dict[str, list[str]], badge_map: dict[str, str]) -> None:
    for code, badge in badge_map.items():
        if code in code_to_reasons and badge not in code_to_reasons[code]:
            code_to_reasons[code].append(badge)


def apply_theme_bonus_to_scores(score_map: dict[str, float], bonus_map: dict[str, float]) -> None:
    for code, bonus in bonus_map.items():
        if code in score_map:
            score_map[code] = candidate_score_value(score_map.get(code)) + candidate_score_value(bonus)


def is_accum_trigger(keys: list[str]) -> bool:
    key_set = {str(k).strip().lower() for k in keys}
    return bool(key_set & {"spring", "lps", "compression"}) and not bool(key_set & {"sos", "evr", "trend_pullback"})


def promotion_limits(selected_for_ai: list[str], cap: int, total_cap: int | None) -> tuple[int | None, int | None]:
    item_left = None if cap <= 0 else max(int(cap), 0)
    if total_cap is None:
        return item_left, None
    return item_left, max(int(total_cap) - len(set(selected_for_ai)), 0)


def promote_theme_l4_for_ai(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    formal_hit_set: set[str],
    bonus_map: dict[str, float],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
    *,
    promotion_cap: int,
    total_cap: int | None = None,
) -> int:
    ranked = [code for code in formal_hit_set if code in bonus_map]
    ranked.sort(key=lambda c: (-candidate_score_value(code_to_total_score.get(c)), c))
    selected_seen = set(selected_for_ai)
    track_seen = set(trend_selected) | set(accum_selected)
    item_left, total_left = promotion_limits(selected_for_ai, promotion_cap, total_cap)
    return _append_theme_promotions(
        ranked,
        selected_for_ai,
        trend_selected,
        accum_selected,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
        selected_seen,
        track_seen,
        item_left,
        total_left,
    )


def select_linked_theme_radar(
    current_snapshot: dict,
    persisted_snapshot: dict | None,
    trade_date: str,
    *,
    enabled: bool,
    link_enabled: bool,
    max_age_days: int,
) -> tuple[dict, str]:
    if not enabled:
        return empty_theme_snapshot(trade_date), "disabled"
    current_is_today = theme_snapshot_age_days(current_snapshot, trade_date, max_age_days) == 0
    current = current_snapshot if current_is_today else empty_theme_snapshot(trade_date)
    if current_is_today and has_theme_radar_payload(current):
        return current, "current"
    if not link_enabled:
        return current, "current"
    if has_theme_radar_payload(persisted_snapshot):
        age_days = theme_snapshot_age_days(persisted_snapshot or {}, trade_date, max_age_days)
        if age_days <= max_age_days:
            return persisted_snapshot or current, "persisted_fallback"
    return current, "current"


def strategic_bypass_seed_codes(
    l1_passed: list[str],
    l2_passed: list[str],
    candidate_map: dict[str, dict],
    *,
    enabled: bool,
    min_theme_score: float,
    min_stock_score: float,
) -> list[str]:
    if not enabled:
        return []
    l2_set = {str(code).strip() for code in l2_passed if str(code).strip()}
    seeds: list[str] = []
    for code in l1_passed:
        code_s = str(code).strip()
        item = candidate_map.get(code_s) or {}
        if (
            code_s
            and code_s not in l2_set
            and strategic_bypass_candidate_ok(
                item,
                min_theme_score=min_theme_score,
                min_stock_score=min_stock_score,
            )
        ):
            seeds.append(code_s)
    return seeds


def strategic_bypass_candidate_ok(item: dict, *, min_theme_score: float, min_stock_score: float) -> bool:
    state = str(item.get("state", "") or "").strip().lower()
    if state == "decay":
        return False
    return (
        safe_float(item.get("theme_score")) >= min_theme_score
        and safe_float(item.get("stock_score")) >= min_stock_score
    )


def theme_snapshot_age_days(snapshot: dict, trade_date: str, fallback_days: int) -> int:
    try:
        snapshot_date = pd.to_datetime(str(snapshot.get("trade_date") or "")).date()
        current_date = pd.to_datetime(str(trade_date)).date()
        age_days = (current_date - snapshot_date).days
        return age_days if age_days >= 0 else fallback_days + 1
    except Exception:
        return fallback_days + 1


def has_theme_radar_payload(snapshot: dict | None) -> bool:
    if not snapshot:
        return False
    return bool(snapshot.get("themes") or snapshot.get("strategic_candidates"))


def empty_theme_snapshot(trade_date: str) -> dict:
    return {"trade_date": trade_date, "themes": [], "strategic_candidates": []}


def _theme_bonus_score(item: dict) -> float:
    theme_score = safe_float(item.get("theme_score"))
    stock_score = safe_float(item.get("stock_score"))
    return max(min(0.55 * theme_score + 0.45 * stock_score, 1.0), 0.0)


def _migration_theme_scores(rows: object, max_score: float) -> dict[str, float]:
    if max_score <= 0 or not isinstance(rows, list):
        return {}
    scores: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        theme = _normalized_theme(row.get("theme"))
        score = safe_float(row.get("score"))
        if theme and score > 0:
            scores[theme] = max(scores.get(theme, 0.0), min(score, 1.0) * max_score)
    return scores


def _normalized_theme(raw: object) -> str:
    text = str(raw or "").strip()
    return normalize_theme_name(text) or text


def _append_theme_promotions(
    ranked: list[str],
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
    selected_seen: set[str],
    track_seen: set[str],
    item_left: int | None,
    total_left: int | None,
) -> int:
    added = 0
    for code in ranked:
        score_map.setdefault(code, candidate_score_value(code_to_total_score.get(code)))
        if code not in selected_seen:
            if item_left == 0 or total_left == 0:
                break
            selected_for_ai.append(code)
            selected_seen.add(code)
            added += 1
            item_left = item_left - 1 if item_left is not None else None
            total_left = total_left - 1 if total_left is not None else None
        if code in track_seen:
            continue
        if is_accum_trigger(code_to_trigger_keys.get(code, [])):
            accum_selected.append(code)
        else:
            trend_selected.append(code)
        track_seen.add(code)
    return added
