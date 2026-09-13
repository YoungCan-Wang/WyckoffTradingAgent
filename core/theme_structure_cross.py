"""Wyckoff × Radar 主线交叉筛选：两道独立漏斗的交集，只作观察。

Radar 主线漏斗回答「这只票是否在当日扩散中的策划主题里」；
威科夫结构漏斗回答「这只票自己的量价/形态是否确认」。
买兴趣只看交集。这里不生成 next_buy、开盘带、股数或工单。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.candidate_metadata import code6
from core.candidate_policy import candidate_score_value
from core.candidate_tracks import MARKET_BLOCK_SUFFIX, normalize_candidate_entry_key, strip_lane_status_suffix

COHORT_PRIMARY = "primary"
COHORT_BROAD = "broad"
MAX_THEME_RANK = 5
PRIMARY_SOURCE_PREFIXES = ("signal_confirmed", "mainline")
PRIMARY_LANES = frozenset({"signal_confirmed", "mainline"})
PRIMARY_STATUSES = frozenset(
    {
        "formal_l4",
        "AI复核候选",
        "主线买点候选",
        "主线观察",
        "主题修复候选",
        "Lane",
        "Accum_B",
        "Accum_C",
    }
)
BROAD_LANES = frozenset({"sos", "spring", "main_force_entry", "wyckoff_structure"})
GATE_BLOCK_STATUSES = frozenset({"市场拦截观察", "禁新仓-影子观察"})
CROSS_SECTION_TITLE = "主线×威科夫交叉"
CROSS_EMPTY_DAY = "今日无交叉"
CROSS_MISSING_CREDS = "雷达库未配置，交叉未计算"
CROSS_FETCH_FAILED = "雷达库读取失败，交叉未计算"
CROSS_OBSERVATION_NOTE = "筛选观察，不是开盘带 / 不是买点"


def normalize_symbol_code(raw: Any) -> str:
    """统一成 6 位数字。威科夫常存 int/补零；Radar 用 001872.SZ。"""
    return code6(raw)


def is_gate_blocked(row: Mapping[str, Any]) -> bool:
    source = str(row.get("selection_source") or row.get("wy_source") or "")
    if source.endswith(MARKET_BLOCK_SUFFIX) or "market_blocked" in source:
        return True
    status = str(row.get("candidate_status") or row.get("status") or row.get("wy_status") or "")
    if status in GATE_BLOCK_STATUSES:
        return True
    return bool(row.get("market_blocked") or row.get("gate_blocked"))


def is_primary_strong(row: Mapping[str, Any]) -> bool:
    source = strip_lane_status_suffix(row.get("selection_source") or row.get("wy_source"))
    if any(source.startswith(prefix) for prefix in PRIMARY_SOURCE_PREFIXES):
        return True
    lane = _lane_of(row)
    if lane in PRIMARY_LANES:
        return True
    return bool(_status_values(row) & PRIMARY_STATUSES)


def is_broad_strong(row: Mapping[str, Any]) -> bool:
    return _lane_of(row) in BROAD_LANES


def classify_cohort(row: Mapping[str, Any]) -> str:
    if is_primary_strong(row):
        return COHORT_PRIMARY
    if is_broad_strong(row):
        return COHORT_BROAD
    return ""


def wyckoff_view(row: Mapping[str, Any]) -> dict[str, Any] | None:
    code = normalize_symbol_code(row.get("ts_code") or row.get("code") or row.get("symbol"))
    if not code:
        return None
    lane = _lane_of(row)
    status = _display_status(row)
    source = str(row.get("selection_source") or row.get("wy_source") or "").strip()
    return {
        "ts_code": code,
        "name": str(row.get("name") or "").strip(),
        "wy_lane": lane,
        "wy_status": status,
        "wy_source": source,
        "wy_score": candidate_score_value(
            row.get("wy_score") or row.get("priority_score") or row.get("score") or row.get("funnel_score")
        ),
        "gate_blocked": is_gate_blocked(row),
        "cohort": classify_cohort(row),
    }


def collect_wyckoff_views(*groups: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for group in groups:
        for raw in group or []:
            view = wyckoff_view(raw)
            if view is None or not view["cohort"]:
                continue
            current = best.get(view["ts_code"])
            if current is None or _stronger_view(view, current):
                best[view["ts_code"]] = view
    return list(best.values())


def intersect_cross_rows(
    *,
    trade_date: str,
    wyckoff_rows: Iterable[Mapping[str, Any]],
    radar_symbols: Iterable[Mapping[str, Any]],
    radar_themes: Iterable[Mapping[str, Any]],
    plan_codes: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """同一交易日：威科夫强 + Radar 当日 rank≤5 主题。一行一只票。"""
    theme_map = _theme_rank_map(radar_themes)
    radar_by_code = _radar_symbol_map(radar_symbols, theme_map)
    plans = {normalize_symbol_code(code) for code in (plan_codes or []) if normalize_symbol_code(code)}
    out: list[dict[str, Any]] = []
    for view in wyckoff_rows:
        code = normalize_symbol_code(view.get("ts_code") or view.get("code"))
        radar = radar_by_code.get(code)
        if not code or radar is None:
            continue
        out.append(_cross_row(trade_date, view, radar, code in plans))
    out.sort(key=lambda row: (row["cohort"] != COHORT_PRIMARY, int(row["theme_rank"] or 99), row["ts_code"]))
    return out


def display_cross_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("cohort") or "") == COHORT_PRIMARY]


def render_cross_section_lines(payload: Mapping[str, Any] | None) -> list[str]:
    if not payload or not payload.get("status"):
        return []
    status = str(payload.get("status") or "")
    rows = display_cross_rows(payload.get("rows") or [])
    lines = [f"**【🔎 {CROSS_SECTION_TITLE}】{len(rows)} 只**", CROSS_OBSERVATION_NOTE]
    if status == "missing_creds":
        lines.append(f"{CROSS_MISSING_CREDS}。需要 RADAR_SUPABASE_URL / RADAR_SUPABASE_SERVICE_ROLE_KEY。")
        return [*lines, ""]
    if status == "fetch_failed":
        error = str(payload.get("error") or "").strip()
        lines.append(f"{CROSS_FETCH_FAILED}{f'：{error}' if error else '。'}")
        return [*lines, ""]
    if not rows:
        lines.append(CROSS_EMPTY_DAY)
        return [*lines, ""]
    lines.extend(_cross_name_line(row) for row in rows)
    return [*lines, ""]


def render_cross_ticket_lines(payload: Mapping[str, Any] | None) -> list[str]:
    if not payload or not payload.get("status"):
        return []
    status = str(payload.get("status") or "")
    rows = display_cross_rows(payload.get("rows") or [])
    lines = [f"🔎 [{CROSS_SECTION_TITLE}] ({len(rows)})", CROSS_OBSERVATION_NOTE]
    if status == "missing_creds":
        return [*lines, CROSS_MISSING_CREDS, ""]
    if status == "fetch_failed":
        return [*lines, CROSS_FETCH_FAILED, ""]
    if not rows:
        return [*lines, CROSS_EMPTY_DAY, ""]
    for row in rows:
        lines.append(f"- {_cross_name_line(row).strip()}")
    lines.append("")
    return lines


def _lane_of(row: Mapping[str, Any]) -> str:
    raw = row.get("wy_lane") or row.get("candidate_lane") or row.get("entry_type") or row.get("signal_key")
    raw = raw or row.get("lane") or row.get("signal_type")
    return normalize_candidate_entry_key(strip_lane_status_suffix(raw))


def _status_values(row: Mapping[str, Any]) -> set[str]:
    values = {
        str(row.get(key) or "").strip() for key in ("candidate_status", "status", "wy_status", "stage", "state", "tag")
    }
    return {item for item in values if item}


def _display_status(row: Mapping[str, Any]) -> str:
    for key in ("candidate_status", "wy_status", "status", "stage", "state"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _stronger_view(new: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    new_primary = new.get("cohort") == COHORT_PRIMARY
    old_primary = current.get("cohort") == COHORT_PRIMARY
    if new_primary != old_primary:
        return new_primary
    return float(new.get("wy_score") or 0) > float(current.get("wy_score") or 0)


def _theme_rank_map(themes: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in themes or []:
        name = str(raw.get("theme") or raw.get("theme_name") or raw.get("name") or "").strip()
        rank = _optional_int(raw.get("rank") if raw.get("rank") is not None else raw.get("theme_rank"))
        if not name or rank is None or not 1 <= rank <= MAX_THEME_RANK:
            continue
        out[name] = dict(raw)
        out[name]["theme"] = name
        out[name]["rank"] = rank
    return out


def _radar_symbol_map(
    symbols: Iterable[Mapping[str, Any]],
    theme_map: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in symbols or []:
        code = normalize_symbol_code(raw.get("symbol") or raw.get("ts_code") or raw.get("code"))
        theme = str(raw.get("primary_theme") or raw.get("theme") or "").strip()
        theme_meta = theme_map.get(theme)
        if not code or theme_meta is None:
            continue
        out[code] = {
            "radar_theme": theme,
            "theme_rank": int(theme_meta["rank"]),
            "theme_status": str(theme_meta.get("status") or theme_meta.get("lifecycle_stage") or "").strip(),
            "radar_roles": _roles_text(raw.get("roles")),
            "action_state": str(raw.get("action_state") or "").strip(),
        }
    return out


def _cross_row(trade_date: str, view: Mapping[str, Any], radar: Mapping[str, Any], has_plan: bool) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "ts_code": view["ts_code"],
        "name": str(view.get("name") or "").strip() or None,
        "wy_lane": str(view.get("wy_lane") or "").strip() or None,
        "wy_status": str(view.get("wy_status") or "").strip() or None,
        "wy_source": str(view.get("wy_source") or "").strip() or None,
        "wy_score": _optional_score(view.get("wy_score")),
        "gate_blocked": bool(view.get("gate_blocked")),
        "cohort": view["cohort"],
        "radar_theme": radar["radar_theme"],
        "theme_rank": radar["theme_rank"],
        "theme_status": radar.get("theme_status") or None,
        "radar_roles": radar.get("radar_roles") or None,
        "action_state": radar.get("action_state") or None,
        "has_radar_plan": has_plan,
    }


def _cross_name_line(row: Mapping[str, Any]) -> str:
    blocked = "是" if row.get("gate_blocked") else "否"
    theme = str(row.get("radar_theme") or "?")
    rank = row.get("theme_rank")
    status = str(row.get("wy_status") or "-")
    lane = str(row.get("wy_lane") or "-")
    return (
        f"  {row['ts_code']} {row.get('name') or row['ts_code']}  {theme}#{rank}  {status}/{lane}  闸门拦截={blocked}"
    )


def _roles_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple, set)):
        return ",".join(str(item).strip() for item in raw if str(item).strip())
    return str(raw or "").strip()


def _optional_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_score(raw: Any) -> float | None:
    score = candidate_score_value(raw)
    return None if score == 0 and raw in (None, "") else round(float(score), 4)
