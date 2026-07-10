"""Compatibility exports for local SQLite tail-buy history persistence."""

from __future__ import annotations

from typing import Any

from integrations.local_db import get_db, load_tail_buy_history


def save_tail_buy_results(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO tail_buy_history
               (code, name, run_date, signal_date, signal_type, status,
                final_decision, rule_decision, rule_score, priority_score, rule_reasons,
                llm_decision, llm_reason, llm_confidence, llm_model_used,
                initial_price, current_price, change_pct, price_updated_at,
                last_close, vwap, dist_vwap_pct, close_pos, day_ret_pct,
                last30_ret_pct, last15_ret_pct, tail30_volume_share, drop_from_high_pct,
                fetch_error, features_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            [_tail_buy_insert_values(r) for r in rows],
        )
    return len(rows)


def _tail_buy_insert_values(r: dict) -> tuple[Any, ...]:
    return (
        str(r.get("code", "")).strip(),
        str(r.get("name", "")).strip(),
        str(r.get("run_date", "")).strip(),
        str(r.get("signal_date", "")).strip(),
        str(r.get("signal_type", "")).strip(),
        str(r.get("status", "")).strip(),
        str(r.get("final_decision", "")).strip(),
        str(r.get("rule_decision", "")).strip(),
        float(r.get("rule_score", 0) or 0),
        float(r.get("priority_score", 0) or 0),
        str(r.get("rule_reasons", "")).strip(),
        str(r.get("llm_decision", "")).strip(),
        str(r.get("llm_reason", "")).strip(),
        r.get("llm_confidence"),
        str(r.get("llm_model_used", "")).strip(),
        float(r.get("initial_price", 0) or 0),
        float(r.get("current_price", 0) or 0),
        float(r.get("change_pct", 0) or 0),
        str(r.get("price_updated_at", "")).strip(),
        float(r.get("last_close", 0) or 0),
        float(r.get("vwap", 0) or 0),
        float(r.get("dist_vwap_pct", 0) or 0),
        float(r.get("close_pos", 0) or 0),
        float(r.get("day_ret_pct", 0) or 0),
        float(r.get("last30_ret_pct", 0) or 0),
        float(r.get("last15_ret_pct", 0) or 0),
        float(r.get("tail30_volume_share", 0) or 0),
        float(r.get("drop_from_high_pct", 0) or 0),
        str(r.get("fetch_error", "")).strip(),
        str(r.get("features_json", "")).strip(),
    )


__all__ = ["get_db", "save_tail_buy_results", "load_tail_buy_history"]
