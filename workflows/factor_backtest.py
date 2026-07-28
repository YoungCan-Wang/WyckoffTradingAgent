"""Orchestrate the cross-sectional factor portfolio backtest end to end."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from core.backtest_metrics import calc_nav_max_drawdown_pct, calc_sharpe_ratio
from core.factor_portfolio import (
    FactorPortfolioConfig,
    build_matrices,
    equal_weight_benchmark,
    holding_period_benchmark,
    run_factor_backtest,
)
from core.factor_scores import apply_universe_filters, prepare_panel
from integrations.factor_panel import load_panel, st_flags

logger = logging.getLogger(__name__)

PANEL_COLUMNS = [
    "date", "symbol", "open", "close", "pre_close", "vol", "amount",
    "adj_factor", "pb", "pe_ttm", "ps_ttm", "dv_ttm", "circ_mv", "turnover_rate_f",
]  # fmt: skip
TRADING_DAYS_PER_YEAR = 244.0
# 引擎与基准只用得上这几列。八年全市场面板约一千万行，把因子原始列一路带到 pivot 会白占几个 GB。
ENGINE_COLUMNS = ["date", "symbol", "close_adj", "open_adj", "score", "can_buy", "can_sell"]


def _list_dates(panel_dir: Path) -> pd.DataFrame | None:
    path = panel_dir / "universe.parquet"
    if not path.exists():
        return None
    universe = pd.read_parquet(path, columns=["symbol", "list_date"])
    out = pd.DataFrame(
        {
            "symbol": universe["symbol"].astype("int32"),
            "list_date": pd.to_datetime(universe["list_date"], format="%Y%m%d", errors="coerce"),
        }
    )
    return out.dropna(subset=["list_date"]).drop_duplicates("symbol")


def load_prepared_panel(
    panel_dir: Path,
    *,
    start: str,
    end: str,
    exclude_st: bool,
    min_amount_thousand: float,
    min_listed_days: int,
) -> pd.DataFrame:
    panel = load_panel(panel_dir, columns=PANEL_COLUMNS)
    panel = panel[panel["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    if panel.empty:
        raise ValueError(f"面板在 {start}~{end} 内为空")
    name_history = panel_dir / "name_history.parquet"
    st = st_flags(pd.read_parquet(name_history), panel["date"]) if name_history.exists() else None
    prepared = prepare_panel(panel, st, _list_dates(panel_dir))
    filtered = apply_universe_filters(
        prepared,
        exclude_st=exclude_st,
        min_amount_thousand=min_amount_thousand,
        min_listed_days=min_listed_days,
    )
    return filtered[ENGINE_COLUMNS]


def _annualised(nav: pd.Series) -> float:
    years = len(nav) / TRADING_DAYS_PER_YEAR
    return float((nav.iloc[-1] / nav.iloc[0]) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0


def summarise(result: dict, benchmark: pd.Series, cfg: FactorPortfolioConfig) -> dict:
    raw_nav = result["nav"]
    nav = raw_nav / raw_nav.iloc[0]
    bench = benchmark.reindex(nav.index).ffill()
    bench = bench / bench.iloc[0]
    daily = nav.pct_change(fill_method=None).dropna() * 100.0
    trades = result["trades"]
    years = max(len(nav) / TRADING_DAYS_PER_YEAR, 1e-9)
    # 分母用平均净值而不是初始资金：组合在窗口里可能翻倍或腰斩，用起点会把摩擦率算歪。
    avg_equity = max(float(raw_nav.mean()), 1e-9)
    traded_value = float((trades["shares"] * trades["price"]).sum()) if not trades.empty else 0.0
    total_cost = float(trades["cost"].sum()) if not trades.empty else 0.0
    return {
        "top_n": cfg.top_n,
        "rebalance_days": cfg.rebalance_days,
        "buffer_mult": cfg.buffer_mult,
        "days": len(nav),
        "total_return_pct": float(nav.iloc[-1] - 1.0) * 100.0,
        "annual_return_pct": _annualised(nav),
        "benchmark_annual_pct": _annualised(bench),
        "annual_alpha_pct": _annualised(nav) - _annualised(bench),
        "max_drawdown_pct": calc_nav_max_drawdown_pct(nav),
        "benchmark_max_drawdown_pct": calc_nav_max_drawdown_pct(bench),
        "sharpe_ratio": calc_sharpe_ratio(daily, periods_per_year=TRADING_DAYS_PER_YEAR),
        "avg_positions": float(result["positions"].mean()),
        "avg_cash_pct": float((result["cash"] / raw_nav).mean()) * 100.0,
        "trade_count": int(len(trades)),
        "total_cost": total_cost,
        "cost_drag_annual_pct": total_cost / avg_equity / years * 100.0,
        "annual_turnover_pct": traded_value / avg_equity / years * 100.0 / 2.0,
    }


def yearly_breakdown(nav: pd.Series, benchmark: pd.Series) -> pd.DataFrame:
    bench = benchmark.reindex(nav.index).ffill()
    rows = []
    for year, group in nav.groupby(nav.index.year):
        bench_group = bench.loc[group.index]
        rows.append(
            {
                "year": int(year),
                "return_pct": float(group.iloc[-1] / group.iloc[0] - 1.0) * 100.0,
                "benchmark_pct": float(bench_group.iloc[-1] / bench_group.iloc[0] - 1.0) * 100.0,
                "max_drawdown_pct": calc_nav_max_drawdown_pct(group / group.iloc[0]),
            }
        )
    out = pd.DataFrame(rows)
    out["alpha_pct"] = out["return_pct"] - out["benchmark_pct"]
    return out


def run_sweep(
    panel: pd.DataFrame,
    configs: list[FactorPortfolioConfig],
    *,
    progress: Callable[[str], None] = logger.info,
) -> tuple[pd.DataFrame, dict[str, dict]]:
    matrices = build_matrices(panel)
    benchmarks = {k: holding_period_benchmark(matrices, k) for k in sorted({c.rebalance_days for c in configs})}
    rows, detail = [], {}
    for cfg in configs:
        result = run_factor_backtest(panel, cfg)
        row = summarise(result, benchmarks[cfg.rebalance_days], cfg)
        rows.append(row)
        key = f"top{cfg.top_n}_reb{cfg.rebalance_days}_buf{cfg.buffer_mult:g}"
        detail[key] = {"result": result, "summary": row, "benchmark": benchmarks[cfg.rebalance_days]}
        progress(
            f"{key}: 年化 {row['annual_return_pct']:.2f}% / 同频等权基准 {row['benchmark_annual_pct']:.2f}% / "
            f"alpha {row['annual_alpha_pct']:.2f}% / 回撤 {row['max_drawdown_pct']:.1f}% / "
            f"换手 {row['annual_turnover_pct']:.0f}% / 摩擦 {row['cost_drag_annual_pct']:.2f}%"
        )
    return pd.DataFrame(rows), {"daily_equal_weight": equal_weight_benchmark(panel), **detail}


def write_artifacts(out_dir: Path, matrix: pd.DataFrame, detail: dict, cfg: FactorPortfolioConfig) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(out_dir / "sweep_matrix.csv", index=False)
    best_key = _best_key(matrix, detail)
    best = detail[best_key]
    benchmark = best["benchmark"]
    nav = best["result"]["nav"] / best["result"]["nav"].iloc[0]
    daily_ew = detail["daily_equal_weight"]
    pd.DataFrame(
        {
            "nav": nav,
            "benchmark": (benchmark / benchmark.iloc[0]).reindex(nav.index).ffill(),
            "daily_equal_weight": (daily_ew / daily_ew.iloc[0]).reindex(nav.index).ffill(),
        }
    ).to_csv(out_dir / "nav.csv")
    best["result"]["trades"].to_csv(out_dir / "trades.csv", index=False)
    yearly = yearly_breakdown(nav, benchmark)
    yearly.to_csv(out_dir / "yearly.csv", index=False)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "best": best_key,
                "config": _config_payload(cfg),
                "summary": best["summary"],
                "sweep": matrix.to_dict("records"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(_markdown(matrix, best_key, best["summary"], yearly), encoding="utf-8")
    return out_dir


def _config_payload(cfg: FactorPortfolioConfig) -> dict:
    payload = asdict(replace(cfg))
    payload["costs"] = asdict(cfg.costs)
    return payload


def _best_key(matrix: pd.DataFrame, detail: dict) -> str:
    ranked = matrix.sort_values("annual_alpha_pct", ascending=False).iloc[0]
    return f"top{int(ranked['top_n'])}_reb{int(ranked['rebalance_days'])}_buf{ranked['buffer_mult']:g}"


def _fmt(value: float | None, ndigits: int = 2) -> str:
    return "-" if value is None or pd.isna(value) else f"{value:.{ndigits}f}"


def _markdown(matrix: pd.DataFrame, best_key: str, best: dict, yearly: pd.DataFrame) -> str:
    lines = [
        "# 截面价值组合回测",
        "",
        f"- 最优格: `{best_key}`",
        f"- 年化 {_fmt(best['annual_return_pct'])}%，等权基准 {_fmt(best['benchmark_annual_pct'])}%，"
        f"alpha {_fmt(best['annual_alpha_pct'])}%",
        f"- 最大回撤 {_fmt(best['max_drawdown_pct'], 1)}%（基准 {_fmt(best['benchmark_max_drawdown_pct'], 1)}%），"
        f"Sharpe {_fmt(best['sharpe_ratio'])}",
        f"- 年换手 {_fmt(best['annual_turnover_pct'], 0)}%，摩擦拖累 {_fmt(best['cost_drag_annual_pct'])}%/年，"
        f"累计费用 {_fmt(best['total_cost'], 0)} 元",
        "",
        "## 参数网格",
        "",
        "| 持仓 | 再平衡 | 年化(%) | 基准(%) | alpha(%) | 回撤(%) | Sharpe | 年换手(%) | 摩擦(%/年) | 均持仓 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *(
            f"| {int(r.top_n)} | {int(r.rebalance_days)} | {_fmt(r.annual_return_pct)} "
            f"| {_fmt(r.benchmark_annual_pct)} | {_fmt(r.annual_alpha_pct)} | {_fmt(r.max_drawdown_pct, 1)} "
            f"| {_fmt(r.sharpe_ratio)} | {_fmt(r.annual_turnover_pct, 0)} | {_fmt(r.cost_drag_annual_pct)} "
            f"| {_fmt(r.avg_positions, 1)} |"
            for r in matrix.itertuples()
        ),
        "",
        "## 分年度",
        "",
        "| 年份 | 组合(%) | 基准(%) | alpha(%) | 回撤(%) |",
        "|---:|---:|---:|---:|---:|",
        *(
            f"| {int(r.year)} | {_fmt(r.return_pct)} | {_fmt(r.benchmark_pct)} | {_fmt(r.alpha_pct)} "
            f"| {_fmt(r.max_drawdown_pct, 1)} |"
            for r in yearly.itertuples()
        ),
    ]
    return "\n".join(lines) + "\n"
