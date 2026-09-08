"""Backtest CLI artifact writing helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from core.backtest_metrics import fmt_metric
from core.backtest_report import build_summary_md

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestArtifactResult:
    summary_md: str
    summary_path: Path
    trades_path: Path
    extra_paths: tuple[Path, ...]


def backtest_stamp(start_dt: date, end_dt: date, hold_days: int, top_n: int) -> str:
    return f"{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}_h{hold_days}_n{top_n}"


def write_backtest_artifacts(
    *,
    out_dir: Path,
    stamp: str,
    trades_df: pd.DataFrame,
    summary: dict,
) -> BacktestArtifactResult:
    summary_path = out_dir / f"summary_{stamp}.md"
    trades_path = out_dir / f"trades_{stamp}.csv"
    summary_md = build_summary_md(summary)
    summary_path.write_text(summary_md + "\n", encoding="utf-8")
    trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
    extra_paths = [
        *_write_optional_frame(out_dir / f"nav_{stamp}.csv", summary.pop("_nav_df", None), "nav"),
        *_write_style_frames(out_dir, stamp, summary.pop("_cash_portfolio_trades_by_style", None), "cash_trades"),
        *_write_style_frames(out_dir, stamp, summary.pop("_cash_portfolio_nav_by_style", None), "cash_nav"),
        *_write_optional_frame(
            out_dir / f"wbt_weights_{stamp}.csv", summary.pop("_wbt_weight_df", None), "wbt weights"
        ),
        *_write_optional_frame(
            out_dir / f"wbt_daily_return_{stamp}.csv",
            summary.pop("_wbt_daily_return_df", None),
            "wbt daily",
        ),
        *_write_optional_frame(out_dir / f"wbt_dailys_{stamp}.csv", summary.pop("_wbt_dailys_df", None), "wbt dailys"),
        *_write_optional_frame(out_dir / f"wbt_pairs_{stamp}.csv", summary.pop("_wbt_pairs_df", None), "wbt pairs"),
    ]
    if "selection_coverage" in summary:
        coverage_path = out_dir / f"selection_coverage_{stamp}.json"
        coverage = {
            key: summary.get(key)
            for key in ("pending_mode", "ai_selection_mode", "mainline_selection_evaluated", "selection_coverage")
        }
        coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        extra_paths.append(coverage_path)
    return BacktestArtifactResult(summary_md, summary_path, trades_path, tuple(extra_paths))


def success_suite_row(hold_days: int, summary: dict) -> dict:
    return {
        "hold_days": hold_days,
        "trades": summary.get("trades"),
        "win_rate_pct": summary.get("win_rate_pct"),
        "avg_ret_pct": summary.get("avg_ret_pct"),
        "median_ret_pct": summary.get("median_ret_pct"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "sharpe_ratio": summary.get("sharpe_ratio"),
        "cash_final": summary.get("cash_portfolio_final_cash"),
        "cash_win_rate_pct": summary.get("cash_portfolio_win_rate_pct"),
        "error": "",
    }


def error_suite_row(hold_days: int, error: str) -> dict:
    row = success_suite_row(hold_days, {})
    row["error"] = error
    return row


def write_suite_summary(
    *,
    out_dir: Path,
    start_dt: date,
    end_dt: date,
    suite_rows: list[dict],
    success_count: int,
    candidate_mode: str,
) -> tuple[Path, Path] | None:
    if len(suite_rows) <= 1:
        return None
    suite_df = pd.DataFrame(suite_rows).sort_values("hold_days").reset_index(drop=True)
    suite_stamp = f"{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    suite_csv = out_dir / f"suite_{suite_stamp}.csv"
    suite_md = out_dir / f"suite_{suite_stamp}.md"
    suite_df.to_csv(suite_csv, index=False, encoding="utf-8-sig")
    suite_md.write_text(
        _suite_markdown(start_dt, end_dt, suite_rows, suite_df, success_count, candidate_mode),
        encoding="utf-8",
    )
    logger.info("suite summary -> %s", suite_md)
    logger.info("suite csv     -> %s", suite_csv)
    return suite_md, suite_csv


def _write_optional_frame(path: Path, df: pd.DataFrame | None, label: str) -> list[Path]:
    if df is None or df.empty:
        return []
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("%s -> %s", label, path)
    return [path]


def _write_style_frames(out_dir: Path, stamp: str, frames_by_style: object, prefix: str) -> list[Path]:
    if not isinstance(frames_by_style, dict):
        return []
    paths: list[Path] = []
    for style, frame in sorted(frames_by_style.items()):
        paths.extend(_write_optional_frame(out_dir / f"{prefix}_{style}_{stamp}.csv", frame, prefix.replace("_", " ")))
    return paths


def _suite_markdown(
    start_dt: date,
    end_dt: date,
    suite_rows: list[dict],
    suite_df: pd.DataFrame,
    success_count: int,
    candidate_mode: str,
) -> str:
    lines = [
        "# AI 输入候选多周期回测汇总",
        "",
        f"- 区间: {start_dt.isoformat()} ~ {end_dt.isoformat()}",
        f"- 候选池: 送给 AI 的股票（mode={candidate_mode}）",
        f"- 持有周期: {', '.join(str(row['hold_days']) for row in suite_rows)}",
        f"- 成功周期数: {success_count}/{len(suite_rows)}",
        "",
        "| 持有天数 | 成交笔数 | 胜率(%) | 平均收益(%) | 中位收益(%) | 最大回撤(%) | 夏普比 | 现金终值 | 现金胜率(%) | 备注 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    lines.extend(_suite_table_row(row) for row in suite_df.to_dict(orient="records"))
    return "\n".join(lines) + "\n"


def _suite_table_row(row: dict) -> str:
    fields = [
        str(int(row.get("hold_days", 0))),
        fmt_metric(row.get("trades"), 0),
        fmt_metric(row.get("win_rate_pct"), 2),
        fmt_metric(row.get("avg_ret_pct"), 3),
        fmt_metric(row.get("median_ret_pct"), 3),
        fmt_metric(row.get("max_drawdown_pct"), 3),
        fmt_metric(row.get("sharpe_ratio"), 3),
        fmt_metric(row.get("cash_final"), 2),
        fmt_metric(row.get("cash_win_rate_pct"), 2),
        str(row.get("error", "") or "").replace("|", "/"),
    ]
    return "| " + " | ".join(fields) + " |"
