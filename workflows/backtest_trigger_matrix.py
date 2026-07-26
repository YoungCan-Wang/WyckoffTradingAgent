"""Sweep one candidate-generating parameter, re-running the full funnel per value.

退出参数网格可以复用同一份信号台账，这里扫的参数不行：改变 spring_vol_ratio 这类检测参数
会改变"有哪些信号"，改变 top_n 会改变"哪些信号被执行"，每个取值都必须完整重跑漏斗。

两类扫描的读法不同，不能混：
- 触发阈值（`<触发器>_*` 命名）：看目标触发器的单信号统计并做 walk-forward 选值。只改一个
  触发器的阈值时，组合指标会被其它触发器稀释，看不出该阈值本身的好坏。
- `top_n`：看全样本统计。它衡量的是排序/选择层相对原始信号池的增益，笔数本就随取值大幅
  变化，套用触发阈值那套"笔数不得崩塌"的选值规则会直接判错。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.wyckoff_engine import FunnelConfig

logger = logging.getLogger(__name__)

# 低于该笔数的样本不参与选值：均收会被个位数交易的噪音主导。
MIN_TRIGGER_TRADES = 20

# 选择层扫描维度：0 表示不截断（原始 L4 信号池），正整数为每日取前 N 名。
SELECTION_PARAM = "top_n"


@dataclass(frozen=True)
class TriggerGrid:
    param: str
    values: tuple[float, ...]

    @property
    def focus_trigger(self) -> str:
        """检测参数按 `<触发器>_*` 命名，取前缀即为它作用的触发器；选择层扫描没有目标触发器。"""
        if self.param == SELECTION_PARAM:
            return ""
        return self.param.split("_", 1)[0]


def parse_trigger_grid(raw: str) -> TriggerGrid | None:
    """解析 `spring_vol_ratio=1.3,1.5,1.8` 或 `top_n=0,1`；空串表示不启用矩阵。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.count("=") != 1:
        raise ValueError(f"非法 trigger grid: {raw}")
    param, values_raw = text.split("=")
    param = param.strip()
    values = tuple(float(item) for item in values_raw.split(",") if item.strip())
    if not param or not values:
        raise ValueError(f"非法 trigger grid: {raw}")
    if param == SELECTION_PARAM:
        if any(value < 0 or value != int(value) for value in values):
            raise ValueError(f"top_n 必须为非负整数: {raw}")
        return TriggerGrid(param=param, values=values)
    # 参数名拼错要在拉快照之前就报错，否则要等整轮全市场取数之后才失败。
    if not hasattr(FunnelConfig(), param):
        raise ValueError(f"未知 FunnelConfig 字段: {param}")
    if any(value <= 0 for value in values):
        raise ValueError(f"触发阈值必须为正: {raw}")
    return TriggerGrid(param=param, values=values)


def trigger_value_dir(prefix: str, param: str, value: float) -> str:
    return f"{prefix}-{param}-{value:g}"


def focus_trigger_stats(summary: dict, focus_trigger: str) -> dict[str, Any]:
    """从分层统计里取出目标触发器的样本。

    by_trigger 的键带确认状态后缀（如 `spring(确认)`），按前缀匹配后合并笔数与均收。
    """
    by_trigger = (summary.get("stratified") or {}).get("by_trigger") or {}
    matched = [stats for key, stats in by_trigger.items() if str(key).startswith(focus_trigger)]
    trades = sum(int(item.get("trades") or 0) for item in matched)
    if trades <= 0:
        return {"trigger_trades": 0, "trigger_avg_ret_pct": None, "trigger_win_rate_pct": None}
    return {
        "trigger_trades": trades,
        "trigger_avg_ret_pct": _weighted(matched, "avg_ret_pct", trades),
        "trigger_win_rate_pct": _weighted(matched, "win_rate_pct", trades),
    }


def _weighted(rows: list[dict], field: str, total: int) -> float | None:
    weighted = [(int(row.get("trades") or 0), float(row[field])) for row in rows if row.get(field) is not None]
    if not weighted:
        return None
    return sum(count * value for count, value in weighted) / total


def build_matrix_row(
    *,
    grid: TriggerGrid,
    value: float,
    period_key: str,
    start_dt: date,
    end_dt: date,
    summary: dict,
) -> dict[str, Any]:
    focus = focus_trigger_stats(summary, grid.focus_trigger) if grid.focus_trigger else {}
    return {
        "param": grid.param,
        "value": value,
        "focus_trigger": grid.focus_trigger,
        "period_key": period_key,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        **focus,
        "total_trades": summary.get("trades"),
        "overall_avg_ret_pct": summary.get("avg_ret_pct"),
        "overall_win_rate_pct": summary.get("win_rate_pct"),
        "cash_total_return_pct": summary.get("cash_portfolio_total_return_pct"),
        "sharpe_ratio": summary.get("sharpe_ratio"),
    }


def write_matrix_artifacts(out_dir: Path, grid: TriggerGrid, rows: list[dict[str, Any]]) -> Path:
    payload = {
        "param": grid.param,
        "focus_trigger": grid.focus_trigger,
        "min_trigger_trades": MIN_TRIGGER_TRADES,
        "rows": rows,
    }
    json_path = out_dir / f"trigger_matrix_{grid.param}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / f"trigger_matrix_{grid.param}.md").write_text(_matrix_markdown(grid, rows), encoding="utf-8")
    logger.info("trigger matrix -> %s", json_path)
    return json_path


def _matrix_markdown(grid: TriggerGrid, rows: list[dict[str, Any]]) -> str:
    header = (
        [
            f"- 目标触发器: {grid.focus_trigger}",
            f"- 选值口径: 该触发器单信号均收，笔数下限 {MIN_TRIGGER_TRADES}",
        ]
        if grid.focus_trigger
        else ["- 读数口径: 全样本均收（选择层增益，不看单触发器）"]
    )
    lines = [
        f"# 参数矩阵：{grid.param}",
        "",
        *header,
        "",
        "| 取值 | 周期 | 目标笔数 | 目标胜率(%) | 目标均收(%) | 全部笔数 | 全样本均收(%) | 现金总收益(%) |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        "| "
        + " | ".join(
            [
                f"{row['value']:g}",
                str(row.get("period_key") or "-"),
                _fmt(row.get("trigger_trades"), 0),
                _fmt(row.get("trigger_win_rate_pct"), 2),
                _fmt(row.get("trigger_avg_ret_pct"), 3),
                _fmt(row.get("total_trades"), 0),
                _fmt(row.get("overall_avg_ret_pct"), 3),
                _fmt(row.get("cash_total_return_pct"), 2),
            ]
        )
        + " |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _fmt(value: object, digits: int) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


# 训练期内笔数低于同周期最高笔数的该比例，视为"笔数崩塌"，即使均收更高也不选。
TRADE_COLLAPSE_FLOOR = 0.6


def build_trigger_walk_forward(rows: list[dict[str, Any]], *, param: str, focus_trigger: str) -> dict[str, Any]:
    """按时间顺序滚动：训练期选阈值，下一周期检验该阈值是否仍然占优。"""
    periods = _ordered_periods(rows)
    by_period = {period: [row for row in rows if row.get("period_key") == period] for period in periods}
    windows = [
        window
        for train, test in zip(periods, periods[1:], strict=False)
        if (window := _evaluate_trigger_window(train, test, by_period)) is not None
    ]
    improved = sum(bool(row["test_beats_baseline"]) for row in windows)
    status = "review" if len(windows) < 2 else ("pass" if improved == len(windows) else "fail")
    return {
        "status": status,
        "param": param,
        "focus_trigger": focus_trigger,
        "criterion": f"{focus_trigger} 单信号均收最大，且笔数不低于同周期最高笔数的 {TRADE_COLLAPSE_FLOOR:.0%}",
        "window_count": len(windows),
        "improved_window_count": improved,
        "recommended_value": _recommended_value(windows),
        "windows": windows,
    }


def build_selection_lift(rows: list[dict[str, Any]], *, param: str) -> dict[str, Any]:
    """选择层增益：以最小取值（top_n=0，即未截断的原始信号池）为基线比较全样本均收。

    这里不做 walk-forward 选值。问题不是"选哪个 top_n"，而是"排序层相对随机取全池是否加分"，
    每个周期都是一次独立检验，不需要用前一周期去预测后一周期。
    """
    values = sorted({float(row["value"]) for row in rows})
    baseline_value = values[0] if values else 0.0
    comparisons = [
        comparison
        for period in _ordered_periods(rows)
        for comparison in _period_lift(rows, period, baseline_value, values[1:])
    ]
    improved = sum(comparison["lift_pct"] > 0 for comparison in comparisons)
    status = "review" if len(comparisons) < 2 else ("pass" if improved == len(comparisons) else "fail")
    return {
        "status": status,
        "param": param,
        "focus_trigger": "",
        "criterion": f"全样本均收高于 {param}={baseline_value:g} 的原始信号池",
        "baseline_value": baseline_value,
        "improved_count": improved,
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
    }


def _period_lift(
    rows: list[dict[str, Any]], period: str, baseline_value: float, values: list[float]
) -> list[dict[str, Any]]:
    by_value = {float(row["value"]): row for row in rows if row.get("period_key") == period}
    baseline = by_value.get(baseline_value)
    if baseline is None or baseline.get("overall_avg_ret_pct") is None:
        return []
    base_avg = float(baseline["overall_avg_ret_pct"])
    return [
        {
            "period_key": period,
            "value": value,
            "baseline_avg_ret_pct": base_avg,
            "baseline_trades": baseline.get("total_trades"),
            "avg_ret_pct": float(row["overall_avg_ret_pct"]),
            "trades": row.get("total_trades"),
            "lift_pct": float(row["overall_avg_ret_pct"]) - base_avg,
        }
        for value in values
        if (row := by_value.get(value)) is not None and row.get("overall_avg_ret_pct") is not None
    ]


def _ordered_periods(rows: list[dict[str, Any]]) -> list[str]:
    period_end: dict[str, str] = {}
    for row in rows:
        period = str(row.get("period_key") or "")
        if period:
            period_end[period] = max(period_end.get(period, ""), str(row.get("end") or ""))
    return sorted(period_end, key=lambda period: (period_end[period], period))


def _evaluate_trigger_window(
    train_period: str, test_period: str, by_period: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    selected = select_trigger_value(by_period.get(train_period, []))
    if selected is None:
        return None
    test_rows = by_period.get(test_period, [])
    test_match = next((row for row in test_rows if row["value"] == selected["value"]), None)
    baseline = select_trigger_value(test_rows)
    test_avg = test_match.get("trigger_avg_ret_pct") if test_match else None
    best_avg = baseline.get("trigger_avg_ret_pct") if baseline else None
    return {
        "train_period": train_period,
        "test_period": test_period,
        "selected_value": selected["value"],
        "train_avg_ret_pct": selected.get("trigger_avg_ret_pct"),
        "train_trades": selected.get("trigger_trades"),
        "test_avg_ret_pct": test_avg,
        "test_trades": test_match.get("trigger_trades") if test_match else None,
        "test_best_value": baseline["value"] if baseline else None,
        "test_best_avg_ret_pct": best_avg,
        "test_beats_baseline": test_avg is not None and best_avg is not None and test_avg >= best_avg,
    }


def select_trigger_value(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """均收最大者胜出，但先剔除样本过少和笔数崩塌的取值。"""
    eligible = [
        row
        for row in rows
        if row.get("trigger_avg_ret_pct") is not None and int(row.get("trigger_trades") or 0) >= MIN_TRIGGER_TRADES
    ]
    if not eligible:
        return None
    floor = max(int(row.get("trigger_trades") or 0) for row in eligible) * TRADE_COLLAPSE_FLOOR
    kept = [row for row in eligible if int(row.get("trigger_trades") or 0) >= floor] or eligible
    return max(kept, key=lambda row: float(row["trigger_avg_ret_pct"]))


def _recommended_value(windows: list[dict[str, Any]]) -> float | None:
    """样本外均收合计最高的取值；没有可用窗口时不给建议。"""
    scored: dict[float, float] = {}
    for window in windows:
        if window.get("test_avg_ret_pct") is None:
            continue
        value = float(window["selected_value"])
        scored[value] = scored.get(value, 0.0) + float(window["test_avg_ret_pct"])
    return max(scored, key=lambda key: scored[key]) if scored else None


def load_trigger_matrix_rows(artifacts_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for path in sorted(artifacts_dir.glob("**/trigger_matrix_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            key = (str(row.get("param")), str(row.get("period_key")), float(row.get("value", 0.0)))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def build_trigger_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    params = sorted({str(row["param"]) for row in rows if row.get("param")})
    reports = [_build_param_report(param, [row for row in rows if row.get("param") == param]) for param in params]
    statuses = {report["status"] for report in reports}
    return {
        "status": "review" if not reports or "review" in statuses else ("fail" if "fail" in statuses else "pass"),
        "params": reports,
        "rows": rows,
    }


def _build_param_report(param: str, param_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if param == SELECTION_PARAM:
        return build_selection_lift(param_rows, param=param)
    return build_trigger_walk_forward(
        param_rows,
        param=param,
        focus_trigger=next(str(row.get("focus_trigger") or "") for row in param_rows),
    )


def render_trigger_report(report: dict[str, Any]) -> str:
    lines = ["# 回测参数扫描", "", f"- 总体状态: {report['status']}", ""]
    for item in report["params"]:
        renderer = _render_lift_section if item["param"] == SELECTION_PARAM else _render_param_section
        lines.extend(renderer(item))
    return "\n".join(lines) + "\n"


def _render_lift_section(item: dict[str, Any]) -> list[str]:
    lines = [
        f"## {item['param']}（选择层增益）",
        "",
        f"- 判定口径: {item['criterion']}",
        f"- 增益为正的周期数: {item['improved_count']}/{item['comparison_count']}",
        "",
        "| 周期 | 取值 | 基线笔数 | 基线均收(%) | 笔数 | 均收(%) | 增益(pct) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        "| "
        + " | ".join(
            [
                comparison["period_key"],
                f"{comparison['value']:g}",
                _fmt(comparison.get("baseline_trades"), 0),
                _fmt(comparison.get("baseline_avg_ret_pct"), 3),
                _fmt(comparison.get("trades"), 0),
                _fmt(comparison.get("avg_ret_pct"), 3),
                _fmt(comparison.get("lift_pct"), 3),
            ]
        )
        + " |"
        for comparison in item["comparisons"]
    )
    lines.append("")
    return lines


def _render_param_section(item: dict[str, Any]) -> list[str]:
    recommended = item.get("recommended_value")
    lines = [
        f"## {item['param']}",
        "",
        f"- 选值口径: {item['criterion']}",
        f"- 样本外占优窗口: {item['improved_window_count']}/{item['window_count']}",
        f"- 建议取值: {recommended:g}" if recommended is not None else "- 建议取值: 样本不足，维持现值",
        "",
        "| 训练期 | 测试期 | 训练期选值 | 训练均收(%) | 训练笔数 | 测试均收(%) | 测试笔数 | 测试期最优值 | 样本外占优 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for window in item["windows"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    window["train_period"],
                    window["test_period"],
                    f"{window['selected_value']:g}",
                    _fmt(window.get("train_avg_ret_pct"), 3),
                    _fmt(window.get("train_trades"), 0),
                    _fmt(window.get("test_avg_ret_pct"), 3),
                    _fmt(window.get("test_trades"), 0),
                    f"{window['test_best_value']:g}" if window.get("test_best_value") is not None else "-",
                    "是" if window["test_beats_baseline"] else "否",
                ]
            )
            + " |"
        )
    lines.append("")
    return lines
