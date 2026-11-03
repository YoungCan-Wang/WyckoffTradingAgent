"""Capital migration radar from existing theme and sector signals."""

from __future__ import annotations

from dataclasses import dataclass
from math import log1p
from typing import Any

from core._price_math import clamp as _clamp
from core.concept_filters import is_actionable_theme_name
from core.theme_radar import normalize_theme_name
from utils.safe import safe_float as _num


@dataclass(frozen=True)
class CapitalMigrationConfig:
    inflow_limit: int = 3
    outflow_limit: int = 3
    min_inflow_score: float = 0.45
    min_outflow_score: float = 0.45


def build_capital_migration_report(
    *,
    trade_date: str,
    concept_heat: list[dict[str, Any]],
    concept_history: dict[str, dict[str, Any]] | None,
    sector_rotation: dict[str, Any] | None,
    theme_radar: dict[str, Any] | None,
    theme_activity: dict[str, Any] | None = None,
    config: CapitalMigrationConfig | None = None,
) -> dict[str, Any]:
    cfg = config or CapitalMigrationConfig()
    current = _current_theme_rows(concept_heat)
    previous = _previous_theme_rows(concept_history or {}, trade_date)
    radar_map = _theme_radar_map(theme_radar or {})
    inflow = _top_inflow_rows(current, radar_map, cfg)
    outflow = _top_outflow_rows(current, previous, sector_rotation or {}, cfg)
    activity = _theme_activity_rows(theme_activity or {}, inflow)
    clusters = _cluster_rows(theme_radar or {}, inflow)
    squeeze = _policy_squeeze_rows(theme_radar or {})
    summary = _summary(inflow, outflow, clusters)
    return {
        "version": "capital_migration_v1",
        "trade_date": trade_date,
        "summary": summary,
        "confidence": _confidence(inflow, outflow, clusters),
        "inflow": inflow,
        "outflow": outflow,
        "activity": activity,
        "clusters": clusters,
        "squeeze": squeeze,
        "rotation": _rotation_lines(inflow, outflow, clusters),
    }


def _current_theme_rows(concept_heat: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(concept_heat or [], start=1):
        theme = _theme(item.get("name"))
        if not theme:
            continue
        row = rows.setdefault(theme, {"theme": theme, "concepts": [], "rank": rank, "pct": 0.0, "net_inflow": 0.0})
        row["rank"] = min(int(row["rank"]), rank)
        row["pct"] = max(float(row["pct"]), _num(item.get("pct")))
        row["net_inflow"] = float(row["net_inflow"]) + _num(item.get("net_inflow") or item.get("inflow"))
        row["concepts"].append(str(item.get("name", "")).strip())
    return rows


def _previous_theme_rows(history: dict[str, dict[str, Any]], trade_date: str) -> dict[str, dict[str, Any]]:
    dates = [d for d in sorted(history.keys(), reverse=True) if _date_key(d) != _date_key(trade_date)]
    if not dates:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for rank, (name, item) in enumerate((history.get(dates[0]) or {}).items(), start=1):
        theme = _theme(name)
        if not theme:
            continue
        rows[theme] = {
            "theme": theme,
            "rank": rank,
            "pct": _num(_field(item, "pct")),
            "net_inflow": _num(_field(item, "net_inflow") or _field(item, "inflow")),
        }
    return rows


def _theme_radar_map(theme_radar: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for item in theme_radar.get("themes") or []:
        theme = _theme(item.get("theme"))
        if theme:
            out[theme] = item
    return out


def _top_inflow_rows(
    current: dict[str, dict[str, Any]],
    radar_map: dict[str, dict[str, Any]],
    cfg: CapitalMigrationConfig,
) -> list[dict[str, Any]]:
    rows = []
    for theme, row in current.items():
        if _amount_yi(row.get("net_inflow")) <= 0:
            continue
        radar = radar_map.get(theme) or {}
        score = _current_score(row, radar)
        if score >= cfg.min_inflow_score:
            rows.append(_inflow_row(row, radar, score))
    return sorted(rows, key=lambda item: (-float(item["score"]), item["theme"]))[: cfg.inflow_limit]


def _top_outflow_rows(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    sector_rotation: dict[str, Any],
    cfg: CapitalMigrationConfig,
) -> list[dict[str, Any]]:
    rows = _history_outflows(current, previous) + _sector_outflows(sector_rotation)
    merged = _dedupe_outflows(rows)
    return sorted(merged, key=lambda item: (-float(item["score"]), item["theme"]))[: cfg.outflow_limit]


def _theme_activity_rows(theme_activity: dict[str, Any], inflow: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inflow_themes = {str(row.get("theme") or "") for row in inflow}
    rows = []
    for item in theme_activity.get("themes") or []:
        theme = str(item.get("theme") or "").strip()
        score = _num(item.get("score"))
        if not theme or theme in inflow_themes or score < 0.48:
            continue
        rows.append(
            {
                "theme": theme,
                "score": round(score, 4),
                "evidence": _activity_evidence(item),
                "source": "theme_activity",
            }
        )
    return sorted(rows, key=lambda item: (-float(item["score"]), item["theme"]))[:5]


def _activity_evidence(item: dict[str, Any]) -> str:
    return (
        f"成分股中位{_fmt_pct(item.get('median_ret'))}，"
        f"上涨占比{_fmt_percent_ratio(item.get('up_ratio'))}，"
        f"强势{int(_num(item.get('strong_count')))}只"
    )


def _cluster_rows(theme_radar: dict[str, Any], inflow: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inflow_themes = {str(row.get("theme") or "") for row in inflow}
    rows = []
    for item in theme_radar.get("cluster_watch") or []:
        theme = _theme(item.get("theme"))
        score = _num(item.get("cluster_score") or item.get("score"))
        if not theme or theme in inflow_themes or score < 0.45:
            continue
        rows.append(
            {
                "theme": theme,
                "score": round(score, 4),
                "evidence": _cluster_evidence(item),
                "source": "theme_cluster",
            }
        )
    return sorted(rows, key=lambda item: (-float(item["score"]), item["theme"]))[:3]


def _cluster_evidence(item: dict[str, Any]) -> str:
    bull = _fmt_percent_ratio(item.get("bull_alignment_ratio"))
    sync = int(_num(item.get("sync_breakout_count")))
    return f"MA50>MA200占{bull}，同频起跳{sync}只"


def _history_outflows(current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    current_scores = {theme: _current_score(row, {}) for theme, row in current.items()}
    for theme, prev in previous.items():
        prev_score = _current_score(prev, {})
        cur_score = current_scores.get(theme, 0.0)
        if cur_score >= prev_score * 0.65:
            continue
        rows.append(
            {
                "theme": theme,
                "score": round(min(1.0, prev_score - cur_score + 0.25), 4),
                "evidence": f"昨日热度掉队，前日净流入{_fmt_money(prev.get('net_inflow'))}",
                "source": "concept_heat_history",
            }
        )
    return rows


def _sector_outflows(sector_rotation: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for sector, info in (sector_rotation.get("state_map") or {}).items():
        state = str(info.get("state") or "")
        if state not in {"DISTRIBUTION_RISK", "CONSENSUS_CLIMAX"}:
            continue
        score = _sector_outflow_score(info, state)
        if score <= 0:
            continue
        rows.append(
            {
                "theme": str(sector),
                "score": round(score, 4),
                "evidence": _sector_evidence(info, state),
                "source": "sector_rotation",
            }
        )
    return rows


def _sector_outflow_score(info: dict[str, Any], state: str) -> float:
    ret3 = _num(info.get("ret_3d"))
    amount_ratio = _num(info.get("amount_ratio_3d"))
    breakdown = _num(info.get("breakdown_pct")) / 100.0
    if state == "DISTRIBUTION_RISK":
        return _clamp(0.58 + 0.22 * min(amount_ratio / 1.5, 1.0) + 0.20 * breakdown)
    if state == "CONSENSUS_CLIMAX" and ret3 < 0:
        return _clamp(0.45 + 0.20 * min(amount_ratio / 1.5, 1.0))
    return 0.0


def _policy_squeeze_rows(theme_radar: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in theme_radar.get("squeeze_watch") or []:
        theme = _theme(item.get("theme"))
        penalty = _num(item.get("penalty"))
        if not theme or penalty < 0.45:
            continue
        title = str(item.get("title") or "监管敲打/去杠杆信号").strip()[:48]
        rows.append(
            {
                "theme": theme,
                "score": round(_clamp(max(0.50, penalty)), 4),
                "evidence": f"政策敲打/去杠杆：{title}",
                "source": "policy_squeeze",
            }
        )
    return rows


def _dedupe_outflows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("theme") or "")
        if not key or float(row.get("score") or 0.0) <= 0:
            continue
        if key not in merged or float(row["score"]) > float(merged[key]["score"]):
            merged[key] = row
    return list(merged.values())


def _inflow_row(row: dict[str, Any], radar: dict[str, Any], score: float) -> dict[str, Any]:
    state = str(radar.get("state") or "")
    evidence = f"净流入{_fmt_money(row.get('net_inflow'))}，涨幅{_fmt_pct(row.get('pct'))}，热度排名#{row.get('rank')}"
    if state:
        evidence += f"，主线状态{state}"
    return {"theme": row["theme"], "score": round(score, 4), "evidence": evidence, "source": "concept_heat"}


def _current_score(row: dict[str, Any], radar: dict[str, Any]) -> float:
    rank = max(float(row.get("rank") or 99.0), 1.0)
    rank_score = _clamp(1.0 - (rank - 1.0) / 30.0)
    flow_score = _clamp(log1p(max(_amount_yi(row.get("net_inflow")), 0.0)) / 3.0)
    pct_score = _clamp((_num(row.get("pct")) + 2.0) / 10.0)
    radar_score = _num(radar.get("score"))
    return _clamp(0.34 * rank_score + 0.34 * flow_score + 0.22 * pct_score + 0.10 * radar_score)


def _sector_evidence(info: dict[str, Any], state: str) -> str:
    label = str(info.get("label") or state)
    return (
        f"{label}，3日{_fmt_pct(info.get('ret_3d'))}，"
        f"近3日成交额{_fmt_ratio(info.get('amount_ratio_3d'))}，"
        f"破位占比{_fmt_pct(info.get('breakdown_pct'))}"
    )


def _summary(
    inflow: list[dict[str, Any]],
    outflow: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
) -> str:
    target = inflow or (clusters or [])
    if target and outflow:
        return f"资金从{_names(outflow)}撤出，转向{_names(target)}"
    if inflow:
        return f"资金集中在{_names(inflow)}，暂未识别明确撤出方向"
    if clusters:
        return f"政策/机构抱团共振集中在{_names(clusters)}"
    if outflow:
        return f"{_names(outflow)}出现资金撤退迹象，尚未形成明确承接方向"
    return "暂无明确资金迁徙信号"


def _rotation_lines(
    inflow: list[dict[str, Any]],
    outflow: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
) -> list[str]:
    lines = []
    if inflow:
        lines.append("流入: " + "； ".join(f"{x['theme']}({x['evidence']})" for x in inflow))
    if clusters:
        lines.append("抱团: " + "； ".join(f"{x['theme']}({x['evidence']})" for x in clusters))
    if outflow:
        lines.append("流出: " + "； ".join(f"{x['theme']}({x['evidence']})" for x in outflow))
    target = inflow or (clusters or [])
    if target and outflow:
        lines.append(f"迁徙: {_names(outflow)} -> {_names(target)}")
    return lines


def _confidence(
    inflow: list[dict[str, Any]],
    outflow: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
) -> str:
    target = inflow or (clusters or [])
    if target and outflow and max(float(x["score"]) for x in target) >= 0.65:
        return "high"
    if target or outflow:
        return "medium"
    return "low"


def _theme(raw: Any) -> str:
    theme = normalize_theme_name(str(raw or ""))
    return theme if theme and is_actionable_theme_name(theme) else ""


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else None


def _date_key(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())[:8]


def _names(rows: list[dict[str, Any]]) -> str:
    return "、".join(str(x["theme"]) for x in rows[:3])


def _amount_yi(raw: Any) -> float:
    amount = _num(raw)
    if abs(amount) < 10_000:
        return amount
    return amount / 100_000_000.0


def _fmt_pct(value: Any) -> str:
    return f"{_num(value):+.1f}%"


def _fmt_ratio(value: Any) -> str:
    return f"{_num(value):.2f}x"


def _fmt_percent_ratio(value: Any) -> str:
    return f"{_num(value) * 100.0:.0f}%"


def _fmt_money(value: Any) -> str:
    amount = _num(value)
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.1f}亿"
    if 0 < abs(amount) < 10_000:
        return f"{amount:.1f}亿"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.0f}万"
    return f"{amount:.0f}"
