"""Runtime workflow for strategic theme radar jobs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.theme_radar import build_theme_radar_snapshot
from integrations.market_metadata import CONCEPT_HEAT_HISTORY
from integrations.supabase_concept_heat import load_concept_heat_history_from_supabase
from integrations.theme_radar_storage import persist_theme_radar_snapshot
from utils.env import env_bool
from utils.feishu import send_feishu_notification
from utils.trading_clock import cn_today
from workflows.theme_radar_report import render_theme_radar_html, render_theme_radar_report
from workflows.wyckoff_funnel import run_funnel_job


@dataclass(frozen=True)
class ThemeRadarArtifacts:
    report: str
    markdown_path: Path
    html_path: Path | None


@dataclass(frozen=True)
class ThemeRadarNotification:
    attempted: bool
    ok: bool
    title: str
    reason: str


def run_theme_radar(*, with_news: bool = False, persist: bool = True) -> dict:
    events = _collect_events(with_news)
    _triggers, metrics = run_funnel_job(include_debug_context=True)
    debug = metrics.get("_debug", {}) or {}
    trade_date = str(metrics.get("end_trade_date") or cn_today().isoformat())
    dynamic_policy = _resolve_dynamic_policy(with_news, trade_date, metrics)
    snapshot = build_theme_radar_snapshot(
        trade_date=trade_date,
        concept_heat=metrics.get("concept_heat_full") or metrics.get("concept_heat", []) or [],
        concept_history=_load_concept_history(),
        concept_map=debug.get("concept_map", {}) or {},
        sector_map=debug.get("sector_map", {}) or {},
        df_map=metrics.get("all_df_map", {}) or {},
        events=events,
        name_map=debug.get("name_map", {}) or {},
        dynamic_policy=dynamic_policy,
    )
    if persist:
        result = persist_theme_radar_snapshot(snapshot)
        print(f"[theme_radar] persist: supabase={result.get('supabase', 0)}, sqlite={result.get('sqlite', 0)}")
    return snapshot


def write_theme_radar_artifacts(snapshot: dict, *, output: str, html_output: str) -> ThemeRadarArtifacts:
    report = render_theme_radar_report(snapshot)
    markdown_path = Path(output)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(report, encoding="utf-8")
    html_path = _write_html_artifact(snapshot, html_output)
    return ThemeRadarArtifacts(report=report, markdown_path=markdown_path, html_path=html_path)


def notify_theme_radar_report(
    snapshot: dict,
    report: str,
    *,
    webhook: str | None = None,
) -> ThemeRadarNotification:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip() if webhook is None else webhook.strip()
    trade_date = str(snapshot.get("trade_date") or "").strip()
    title = f"主线雷达周报 {trade_date}".strip()
    if not webhook:
        return ThemeRadarNotification(False, False, title, "FEISHU_WEBHOOK_URL 未配置")
    ok = bool(send_feishu_notification(webhook, title, report))
    return ThemeRadarNotification(True, ok, title, "ok" if ok else "failed")


def _write_html_artifact(snapshot: dict, html_output: str) -> Path | None:
    if not html_output:
        return None
    html_path = Path(html_output)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_theme_radar_html(snapshot), encoding="utf-8")
    return html_path


def _collect_events(with_news: bool) -> list[dict]:
    if not with_news:
        return []
    from integrations.theme_news import collect_theme_events

    return collect_theme_events()


def _resolve_dynamic_policy(with_news: bool, trade_date: str, metrics: dict) -> dict | None:
    if not env_bool("FUNNEL_POLICY_DISCOVERY_ENABLED", False):
        return None
    from integrations.policy_discovery import load_dynamic_policy_themes, refresh_dynamic_policy_themes

    if not with_news:
        return load_dynamic_policy_themes(trade_date=trade_date)
    return refresh_dynamic_policy_themes(
        trade_date=trade_date,
        ths_hot_events=metrics.get("ths_hot_events"),
    )


def _load_concept_history() -> dict:
    history = load_concept_heat_history_from_supabase()
    if history:
        return history
    if not CONCEPT_HEAT_HISTORY.exists():
        return {}
    with open(CONCEPT_HEAT_HISTORY, encoding="utf-8") as f:
        return json.load(f)
