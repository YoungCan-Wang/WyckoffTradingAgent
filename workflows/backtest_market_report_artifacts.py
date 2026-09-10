"""Artifact parsing for the persistent backtest market report."""

from __future__ import annotations

import csv
import glob
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.backtest_periods import period_key_from_dirname


@dataclass(frozen=True)
class GridCell:
    artifact_dir: Path
    summary_path: Path
    trades_path: Path | None
    period_key: str
    portfolio_style: str
    portfolio_style_label: str
    hold: int
    stop_loss: int
    take_profit: int
    trailing_stop: int
    start: str
    end: str
    top_n: str
    board: str
    sample_size: str
    trades: int | None
    win_rate: float | None
    avg_ret: float | None
    median_ret: float | None
    max_drawdown: float | None
    sharpe: float | None
    calmar: float | None
    total_return: float | None
    cash_initial: float | None
    cash_final: float | None
    cash_total_return: float | None
    cash_trades: int | None
    cash_commission_total: float | None
    cash_max_drawdown: float | None
    wbt_sharpe: float | None
    wbt_max_drawdown: float | None
    wbt_daily_win_rate: float | None
    metrics_engine: str
    entry_price_mode: str
    strategy_policy: str
    #: 移动止盈激活门槛（浮盈达该%后启用）。0 = 入场即启用。默认值保证旧产物可解析。
    trailing_activate: int = 0


def load_grid_cells(artifacts_dir: Path) -> list[GridCell]:
    summaries = sorted(Path(p) for p in glob.glob(str(artifacts_dir / "**" / "summary_*.md"), recursive=True))
    cells: list[GridCell] = []
    for summary_path in summaries:
        params = _parse_params(summary_path.parent.name)
        if not params:
            continue
        content = summary_path.read_text(encoding="utf-8")
        meta = _summary_metadata(summary_path, content)
        for style_row in _parse_cash_style_rows(content) or _fallback_cash_style_rows(content):
            cells.append(_grid_cell_from_style(meta, content, params, style_row))
    return cells


def read_trades(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def win_rate_span(cells: Iterable[GridCell]) -> tuple[float | None, float | None]:
    """一组同参数格子的（平均胜率, 最差周期胜率），单位为百分点。

    跨周期稳健性那张表原本整行只报现金收益,于是「赚钱但十次只对四次」和「胜率高但不赚钱」
    在表上完全同形。这里只做展示用的汇总,不参与排序:排序键按现金收益排,换成胜率会退化——
    ``_robust_score`` 里权重最大的 ``positive_periods * 4.0`` 判的是「> 0」,而胜率没有负数,
    60 组参数会全部拿到满格 6/6,那一项就变成人人加同样分的常数。

    非有限值一律剔除而不是当 0:胜率 0.0 是「一笔没赢」这个真实取值,拿 NaN 冒充它会把
    读数拖低;而 ``min()`` 碰到 NaN 的结果取决于它在序列里的位置(``min([1.0, nan])`` 得
    1.0,``min([nan, 1.0])`` 得 nan),一个脏值就能静默改写整组的最差周期。
    """
    values = [cell.win_rate for cell in cells if cell.win_rate is not None and math.isfinite(cell.win_rate)]
    if not values:
        return None, None
    return sum(values) / len(values), min(values)


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace("%", "")
    text = text.replace("（wbt 可用）", "").replace("（wbt 不可用，已保留 legacy 指标）", "")
    if not text or text in {"-", "None", "nan"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _to_int(raw: str | None) -> int | None:
    value = _to_float(raw)
    return int(value) if value is not None else None


def _coalesce(value: object | None, fallback: object | None) -> object | None:
    return value if value is not None else fallback


def _extract_line_value(content: str, label_pattern: str) -> str | None:
    pattern = re.compile(rf"^\s*-\s*{label_pattern}\s*:\s*(.+?)\s*$")
    for line in content.splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def _extract_float(content: str, label_pattern: str) -> float | None:
    return _to_float(_extract_line_value(content, label_pattern))


def _extract_int(content: str, label_pattern: str) -> int | None:
    return _to_int(_extract_line_value(content, label_pattern))


def _parse_range(content: str) -> tuple[str, str]:
    raw = _extract_line_value(content, "区间")
    if not raw:
        return "", ""
    parts = [part.strip() for part in raw.split("~", 1)]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _parse_simple_field(content: str, label: str) -> str:
    raw = _extract_line_value(content, re.escape(label))
    return raw or ""


def _parse_board_sample(content: str) -> tuple[str, str]:
    raw = _parse_simple_field(content, "股票池")
    if not raw:
        return "", ""
    match = re.match(r"(.+?)\s*\(sample=(.+?)\)", raw)
    if not match:
        return raw, ""
    return match.group(1).strip(), match.group(2).strip()


def _parse_params(dirname: str) -> tuple[int, int, int, int, int] | None:
    """解析参数格目录名。

    含 ta 段（移动止盈激活门槛）——不解析它会让 tr8 / tr8-ta5 / tr8-ta7 三个 cell
    折叠成同一个 param key，参数稳定性验证器于是只评到其中一个（实测 2026-08-10
    的 run 31366326715 里 anchor 的 ta 是 None，即只评了 activate=0 那档，而那恰是
    三档中配对 t 最低的一档：ta0 +1.46 / ta5 +1.98 / ta7 +2.36）。
    """
    match = re.search(
        r"h(?P<hold>\d+).*?sl-?(?P<sl>\d+).*?tp(?P<tp>\d+)(?:.*?tr-?(?P<tr>\d+))?(?:.*?ta(?P<ta>\d+))?",
        dirname,
    )
    if not match:
        return None
    return (
        int(match.group("hold")),
        int(match.group("sl")),
        int(match.group("tp")),
        int(match.group("tr") or 0),
        int(match.group("ta") or 0),
    )


def _parse_period_key(dirname: str) -> str:
    # 名单收敛到 core.backtest_periods，避免 matrix 加周期时漏改这里：漏改不报错，
    # 只是 period_key 变空串，而 backtest_walk_forward 会静默丢掉空 key 的周期。
    return period_key_from_dirname(dirname)


def _split_md_row(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _parse_cash_style_rows(content: str) -> list[dict[str, str]]:
    lines = content.splitlines()
    rows: list[dict[str, str]] = []
    for idx, line in enumerate(lines):
        cells = _split_md_row(line) if line.strip().startswith("|") else []
        if cells[:2] != ["风格ID", "风格"]:
            continue
        rows.extend(_cash_style_rows_after_header(lines, idx, cells))
        break
    return rows


def _cash_style_rows_after_header(lines: list[str], idx: int, headers: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in lines[idx + 2 :]:
        if not raw.strip().startswith("|"):
            break
        values = _split_md_row(raw)
        if len(values) != len(headers):
            break
        rows.append(dict(zip(headers, values, strict=True)))
    return rows


def _fallback_cash_style_rows(content: str) -> list[dict[str, str]]:
    raw_style = _parse_simple_field(content, "主风格")
    return [
        {
            "风格ID": raw_style.split("(")[-1].rstrip(")") or "slot_equal_4",
            "风格": raw_style.split("(", 1)[0].strip() or "等额四仓",
            "最终现金": _extract_line_value(content, "最终现金") or "",
            "总收益": _extract_line_value(content, "总收益") or "",
            "现金回撤": _extract_line_value(content, "现金最大回撤") or "",
            "成交": _extract_line_value(content, "成交笔数") or "",
            "胜率": _extract_line_value(content, "胜率") or "",
            "佣金": _extract_line_value(content, "佣金合计") or "",
        }
    ]


def _find_trades_path(summary_path: Path) -> Path | None:
    matches = sorted(summary_path.parent.glob("trades_*.csv"))
    return matches[0] if matches else None


def _find_style_trades_path(summary_path: Path, portfolio_style: str) -> Path | None:
    prefix = f"cash_trades_{portfolio_style}_"
    matches = [path for path in sorted(summary_path.parent.glob("cash_trades_*.csv")) if path.name.startswith(prefix)]
    return matches[0] if matches else None


def _summary_metadata(summary_path: Path, content: str) -> dict[str, Any]:
    start, end = _parse_range(content)
    board, sample_size = _parse_board_sample(content)
    return {
        "artifact_dir": summary_path.parent,
        "summary_path": summary_path,
        "trades_path": _find_trades_path(summary_path),
        "period_key": _parse_period_key(summary_path.parent.name),
        "start": start,
        "end": end,
        "top_n": _parse_simple_field(content, "每日候选上限").replace("Top", "").strip(),
        "board": board,
        "sample_size": sample_size,
        "metrics_engine": _parse_simple_field(content, "绩效引擎"),
        "entry_price_mode": _parse_simple_field(content, "入场价格模式"),
        "strategy_policy": _parse_simple_field(content, "策略治理调权"),
    }


def _grid_cell_from_style(
    meta: dict[str, Any],
    content: str,
    params: tuple[int, int, int, int],
    style_row: dict[str, str],
) -> GridCell:
    hold, stop_loss, take_profit, trailing_stop, trailing_activate = params
    portfolio_style = style_row.get("风格ID", "") or "slot_equal_4"
    meta = {
        **meta,
        "trades_path": _find_style_trades_path(meta["summary_path"], portfolio_style) or meta["trades_path"],
    }
    return GridCell(
        **meta,
        portfolio_style=portfolio_style,
        portfolio_style_label=style_row.get("风格", "") or style_row.get("风格ID", ""),
        hold=hold,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_stop=trailing_stop,
        trailing_activate=trailing_activate,
        trades=_extract_int(content, "成交样本"),
        win_rate=_coalesce(_to_float(style_row.get("胜率")), _extract_float(content, "胜率")),
        avg_ret=_extract_float(content, "平均收益"),
        median_ret=_extract_float(content, "中位收益"),
        max_drawdown=_extract_float(content, "最大回撤"),
        sharpe=_extract_float(content, r"夏普比(?:\s*\(Sharpe Ratio\))?"),
        calmar=_extract_float(content, r"卡玛比(?:\s*\(Calmar Ratio\))?"),
        total_return=_extract_float(content, "组合总收益"),
        cash_initial=_extract_float(content, "初始现金"),
        cash_final=_coalesce(_to_float(style_row.get("最终现金")), _extract_float(content, "最终现金")),
        cash_total_return=_coalesce(_to_float(style_row.get("总收益")), _extract_float(content, "总收益")),
        cash_trades=_coalesce(_to_int(style_row.get("成交")), _extract_int(content, "成交笔数")),
        cash_commission_total=_coalesce(_to_float(style_row.get("佣金")), _extract_float(content, "佣金合计")),
        cash_max_drawdown=_coalesce(_to_float(style_row.get("现金回撤")), _extract_float(content, "现金最大回撤")),
        wbt_sharpe=_extract_float(content, "wbt 夏普比"),
        wbt_max_drawdown=_extract_float(content, "wbt 最大回撤"),
        wbt_daily_win_rate=_extract_float(content, "wbt 日胜率"),
    )
