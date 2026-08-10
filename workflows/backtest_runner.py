"""Backtest runner workflow orchestration."""

from __future__ import annotations

import logging
from argparse import Namespace
from dataclasses import dataclass, replace
from pathlib import Path

from core.backtest_run import parse_date
from workflows.backtest import BacktestWorkflowRequest, run_backtest_request, run_backtest_request_suite
from workflows.backtest_artifacts import (
    backtest_stamp,
    error_suite_row,
    success_suite_row,
    write_backtest_artifacts,
    write_suite_summary,
)
from workflows.backtest_cli import parse_hold_days_list
from workflows.backtest_defaults import FUNNEL_AI_SELECTION_MODE
from workflows.backtest_strategy_variants import normalize_strategy_variant
from workflows.backtest_trigger_matrix import (
    SELECTION_PARAM,
    TriggerGrid,
    build_matrix_row,
    parse_trigger_grid,
    trigger_value_dir,
    write_matrix_artifacts,
)

logger = logging.getLogger(__name__)


def run_backtest_runner(args, progress=None) -> int:
    if progress is None:
        from utils.progress import report_progress

        progress = report_progress
    start_dt = parse_date(args.start)
    end_dt = parse_date(args.end)
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    trigger_grid = parse_trigger_grid(getattr(args, "trigger_grid", ""))
    if trigger_grid:
        return _run_trigger_matrix(args, start_dt, end_dt, out_dir, trigger_grid, progress)
    grid_cells = parse_grid_cells(getattr(args, "grid_cells", ""))
    if grid_cells:
        return _run_grid_suite(args, start_dt, end_dt, out_dir, grid_cells, progress)
    strategy_variants = parse_strategy_variants(getattr(args, "strategy_variants", ""))
    if strategy_variants:
        return _run_strategy_suite(args, start_dt, end_dt, out_dir, strategy_variants, progress)
    hold_days_list = _hold_days_list(args)

    suite_rows: list[dict] = []
    success_count = 0
    last_error: Exception | None = None
    for hold_days in hold_days_list:
        try:
            row = run_one_hold_days(args, start_dt, end_dt, out_dir, hold_days, progress)
        except Exception as exc:
            last_error = exc
            logger.error("hold_days=%d 失败: %s", hold_days, exc, exc_info=True)
            suite_rows.append(error_suite_row(hold_days, str(exc)))
            continue
        success_count += 1
        suite_rows.append(row)

    if success_count == 0:
        raise RuntimeError("多周期回测全部失败，请检查日期区间、快照覆盖范围或 TUSHARE_TOKEN。") from last_error

    write_suite_summary(
        out_dir=out_dir,
        start_dt=start_dt,
        end_dt=end_dt,
        suite_rows=suite_rows,
        success_count=success_count,
        candidate_mode=FUNNEL_AI_SELECTION_MODE,
    )
    return 0


@dataclass(frozen=True)
class GridCell:
    hold_days: int
    stop_loss: float
    take_profit: float
    trailing_stop: float
    # 移动止盈激活门槛（浮盈达到该百分比后才启用移动止盈）。0 = 入场即启用。
    #
    # 2026-08-10 补齐：此前 grid cell 只有 4 段，activate 固定取默认值 0，于是
    # 移动止盈从入场就生效。实测（run 31348338247，360 笔配对）该设定把 stop_loss
    # 占比从 49% 压到 31%、最大亏损 -28.89% → -18.32%，但同时截断赢家：68 单平均
    # 少赚 6.48%（最惨 -43.58%），盈利单均盈 +10.43% → +8.50%，配对 t 仅 +1.77。
    # 而 MFE 证据显示被止损的单里 18% 曾浮盈超 +7% —— 真正要测的是"先让利润跑到
    # +5~7% 再用移动止盈保住"，而不是入场就贴着价格跟。引擎早已支持该门槛
    # （core/backtest_execution.py:461），缺的只是 grid 这一层的传参。
    trailing_activate: float = 0.0


def parse_strategy_variants(raw: str) -> list[str]:
    variants = list(
        dict.fromkeys(normalize_strategy_variant(item) for item in str(raw or "").split(",") if item.strip())
    )
    if len(variants) == 1:
        raise ValueError("strategy_variants 至少需要两个策略组")
    return variants


def parse_grid_cells(raw: str) -> list[GridCell]:
    cells: list[GridCell] = []
    for item in str(raw or "").split(","):
        if not item.strip():
            continue
        parts = item.strip().split(":")
        # 第 5 段（移动止盈激活门槛）可选，省略时为 0=入场即启用，保持既有参数格兼容。
        if len(parts) not in (4, 5):
            raise ValueError(f"非法 grid cell: {item}")
        activate = float(parts[4]) if len(parts) == 5 else 0.0
        cell = GridCell(int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), activate)
        if cell.hold_days < 1 or cell.stop_loss > 0 or cell.take_profit < 0 or cell.trailing_stop > 0:
            raise ValueError(f"非法 grid cell: {item}")
        # 激活门槛是"浮盈达到多少后启用"，必须为非负；且没有移动止盈时设门槛无意义，
        # 静默接受会让参数格看起来测了某个组合、实际没测。
        if cell.trailing_activate < 0:
            raise ValueError(f"非法 grid cell（激活门槛需 >= 0）: {item}")
        if cell.trailing_activate > 0 and cell.trailing_stop == 0:
            raise ValueError(f"非法 grid cell（设了激活门槛却没有移动止盈）: {item}")
        cells.append(cell)
    return cells


def _run_grid_suite(args, start_dt, end_dt, out_dir: Path, cells: list[GridCell], progress) -> int:
    cell_args = [_args_for_grid_cell(args, cell) for cell in cells]
    requests = [request_from_args(item, start_dt, end_dt, item.hold_days) for item in cell_args]
    results = run_backtest_request_suite(requests, progress=progress)
    for item, cell, (trades_df, summary) in zip(cell_args, cells, results, strict=True):
        cell_dir = out_dir / _grid_cell_dir(getattr(args, "grid_prefix", "backtest-grid"), cell)
        cell_dir.mkdir(parents=True, exist_ok=True)
        _write_result(item, start_dt, end_dt, cell_dir, cell.hold_days, trades_df, summary)
    return 0


def _run_strategy_suite(args, start_dt, end_dt, out_dir: Path, variants: list[str], progress) -> int:
    variant_args = [Namespace(**{**vars(args), "strategy_variant": variant}) for variant in variants]
    requests = [request_from_args(item, start_dt, end_dt, item.hold_days) for item in variant_args]
    results = run_backtest_request_suite(requests, progress=progress)
    prefix = str(getattr(args, "strategy_prefix", "backtest-strategy"))
    for item, variant, (trades_df, summary) in zip(variant_args, variants, results, strict=True):
        variant_dir = out_dir / f"{prefix}-{variant}"
        variant_dir.mkdir(parents=True, exist_ok=True)
        _write_result(item, start_dt, end_dt, variant_dir, item.hold_days, trades_df, summary)
    return 0


def _run_trigger_matrix(args, start_dt, end_dt, out_dir: Path, grid: TriggerGrid, progress) -> int:
    hold_days = int(args.hold_days)
    period_key = str(getattr(args, "period_key", "") or "").strip()
    rows: list[dict] = []
    for value in grid.values:
        # top_n 是执行层参数，不在 FunnelConfig 里；其余扫描项都是漏斗检测阈值。
        selection_sweep = grid.param == SELECTION_PARAM
        value_args = Namespace(**{**vars(args), "top_n": int(value)}) if selection_sweep else args
        request = request_from_args(value_args, start_dt, end_dt, hold_days)
        if not selection_sweep:
            request = replace(request, funnel_overrides=((grid.param, value),))
        trades_df, summary = run_backtest_request(request, progress=progress)
        value_dir = out_dir / trigger_value_dir(getattr(args, "grid_prefix", "backtest-trigger"), grid.param, value)
        value_dir.mkdir(parents=True, exist_ok=True)
        _write_result(value_args, start_dt, end_dt, value_dir, hold_days, trades_df, summary)
        rows.append(
            build_matrix_row(
                grid=grid,
                value=value,
                period_key=period_key,
                start_dt=start_dt,
                end_dt=end_dt,
                summary=summary,
            )
        )
    write_matrix_artifacts(out_dir, grid, rows)
    return 0


def _args_for_grid_cell(args, cell: GridCell) -> Namespace:
    values = vars(args).copy()
    values.update(
        hold_days=cell.hold_days,
        hold_days_list="",
        stop_loss=cell.stop_loss,
        take_profit=cell.take_profit,
        trailing_stop=cell.trailing_stop,
        trailing_activate=cell.trailing_activate,
    )
    return Namespace(**values)


def _grid_cell_dir(prefix: str, cell: GridCell) -> str:
    name = f"{prefix}-h{cell.hold_days}-sl{abs(cell.stop_loss):g}-tp{cell.take_profit:g}-tr{abs(cell.trailing_stop):g}"
    # 只在设了门槛时加后缀，保持既有参数格的目录名不变（便于跨轮对比）。
    if cell.trailing_activate > 0:
        name += f"-ta{cell.trailing_activate:g}"
    return name


def run_one_hold_days(args, start_dt, end_dt, out_dir: Path, hold_days: int, progress) -> dict:
    trades_df, summary = run_backtest_request(
        request_from_args(args, start_dt, end_dt, hold_days),
        progress=progress,
    )
    return _write_result(args, start_dt, end_dt, out_dir, hold_days, trades_df, summary)


def _write_result(args, start_dt, end_dt, out_dir: Path, hold_days: int, trades_df, summary) -> dict:
    stamp = backtest_stamp(start_dt, end_dt, hold_days, args.top_n)
    artifact = write_backtest_artifacts(out_dir=out_dir, stamp=stamp, trades_df=trades_df, summary=summary)
    print(artifact.summary_md)
    print("")
    logger.info("summary -> %s", artifact.summary_path)
    logger.info("trades  -> %s", artifact.trades_path)
    return success_suite_row(hold_days, summary)


def request_from_args(args, start_dt, end_dt, hold_days: int) -> BacktestWorkflowRequest:
    return BacktestWorkflowRequest(
        start_dt=start_dt,
        end_dt=end_dt,
        hold_days=hold_days,
        top_n=args.top_n,
        board=args.board,
        sample_size=args.sample_size,
        trading_days=args.trading_days,
        max_workers=args.workers,
        snapshot_dir=Path(args.snapshot_dir).resolve() if str(args.snapshot_dir).strip() else None,
        benchmark=args.benchmark,
        exit_mode=args.exit_mode,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        trailing_stop_pct=args.trailing_stop,
        trailing_activate_pct=args.trailing_activate,
        sltp_priority=args.sltp_priority,
        use_current_meta=args.use_current_meta,
        allow_static_meta=getattr(args, "allow_static_meta", False),
        buy_friction_pct=args.buy_friction_pct,
        sell_friction_pct=args.sell_friction_pct,
        regime_filter=args.regime_filter,
        execution_regime_gate=args.execution_regime_gate,
        strategy_variant=getattr(args, "strategy_variant", "live"),
        pending_mode=args.pending_mode,
        pending_merge_order=args.pending_merge_order,
        atr_period=args.atr_period,
        atr_multiplier=args.atr_multiplier,
        atr_hard_stop_pct=args.atr_hard_stop,
        metrics_engine=args.metrics_engine,
        wbt_fee_rate=args.wbt_fee_rate,
        wbt_n_jobs=args.wbt_n_jobs,
        abc_filter=args.abc_filter,
        entry_price_mode=args.entry_price_mode,
        entry_price_time=args.entry_price_time,
        entry_price_fallback=args.entry_price_fallback,
        cash_portfolio=args.cash_portfolio,
        initial_cash=args.initial_cash,
        max_positions=args.max_positions,
        commission_rate=args.commission_rate,
        min_commission=args.min_commission,
        stamp_duty_rate=args.stamp_duty_rate,
        transfer_fee_rate=args.transfer_fee_rate,
        lot_size=args.lot_size,
        portfolio_styles=args.portfolio_styles,
    )


def _hold_days_list(args) -> list[int]:
    raw = str(args.hold_days_list or "").strip()
    return parse_hold_days_list(raw) if raw else [int(args.hold_days)]
