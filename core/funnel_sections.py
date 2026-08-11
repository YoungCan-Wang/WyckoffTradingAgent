"""Renderable report sections for Wyckoff funnel notifications."""

from __future__ import annotations

from collections.abc import Callable

from core.candidate_ranker import TRIGGER_GROUP_ORDER, TRIGGER_GROUP_TITLES, TRIGGER_SHORT_LABELS
from core.funnel_format import fmt_pct, fmt_ratio


def append_formal_l4_sections(
    lines: list[str],
    formal_codes: list[str],
    selected_codes: list[str],
    name_map: dict[str, str],
    code_to_trigger_keys: dict[str, list[str]],
    display_score: Callable[[str], float],
    theme_badge_map: dict[str, str] | None = None,
    confirmation_label: Callable[[str], str] | None = None,
) -> None:
    selected_set = set(selected_codes)
    badge_map = theme_badge_map or {}
    confirmation_fn = confirmation_label or (lambda _code: "")
    multi_signal = [code for code in formal_codes if len(code_to_trigger_keys.get(code, [])) > 1]
    _append_multi_signal_rows(
        lines, multi_signal, selected_set, name_map, code_to_trigger_keys, display_score, badge_map, confirmation_fn
    )
    _append_single_signal_rows(
        lines,
        formal_codes,
        multi_signal,
        selected_set,
        name_map,
        code_to_trigger_keys,
        display_score,
        badge_map,
        confirmation_fn,
    )


def score_star(score: float) -> str:
    if score >= 10:
        return "★★"
    if score >= 5:
        return "★ "
    return "  "


def _leader_row(row: dict, name_map: dict[str, str]) -> str:
    code = str(row.get("code", "") or "").strip()
    name = name_map.get(code, code)
    parts = [
        f"分{float(row.get('score', 0.0) or 0.0):.2f}",
        f"20日{fmt_pct(row.get('ret20'))}",
        f"60日{fmt_pct(row.get('ret60'))}",
        f"120日{fmt_pct(row.get('ret120'))}",
        f"量{fmt_ratio(row.get('vol_ratio_5_20'))}",
        str(row.get("risk", "") or "主升跟踪"),
    ]
    suffix = " / ".join(x for x in [str(row.get("sector", "") or ""), str(row.get("channel", "") or "")] if x)
    return f"  {code} {name}  {' | '.join(parts)}" + (f"  [{suffix}]" if suffix else "")


def _append_multi_signal_rows(
    lines: list[str],
    codes: list[str],
    selected_set: set[str],
    name_map: dict[str, str],
    code_to_trigger_keys: dict[str, list[str]],
    display_score: Callable[[str], float],
    badge_map: dict[str, str],
    confirmation_label: Callable[[str], str],
) -> None:
    if not codes:
        return
    lines.append(f"**【🔥 多信号共振】{len(codes)} 只**")
    for code in sorted(codes, key=lambda c: -float(display_score(c))):
        short = "+".join(TRIGGER_SHORT_LABELS.get(k, k) for k in code_to_trigger_keys.get(code, []))
        lines.append(
            _formal_l4_row(code, selected_set, name_map, display_score, badge_map, confirmation_label, f"  {short}")
        )
    lines.append("")


def _append_single_signal_rows(
    lines: list[str],
    formal_codes: list[str],
    multi_signal: list[str],
    selected_set: set[str],
    name_map: dict[str, str],
    code_to_trigger_keys: dict[str, list[str]],
    display_score: Callable[[str], float],
    badge_map: dict[str, str],
    confirmation_label: Callable[[str], str],
) -> None:
    multi_signal_set = set(multi_signal)
    single_codes = [c for c in formal_codes if c not in multi_signal_set and code_to_trigger_keys.get(c)]
    primary_key = {code: code_to_trigger_keys.get(code, [""])[0] for code in single_codes}
    for group_key in TRIGGER_GROUP_ORDER:
        group_codes = [code for code in single_codes if primary_key.get(code) == group_key]
        if group_codes:
            _append_signal_group(
                lines, group_key, group_codes, selected_set, name_map, display_score, badge_map, confirmation_label
            )


def _append_signal_group(
    lines: list[str],
    group_key: str,
    group_codes: list[str],
    selected_set: set[str],
    name_map: dict[str, str],
    display_score: Callable[[str], float],
    badge_map: dict[str, str],
    confirmation_label: Callable[[str], str],
) -> None:
    lines.append(f"**【{TRIGGER_GROUP_TITLES.get(group_key, group_key)}】{len(group_codes)} 只**")
    for code in sorted(group_codes, key=lambda c: -float(display_score(c))):
        lines.append(_formal_l4_row(code, selected_set, name_map, display_score, badge_map, confirmation_label))
    lines.append("")


def _formal_l4_row(
    code: str,
    selected_set: set[str],
    name_map: dict[str, str],
    display_score: Callable[[str], float],
    badge_map: dict[str, str],
    confirmation_label: Callable[[str], str],
    extra: str = "",
) -> str:
    score = float(display_score(code))
    ai_mark = "  →AI" if code in selected_set else ""
    badge = f"  {badge_map[code]}" if code in badge_map else ""
    confirmation_raw = confirmation_label(code)
    confirmation = f"  {confirmation_raw}" if confirmation_raw else ""
    return f"{score_star(score)} {code} {name_map.get(code, code)}  {score:.2f}{ai_mark}{extra}{confirmation}{badge}"
