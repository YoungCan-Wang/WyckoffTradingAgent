"""Point-in-time evaluation for observation-only review shadow lanes."""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from core.funnel_effect_panels import build_panels_from_snapshot
from core.review_shadow_lanes import ReviewShadowSignal, shadow_lane_label, shadow_signal_from_decision
from workflows.backtest_data import load_snapshot_hist_map
from workflows.review_shadow_control import control_verdict_lines, lane_control_summary


@dataclass(frozen=True)
class ShadowTrade:
    signal_date: str
    entry_date: str
    code: str
    name: str
    lane: str
    score: float | None
    ranked: bool
    entry_open: float
    signal_pct_chg: float | None
    next_pct_chg: float | None
    review_hit: bool
    open_executable: bool
    intraday_executable: bool
    ret_t1_pct: float | None
    ret_t3_pct: float | None
    ret_t5_pct: float | None
    mfe_t5_pct: float | None
    mae_t5_pct: float | None


def run_shadow_backtest(trace_dir: Path, snapshot_dir: Path, output_dir: Path) -> dict[str, Any]:
    traces = load_trace_payloads(trace_dir)
    history, _ = load_snapshot_hist_map(snapshot_dir)
    trades = evaluate_shadow_traces(traces, history)
    # 同动量对照要全市场面板，与 history 用同一份快照，动量口径由 core 单点定义。
    panels = build_panels_from_snapshot(snapshot_dir)
    report = summarize_shadow_trades(trades, traces, history, panels=panels)
    write_shadow_outputs(output_dir, trades, report)
    return report


def load_trace_payloads(trace_dir: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted(trace_dir.glob("review_trace_*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and payload.get("trade_date") and isinstance(payload.get("symbols"), dict):
            payloads.append(payload)
    return payloads


def evaluate_shadow_traces(
    traces: list[dict[str, Any]],
    history: dict[str, pd.DataFrame],
) -> list[ShadowTrade]:
    trades: list[ShadowTrade] = []
    next_dates = _next_trade_dates(traces, history)
    for payload in traces:
        signal_date = date.fromisoformat(str(payload["trade_date"]))
        next_date = next_dates.get(signal_date)
        if next_date is None:
            continue
        for code, raw in (payload.get("symbols") or {}).items():
            row = dict(raw or {})
            policy = payload.get("policy") or {}
            signal = shadow_signal_from_decision(
                row,
                near_l2_max_gap_pct=float(policy.get("shadow_near_l2_max_gap_pct") or 10.0),
            )
            if signal is None:
                continue
            trade = _shadow_trade(
                signal_date,
                next_date,
                str(code),
                row,
                signal,
                history.get(str(code)),
            )
            if trade is not None:
                trades.append(trade)
    return trades


def summarize_shadow_trades(
    trades: list[ShadowTrade],
    traces: list[dict[str, Any]],
    history: dict[str, pd.DataFrame] | None = None,
    *,
    panels: Any | None = None,
) -> dict[str, Any]:
    lanes = sorted({trade.lane for trade in trades})
    recall = _review_recall(traces, history or {})
    return {
        "mode": "observation_only",
        "uses_future_for_selection": False,
        "trace_days": len(traces),
        "evaluated_trace_days": recall["evaluated_trace_days"],
        "trades": len(trades),
        "review_recall": recall,
        "review_shadow_recall_rate": _ratio(recall["shadow_hits"], recall["review_hits"]),
        "review_candidate_recall_rate": _ratio(recall["candidate_hits"], recall["review_hits"]),
        "by_lane": {lane: _lane_summary([trade for trade in trades if trade.lane == lane]) for lane in lanes},
        "by_score_band": {lane: _score_band_summary([t for t in trades if t.lane == lane]) for lane in lanes},
        "overall": _lane_summary(trades),
        # 裸收益混着动量 beta。「要不要补强」只有配对超额跑赢随机负控制才算是"要"。
        "momentum_control": lane_control_summary(trades, panels),
    }


def _score_band_summary(trades: list[ShadowTrade]) -> dict[str, Any]:
    """按分值三分档拆开,看这个排序键到底有没有单调性。

    v1 的常数分让这张表全落进一个桶——那才是「无法效果检验」的具体形态。
    不可排序的车道(rotation_setup、缺 watch_score 的老 trace)显式返回
    ranked=False,不要在这里造一个假的分档结论。
    """
    ranked = [t for t in trades if t.ranked and t.score is not None]
    if not ranked:
        # 「本层无排序键」和「样本还不够」是两回事:前者攒数据也不会变,
        # 后者下个月就能重跑。混成一句话会让人以为轮动车道等等就能分档。
        if trades:
            return {"ranked": False, "reason": "本层无连续排序键，只作标签，不参与分档", "count": 0}
        return {"ranked": False, "reason": "无样本", "count": 0}
    if len(ranked) < 3:
        return {"ranked": False, "reason": "可排序样本不足 3 只，分档没有意义", "count": len(ranked)}
    scores = pd.Series([float(t.score) for t in ranked], dtype="float64")
    if scores.nunique() <= 1:
        return {"ranked": False, "reason": f"分值全同({scores.iloc[0]:.2f})，排序键无区分度", "count": len(ranked)}
    lo, hi = float(scores.quantile(1 / 3)), float(scores.quantile(2 / 3))
    bands = {
        "low": [t for t in ranked if float(t.score) <= lo],
        "mid": [t for t in ranked if lo < float(t.score) <= hi],
        "high": [t for t in ranked if float(t.score) > hi],
    }
    return {
        "ranked": True,
        "cut_points": [round(lo, 4), round(hi, 4)],
        "bands": {name: _lane_summary(rows) for name, rows in bands.items()},
    }


def write_shadow_outputs(output_dir: Path, trades: list[ShadowTrade], report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(trade) for trade in trades]).to_csv(output_dir / "review_shadow_trades.csv", index=False)
    (output_dir / "review_shadow_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "review_shadow_report.md").write_text(_markdown_report(report), encoding="utf-8")


def _shadow_trade(
    signal_date: date,
    next_date: date,
    code: str,
    row: dict[str, Any],
    signal: ReviewShadowSignal,
    frame: pd.DataFrame | None,
) -> ShadowTrade | None:
    signal_row, future = _signal_and_future_rows(frame, signal_date, next_date)
    if signal_row is None or future is None:
        return None
    entry = float(future["open"].iloc[0])
    signal_close = float(signal_row["close"])
    if entry <= 0 or signal_close <= 0:
        return None
    next_row = future.iloc[0]
    signal_pct = _row_pct_change(signal_row)
    next_pct = (float(next_row["close"]) / signal_close - 1.0) * 100.0
    one_price = _one_price_limit(next_row)
    if one_price:
        return None
    open_gap = (entry / signal_close - 1.0) * 100.0
    low_gap = (float(next_row.get("low", entry)) / signal_close - 1.0) * 100.0
    returns = {h: _close_return(future, entry, h) for h in (1, 3, 5)}
    mfe, mae = _excursions(future.head(5), entry)
    return ShadowTrade(
        signal_date=signal_date.isoformat(),
        entry_date=str(future["date"].iloc[0]),
        code=code,
        name=str(row.get("name") or code),
        lane=signal.lane,
        score=None if signal.score is None else float(signal.score),
        ranked=bool(signal.ranked),
        entry_open=entry,
        signal_pct_chg=signal_pct,
        next_pct_chg=next_pct,
        review_hit=signal_pct is not None and signal_pct < 3.0 and next_pct > 7.0,
        open_executable=open_gap <= 4.0 and not one_price,
        intraday_executable=low_gap <= 4.0 and not one_price,
        ret_t1_pct=returns[1],
        ret_t3_pct=returns[3],
        ret_t5_pct=returns[5],
        mfe_t5_pct=mfe,
        mae_t5_pct=mae,
    )


def _signal_and_future_rows(
    frame: pd.DataFrame | None,
    signal_date: date,
    next_date: date,
) -> tuple[pd.Series | None, pd.DataFrame | None]:
    if frame is None or frame.empty or not {"date", "open", "close"}.issubset(frame.columns):
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    signal_rows = frame.loc[dates == signal_date]
    future = frame.loc[dates >= next_date].sort_values("date").reset_index(drop=True)
    if signal_rows.empty or future.empty or pd.to_datetime(future["date"].iloc[0]).date() != next_date:
        return None, None
    return signal_rows.iloc[-1], future


def _one_price_limit(row: pd.Series) -> bool:
    if not {"high", "low"}.issubset(row.index):
        return False
    return abs(float(row["high"]) - float(row["low"])) <= 1e-8


def _next_trade_dates(
    traces: list[dict[str, Any]],
    history: dict[str, pd.DataFrame],
) -> dict[date, date]:
    available: set[date] = set()
    for frame in history.values():
        if frame is not None and not frame.empty and "date" in frame.columns:
            available.update(pd.to_datetime(frame["date"], errors="coerce").dt.date.dropna())
    result: dict[date, date] = {}
    for payload in traces:
        signal_date = date.fromisoformat(str(payload["trade_date"]))
        future = sorted(value for value in available if value > signal_date)
        if future:
            result[signal_date] = future[0]
    return result


def _row_pct_change(row: pd.Series) -> float | None:
    if "pct_chg" not in row.index or pd.isna(row["pct_chg"]):
        return None
    return float(row["pct_chg"])


def _review_recall(traces: list[dict[str, Any]], history: dict[str, pd.DataFrame]) -> dict[str, Any]:
    from workflows.review_big_gainers import is_target_cn_board

    by_day: list[dict[str, Any]] = []
    next_dates = _next_trade_dates(traces, history)
    for payload in traces:
        signal_date = date.fromisoformat(str(payload["trade_date"]))
        next_date = next_dates.get(signal_date)
        if next_date is None:
            continue
        row = _review_recall_day(payload, history, signal_date, next_date, is_target_cn_board)
        by_day.append(row)
    return {
        "evaluated_trace_days": len(by_day),
        "review_hits": sum(int(row["review_hits"]) for row in by_day),
        "shadow_hits": sum(int(row["shadow_hits"]) for row in by_day),
        "candidate_hits": sum(int(row["candidate_hits"]) for row in by_day),
        "shadow_open_executable_hits": sum(int(row["shadow_open_executable_hits"]) for row in by_day),
        "shadow_intraday_executable_hits": sum(int(row["shadow_intraday_executable_hits"]) for row in by_day),
        "by_day": by_day,
    }


def _review_recall_day(
    payload: dict[str, Any],
    history: dict[str, pd.DataFrame],
    signal_date: date,
    next_date: date,
    board_filter,
) -> dict[str, Any]:
    counts = {
        "signal_date": signal_date.isoformat(),
        "next_trade_date": next_date.isoformat(),
        "review_hits": 0,
        "shadow_hits": 0,
        "candidate_hits": 0,
        "shadow_open_executable_hits": 0,
        "shadow_intraday_executable_hits": 0,
    }
    for code, raw in (payload.get("symbols") or {}).items():
        if not board_filter(str(code)) or "ST" in str((raw or {}).get("name") or "").upper():
            continue
        observed = _review_observation(history.get(str(code)), signal_date, next_date)
        if observed is None or not observed["review_hit"]:
            continue
        counts["review_hits"] += 1
        if (raw or {}).get("entry"):
            counts["candidate_hits"] += 1
        maximum = float((payload.get("policy") or {}).get("shadow_near_l2_max_gap_pct") or 10.0)
        signal = shadow_signal_from_decision(dict(raw or {}), near_l2_max_gap_pct=maximum)
        if signal is None:
            continue
        counts["shadow_hits"] += 1
        counts["shadow_open_executable_hits"] += int(observed["open_executable"])
        counts["shadow_intraday_executable_hits"] += int(observed["intraday_executable"])
    return counts


def _review_observation(frame: pd.DataFrame | None, signal_date: date, next_date: date) -> dict[str, bool] | None:
    signal_row, future = _signal_and_future_rows(frame, signal_date, next_date)
    if signal_row is None or future is None:
        return None
    next_row = future.iloc[0]
    signal_close = float(signal_row["close"])
    signal_pct = _row_pct_change(signal_row)
    if signal_close <= 0 or signal_pct is None:
        return None
    next_pct = (float(next_row["close"]) / signal_close - 1.0) * 100.0
    one_price = _one_price_limit(next_row)
    open_gap = (float(next_row["open"]) / signal_close - 1.0) * 100.0
    low_gap = (float(next_row.get("low", next_row["open"])) / signal_close - 1.0) * 100.0
    return {
        "review_hit": signal_pct < 3.0 and next_pct > 7.0,
        "open_executable": open_gap <= 4.0 and not one_price,
        "intraday_executable": low_gap <= 4.0 and not one_price,
    }


def _close_return(future: pd.DataFrame, entry: float, horizon: int) -> float | None:
    if len(future) < horizon:
        return None
    return (float(future["close"].iloc[horizon - 1]) / entry - 1.0) * 100.0


def _excursions(rows: pd.DataFrame, entry: float) -> tuple[float | None, float | None]:
    if rows.empty or not {"high", "low"}.issubset(rows.columns):
        return None, None
    return (float(rows["high"].max()) / entry - 1.0) * 100.0, (float(rows["low"].min()) / entry - 1.0) * 100.0


def _lane_summary(trades: list[ShadowTrade]) -> dict[str, Any]:
    summary: dict[str, Any] = {"count": len(trades)}
    for field in ("ret_t1_pct", "ret_t3_pct", "ret_t5_pct", "mfe_t5_pct", "mae_t5_pct"):
        values = pd.Series([getattr(trade, field) for trade in trades], dtype="float64").dropna()
        summary[field] = {
            "count": int(len(values)),
            "mean": round(float(values.mean()), 4) if not values.empty else None,
            "median": round(float(values.median()), 4) if not values.empty else None,
            "win_rate": round(float((values > 0).mean()), 4) if not values.empty else None,
        }
    return summary


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(float(numerator) / float(denominator), 4) if denominator else None


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Review Shadow Lane Backtest",
        "",
        "候选仅由信号日收盘时的生产 trace 生成；T+1/T+3/T+5 行情只用于结果评价。",
        "",
        "下表是**裸收益**，混着动量 beta，不能单独用来判定「要不要补强」——结论看后面的同动量对照一节。",
        "",
        _recall_line(report),
        "",
        "| 车道 | 样本 | T+1均值 | T+3均值 | T+5均值 | T+5胜率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for lane, summary in report.get("by_lane", {}).items():
        t1, t3, t5 = (summary.get(f"ret_t{day}_pct") or {} for day in (1, 3, 5))
        lines.append(
            f"| {shadow_lane_label(lane)} | {summary.get('count', 0)} | {_metric(t1, 'mean')} | "
            f"{_metric(t3, 'mean')} | {_metric(t5, 'mean')} | {_metric(t5, 'win_rate', percent=False)} |"
        )
    lines.extend(_score_band_lines(report))
    lines.extend(["", *control_verdict_lines(report.get("momentum_control") or {})])
    return "\n".join(lines) + "\n"


def _score_band_lines(report: dict[str, Any]) -> list[str]:
    """分档表:排序键有没有单调性,只能靠这张表说话。"""
    bands = report.get("by_score_band") or {}
    if not bands:
        return []
    lines = ["", "## 排序键分档（低/中/高，看单调性）", ""]
    for lane, payload in bands.items():
        label = shadow_lane_label(lane)
        if not payload.get("ranked"):
            lines.append(f"- {label}：不可排序 —— {payload.get('reason', '未说明')}（样本 {payload.get('count', 0)}）")
            continue
        cuts = payload.get("cut_points") or []
        cut_text = "/".join(f"{c:.2f}" for c in cuts)
        parts = []
        for name in ("low", "mid", "high"):
            summary = (payload.get("bands") or {}).get(name) or {}
            t5 = summary.get("ret_t5_pct") or {}
            parts.append(f"{name} n={summary.get('count', 0)} T+5={_metric(t5, 'mean')}")
        lines.append(f"- {label}（切点 {cut_text}）：" + " | ".join(parts))
    return lines


def _recall_line(report: dict[str, Any]) -> str:
    recall = report.get("review_recall") or {}
    return (
        f"Review 强势样本 {recall.get('review_hits', 0)}，正式候选命中 {recall.get('candidate_hits', 0)}，"
        f"影子车道命中 {recall.get('shadow_hits', 0)}；影子命中中开盘可交易 "
        f"{recall.get('shadow_open_executable_hits', 0)}，盘中曾可交易 "
        f"{recall.get('shadow_intraday_executable_hits', 0)}。"
    )


def _metric(row: dict[str, Any], key: str, *, percent: bool = True) -> str:
    value = row.get(key)
    if value is None:
        return "—"
    scaled = float(value) * (100.0 if key == "win_rate" else 1.0)
    return f"{scaled:+.2f}%" if percent or key == "win_rate" else f"{scaled:.2f}%"
