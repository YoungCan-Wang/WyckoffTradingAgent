"""Step4 LLM decision parsing and portfolio-level buy throttling."""

from __future__ import annotations

import json
import logging
import re

from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES, PROBE_ONLY_REGIMES, normalize_regime
from utils.json_text import extract_json_block
from workflows.step4_models import DecisionItem, NewBuyLimits
from workflows.step4_text import clean_text

logger = logging.getLogger(__name__)

# Prefer risk-reducing actions when the model emits duplicate codes.
_ACTION_PRIORITY = {
    "EXIT": 1,
    "TRIM": 2,
    "HOLD": 3,
    "PROBE": 4,
    "ATTACK": 5,
}


def parse_decisions(
    raw_text: str,
    allowed_codes: set[str],
    name_map: dict[str, str],
) -> tuple[str, list[DecisionItem], str | None]:
    try:
        data = json.loads(extract_json_block(raw_text))
    except Exception as e:
        return ("", [], f"json_parse_failed: {e}")

    market_view = str(data.get("market_view", "")).strip()
    raw_decisions = data.get("decisions", []) or []
    if not isinstance(raw_decisions, list):
        return (market_view, [], "decisions_not_list")

    valid_actions = {"EXIT", "TRIM", "HOLD", "PROBE", "ATTACK"}
    out: list[DecisionItem] = []
    for item in raw_decisions:
        decision = _parse_decision_item(
            item,
            allowed_codes=allowed_codes,
            name_map=name_map,
            valid_actions=valid_actions,
        )
        if decision:
            out.append(decision)
    return (market_view, _dedupe_decisions(out), None)


def _dedupe_decisions(decisions: list[DecisionItem]) -> list[DecisionItem]:
    selected: dict[str, DecisionItem] = {}
    order: list[str] = []
    for decision in decisions:
        prev = selected.get(decision.code)
        if prev is None:
            selected[decision.code] = decision
            order.append(decision.code)
            continue
        if _ACTION_PRIORITY.get(decision.action, 99) < _ACTION_PRIORITY.get(prev.action, 99):
            selected[decision.code] = decision
    if len(selected) < len(decisions):
        logger.warning(
            "dropped duplicate step4 decisions: before=%s after=%s codes=%s",
            len(decisions),
            len(selected),
            ",".join(order),
        )
    return [selected[code] for code in order]


def max_new_buy_names(market_regime: str, limits: NewBuyLimits) -> int:
    regime = normalize_regime(clean_text(market_regime))
    if regime in EXECUTE_BLOCK_NEW_BUY_REGIMES:
        return 0
    if regime in PROBE_ONLY_REGIMES:
        return max(min(limits.caution, 1), 0)
    return max(limits.neutral, 0)


def trim_new_buy_decisions(
    decisions: list[DecisionItem],
    held_codes: set[str],
    market_regime: str,
    limits: NewBuyLimits,
) -> tuple[list[DecisionItem], list[str], int]:
    max_new_names = max_new_buy_names(market_regime, limits)
    new_buys = [
        dec
        for dec in decisions
        if dec.action in {"PROBE", "ATTACK"} and dec.code not in held_codes and not dec.system_reject_reason
    ]
    if len(new_buys) <= max_new_names:
        return decisions, [], max_new_names

    keep_codes = {dec.code for dec in sorted(new_buys, key=_new_buy_rank_key, reverse=True)[:max_new_names]}
    dropped = [dec.code for dec in new_buys if dec.code not in keep_codes]
    trimmed = [
        dec
        for dec in decisions
        if not (
            dec.action in {"PROBE", "ATTACK"}
            and dec.code not in held_codes
            and not dec.system_reject_reason
            and dec.code not in keep_codes
        )
    ]
    return trimmed, dropped, max_new_names


def _new_buy_rank_key(dec: DecisionItem) -> tuple[float, float, float, int]:
    evidence_score = dec.funnel_score if dec.funnel_score is not None else float("-inf")
    capital_migration_bonus = dec.capital_migration_bonus if dec.capital_migration_bonus is not None else 0.0
    confidence = dec.confidence if dec.confidence is not None else -1.0
    action_rank = 1 if dec.action == "ATTACK" else 0
    return (evidence_score, capital_migration_bonus, confidence, action_rank)


def _parse_bool_like(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    value = str(v or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off", ""}:
        return False
    return False


def _parse_confidence_like(v: object) -> float | None:
    if v is None:
        return None
    value = str(v).strip()
    if not value:
        return None
    try:
        if value.endswith("%"):
            pct = float(value[:-1].strip())
            return pct / 100.0 if 0.0 <= pct <= 100.0 else None
        raw = float(value)
        if 0.0 <= raw <= 1.0:
            return raw
        if 1.0 < raw <= 100.0:
            return raw / 100.0
    except Exception:
        logger.debug("_parse_confidence_like failed for %s", v, exc_info=True)
        return None
    return None


def _parse_entry_zone(raw_zone: object, code: str) -> tuple[float | None, float | None]:
    if not isinstance(raw_zone, list) or len(raw_zone) < 2:
        return None, None
    try:
        z1 = float(raw_zone[0])
        z2 = float(raw_zone[1])
        return min(z1, z2), max(z1, z2)
    except Exception:
        logger.debug("entry_zone parse failed for %s", code, exc_info=True)
        return None, None


def _parse_decision_float(item: dict, key: str, code: str) -> float | None:
    if item.get(key) is None:
        return None
    try:
        return float(item.get(key))
    except Exception:
        logger.debug("%s parse failed for %s", key, code, exc_info=True)
        return None


def _parse_decision_item(
    item: object,
    *,
    allowed_codes: set[str],
    name_map: dict[str, str],
    valid_actions: set[str],
) -> DecisionItem | None:
    if not isinstance(item, dict):
        return None
    code = str(item.get("code", "")).strip()
    action = str(item.get("action", "")).strip().upper()
    if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes or action not in valid_actions:
        return None
    entry_zone_min, entry_zone_max = _parse_entry_zone(item.get("entry_zone"), code)
    stop_loss = _parse_decision_float(item, "stop_loss", code)
    trim_ratio = _parse_decision_float(item, "trim_ratio", code)
    if stop_loss is not None and stop_loss <= 0:
        stop_loss = None
    return DecisionItem(
        code=code,
        name=str(item.get("name", "")).strip() or name_map.get(code, code),
        action=action,
        entry_zone_min=entry_zone_min,
        entry_zone_max=entry_zone_max,
        stop_loss=stop_loss,
        trim_ratio=trim_ratio,
        tape_condition=str(item.get("tape_condition", "")).strip(),
        invalidate_condition=str(item.get("invalidate_condition", "")).strip(),
        is_add_on=_parse_bool_like(item.get("is_add_on", False)),
        reason=str(item.get("reason", "")).strip(),
        confidence=_parse_confidence_like(item.get("confidence")),
    )
