"""Point-in-time evaluation of the fundamental overlay on historical trades."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.fundamental_overlay import FUNDAMENTAL_OVERLAY_SCHEMA_VERSION, evaluate_fundamental_overlay

PROMOTION_RULES = {
    "minimum_trades": 100,
    "minimum_coverage_pct": 60.0,
    "minimum_retained_pct": 60.0,
    "minimum_passing_horizons": 2,
    "minimum_avg_return_delta_pct": 0.20,
    "minimum_p10_delta_pct": 0.50,
    "minimum_drawdown_delta_pct": 0.00,
    "maximum_big_loss_delta_pct": 0.00,
    "maximum_safety_regression_pct": 1.00,
    "minimum_positive_window_ratio": 0.60,
}


def load_trade_files(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        frame = pd.read_csv(path, dtype={"code": str})
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["hold_days"] = _hold_days_from_path(path)
        frame["research_window"] = _research_window_from_path(path)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_tickflow_history(codes: list[str], api_key: str) -> dict[str, list[dict]]:
    from integrations.tickflow_client import TickFlowClient

    raw = TickFlowClient(api_key=api_key).get_financial_metrics(codes, latest=False)
    return {_plain_code(symbol): records for symbol, records in raw.items() if isinstance(records, list)}


def attach_point_in_time_overlay(trades: pd.DataFrame, history: dict[str, list[dict]]) -> pd.DataFrame:
    rows = []
    for trade in trades.to_dict(orient="records"):
        record = latest_public_record(history.get(str(trade.get("code", "")).zfill(6), []), trade["signal_date"])
        overlay = evaluate_fundamental_overlay(record, signal_date=trade["signal_date"])
        rows.append({**trade, **_overlay_columns(record, overlay)})
    return pd.DataFrame(rows)


def latest_public_record(records: list[dict], signal_date: Any) -> dict | None:
    """Use only reports announced strictly before the signal date."""
    signal = _as_date(signal_date)
    eligible = []
    for record in records:
        announced = _as_date(record.get("announce_date") or record.get("ann_date"))
        period_end = _as_date(record.get("period_end") or record.get("end_date"))
        if signal and announced and period_end and announced < signal and period_end <= signal:
            eligible.append((announced, period_end, record))
    return max(eligible, key=lambda item: (item[0], item[1]))[2] if eligible else None


def build_overlay_evidence(enriched: pd.DataFrame) -> dict[str, Any]:
    horizons = {}
    windows = {}
    for hold_days, frame in enriched.groupby("hold_days"):
        horizons[str(int(hold_days))] = _comparison(frame)
        for window, window_frame in frame.groupby("research_window"):
            windows[f"h{int(hold_days)}:{window}"] = _comparison(window_frame)
    decision = _promotion_decision(enriched, horizons, windows)
    return {
        "schema_version": FUNDAMENTAL_OVERLAY_SCHEMA_VERSION,
        "research_only": True,
        "point_in_time_contract": "announce_date < signal_date",
        "strategy_under_test": "exclude grade=weak; unknown records remain in baseline",
        "promotion_rules": PROMOTION_RULES,
        "overall": _comparison(enriched),
        "grade_cohorts": {
            str(grade): _stats(frame) for grade, frame in enriched.groupby("fundamental_grade", sort=True)
        },
        "horizons": horizons,
        "windows": windows,
        "decision": decision,
    }


def write_overlay_artifacts(
    output_dir: Path, enriched: pd.DataFrame, evidence: dict[str, Any]
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "fundamental_overlay_trades.csv"
    evidence_path = output_dir / "fundamental_overlay_evidence.json"
    report_path = output_dir / "fundamental_overlay_report.md"
    enriched.to_csv(trades_path, index=False, encoding="utf-8-sig")
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_overlay_report(evidence), encoding="utf-8")
    return trades_path, evidence_path, report_path


def render_overlay_report(evidence: dict[str, Any]) -> str:
    overall = evidence["overall"]
    lines = [
        "# 基本面质量 Overlay 历史回放",
        "",
        f"- 决策: **{evidence['decision']['status']}** — {evidence['decision']['reason']}",
        f"- Point-in-time: `{evidence['point_in_time_contract']}`",
        f"- 样本: {overall['baseline']['trades']} 笔；财务覆盖率 {overall['coverage_pct']:.2f}%",
        f"- 弱基本面剔除后保留: {overall['overlay']['trades']} 笔（{overall['retained_pct']:.2f}%）",
        "",
        "| 周期 | 基线均收 | Overlay均收 | Δ均收 | ΔP10 | Δ大亏率 | Δ事件回撤 | 正向窗口 | 通过 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for hold, row in sorted(evidence["horizons"].items(), key=lambda item: int(item[0])):
        lines.append(
            f"| {hold} | {row['baseline']['avg_ret_pct']:.3f}% | {row['overlay']['avg_ret_pct']:.3f}% | "
            f"{row['delta']['avg_ret_pct']:+.3f}pp | {row['delta']['p10_ret_pct']:+.3f}pp | "
            f"{row['delta']['big_loss_rate_pct']:+.3f}pp | {row['delta']['event_max_drawdown_pct']:+.3f}pp | "
            f"{row['positive_window_ratio'] * 100:.1f}% | "
            f"{'是' if row.get('passes') else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 分档诊断",
            "",
            "| 档位 | 样本 | 均收 | P10 | 大亏率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for grade, row in evidence["grade_cohorts"].items():
        lines.append(
            f"| {grade} | {row['trades']} | {row['avg_ret_pct']:.3f}% | "
            f"{row['p10_ret_pct']:.3f}% | {row['big_loss_rate_pct']:.3f}% |"
        )
    lines.extend(
        [
            "",
            "## 研究边界",
            "",
            "- 股票池来自当前在市股票，仍有幸存者偏差。",
            "- TickFlow 历史财务记录按公告日匹配，但供应商若回填修订值，仍可能有轻微重述偏差。",
            "- 最大回撤为按信号日等权事件收益曲线，用于 overlay 相对比较，不等同真实现金账户 NAV。",
            "- 本实验不改变 confirmed、市场闸门、候选排序或 OMS。",
            "",
        ]
    )
    return "\n".join(lines)


def _comparison(frame: pd.DataFrame) -> dict[str, Any]:
    baseline = _stats(frame)
    overlay_frame = frame[frame["fundamental_grade"] != "weak"] if not frame.empty else frame
    overlay = _stats(overlay_frame)
    coverage = float((frame["fundamental_grade"] != "unknown").mean() * 100) if not frame.empty else 0.0
    retained = float(len(overlay_frame) / len(frame) * 100) if len(frame) else 0.0
    return {
        "coverage_pct": round(coverage, 3),
        "retained_pct": round(retained, 3),
        "baseline": baseline,
        "overlay": overlay,
        "delta": {key: round(overlay[key] - baseline[key], 3) for key in _DELTA_FIELDS},
    }


_DELTA_FIELDS = ("avg_ret_pct", "p10_ret_pct", "big_loss_rate_pct", "event_max_drawdown_pct", "win_rate_pct")


def _stats(frame: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(frame.get("ret_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    event_returns = (
        frame.assign(_ret=pd.to_numeric(frame.get("ret_pct"), errors="coerce"))
        .groupby("signal_date")["_ret"]
        .mean()
        .dropna()
        if not frame.empty
        else pd.Series(dtype=float)
    )
    return {
        "trades": int(len(returns)),
        "avg_ret_pct": round(float(returns.mean()), 3) if len(returns) else 0.0,
        "p10_ret_pct": round(float(returns.quantile(0.10)), 3) if len(returns) else 0.0,
        "big_loss_rate_pct": round(float((returns <= -10).mean() * 100), 3) if len(returns) else 0.0,
        "win_rate_pct": round(float((returns > 0).mean() * 100), 3) if len(returns) else 0.0,
        "event_max_drawdown_pct": round(_event_max_drawdown(event_returns), 3),
    }


def _event_max_drawdown(event_returns: pd.Series) -> float:
    if event_returns.empty:
        return 0.0
    nav = (1 + event_returns / 100).cumprod()
    drawdown = (nav / nav.cummax() - 1) * 100
    return float(drawdown.min())


def _promotion_decision(enriched: pd.DataFrame, horizons: dict, windows: dict) -> dict[str, Any]:
    for hold_days, row in horizons.items():
        row["positive_window_ratio"] = _positive_window_ratio(hold_days, windows)
        row["passes"] = _horizon_passes(row)
    passing = sum(bool(row["passes"]) for row in horizons.values())
    overall = _comparison(enriched)
    safety_regression = any(
        row["delta"][field] < -PROMOTION_RULES["maximum_safety_regression_pct"]
        for row in horizons.values()
        for field in ("avg_ret_pct", "p10_ret_pct", "event_max_drawdown_pct")
    )
    ready = (
        overall["baseline"]["trades"] >= PROMOTION_RULES["minimum_trades"]
        and overall["coverage_pct"] >= PROMOTION_RULES["minimum_coverage_pct"]
        and passing >= PROMOTION_RULES["minimum_passing_horizons"]
        and not safety_regression
    )
    reason = f"{passing}/{len(horizons)} 个持有周期通过；样本={overall['baseline']['trades']}；覆盖率={overall['coverage_pct']:.1f}%"
    return {
        "status": "pass_for_shadow" if ready else "keep_research_only",
        "reason": reason,
        "passing_horizons": passing,
    }


def _horizon_passes(row: dict) -> bool:
    delta = row["delta"]
    return (
        row["coverage_pct"] >= PROMOTION_RULES["minimum_coverage_pct"]
        and row["retained_pct"] >= PROMOTION_RULES["minimum_retained_pct"]
        and delta["avg_ret_pct"] >= PROMOTION_RULES["minimum_avg_return_delta_pct"]
        and delta["p10_ret_pct"] >= PROMOTION_RULES["minimum_p10_delta_pct"]
        and delta["event_max_drawdown_pct"] >= PROMOTION_RULES["minimum_drawdown_delta_pct"]
        and delta["big_loss_rate_pct"] <= PROMOTION_RULES["maximum_big_loss_delta_pct"]
        and row["positive_window_ratio"] >= PROMOTION_RULES["minimum_positive_window_ratio"]
    )


def _positive_window_ratio(hold_days: str, windows: dict) -> float:
    hold_prefix = f"h{hold_days}:"
    positive_windows = [
        value["delta"]["avg_ret_pct"] > 0 for key, value in windows.items() if key.startswith(hold_prefix)
    ]
    return sum(positive_windows) / len(positive_windows) if positive_windows else 0.0


def _overlay_columns(record: dict | None, overlay: dict) -> dict[str, Any]:
    return {
        "fundamental_grade": overlay["grade"],
        "fundamental_action": overlay["action"],
        "fundamental_score": overlay["score"],
        "fundamental_report_period": (record or {}).get("period_end", ""),
        "fundamental_announce_date": (record or {}).get("announce_date", ""),
        "fundamental_available_fields": overlay["available_fields"],
        "fundamental_report_age_days": overlay["report_age_days"],
        "fundamental_positive_rules": ",".join(overlay["positive_rules"]),
        "fundamental_negative_rules": ",".join(overlay["negative_rules"]),
    }


def _hold_days_from_path(path: Path) -> int:
    match = re.search(r"_h(\d+)_", path.name)
    if not match:
        raise ValueError(f"cannot infer hold_days from {path}")
    return int(match.group(1))


def _research_window_from_path(path: Path) -> str:
    """Keep the market-period label when files live under a grid-cell directory."""
    parent = path.parent
    if parent.name.startswith("backtest-grid-"):
        return parent.parent.name
    return parent.name


def _plain_code(symbol: str) -> str:
    return str(symbol).split(".", 1)[0].zfill(6)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None
