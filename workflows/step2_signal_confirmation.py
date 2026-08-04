"""Step2.5 pending signal confirmation workflow."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from core.candidate_metadata import (
    build_candidate_signal_metadata_map,
    candidate_metadata_for_signal,
    candidate_signal_triggers,
    code6,
    merge_trigger_maps,
)
from core.signal_confirmation import SIGNAL_TTL_DAYS, build_snap, run_confirmation_cycle
from integrations.supabase_signal_pending import batch_update_signals, insert_pending_signal_rows, load_pending_signals

logger = logging.getLogger(__name__)


def build_pending_signal_rows(
    *,
    signal_date: str,
    triggers: dict[str, list[tuple[str, float]]],
    df_map: dict[str, pd.DataFrame],
    regime: str = "NEUTRAL",
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    candidate_metadata_map: dict[str, dict[str, Any]] | None = None,
    cfg: Any = None,
) -> list[dict[str, Any]]:
    name_map, sector_map = name_map or {}, sector_map or {}
    candidate_metadata_map = candidate_metadata_map or {}
    now_iso = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    for signal_type, hits in triggers.items():
        for code, score in hits:
            df = df_map.get(code)
            if df is None or df.empty:
                continue
            code_s = code6(code)
            rows.append(
                {
                    "code": int(code_s) if code_s.isdigit() else 0,
                    "signal_type": signal_type,
                    "signal_date": signal_date,
                    "signal_score": float(score),
                    "status": "pending",
                    "ttl_days": SIGNAL_TTL_DAYS.get(signal_type, 3),
                    "days_elapsed": 0,
                    "regime": regime,
                    "name": name_map.get(code_s, code_s),
                    "industry": sector_map.get(code_s, ""),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    **build_snap(signal_type, df, score, cfg),
                    **candidate_metadata_for_signal(candidate_metadata_map, code_s, signal_type),
                }
            )
    return rows


def _confirmation_trigger_map(
    triggers: dict[str, list[tuple[str, float]]],
    candidate_entries: list[dict[str, Any]] | None,
) -> dict[str, list[tuple[str, float]]]:
    return merge_trigger_maps(triggers, candidate_signal_triggers(candidate_entries))


def run_step2_5(
    signal_date: str,
    triggers: dict[str, list[tuple[str, float]]],
    df_map: dict[str, pd.DataFrame],
    regime: str = "NEUTRAL",
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    candidate_entries: list[dict[str, Any]] | None = None,
    mainline_candidates: list[dict[str, Any]] | None = None,
    cfg: Any = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Write fresh pending signals and confirm/expire existing pending signals."""
    triggers = _confirmation_trigger_map(triggers, candidate_entries)
    metadata_map = build_candidate_signal_metadata_map(candidate_entries, mainline_candidates)
    if not dry_run:
        rows = build_pending_signal_rows(
            signal_date=signal_date,
            triggers=triggers,
            df_map=df_map,
            regime=regime,
            name_map=name_map,
            sector_map=sector_map,
            candidate_metadata_map=metadata_map,
            cfg=cfg,
        )
        insert_pending_signal_rows(rows)
    pending = load_pending_signals()
    if not pending:
        return []
    updates, confirmed = run_confirmation_cycle(pending, df_map, signal_date)
    if updates and not dry_run:
        batch_update_signals(updates)
    elif updates:
        logger.info("dry-run: skipped %s pending signal status updates", len(updates))
    return confirmed
