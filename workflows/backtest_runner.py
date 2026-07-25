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
from workflows.backtest_trigger_matrix import (
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


def parse_grid_cells(raw: str) -> list[GridCell]:
    cells: list[GridCell] = []
    for item in str(raw or "").split(","):
        if not item.strip():
            continue
        parts = item.strip().split(":")
        if len(parts) != 4:
            raise ValueError(f"非法 grid cell: {item}")
        cell = GridCell(int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
        if cell.hold_days < 1 or cell.stop_loss > 0 or cell.take_profit < 0 or cell.trailing_stop > 0:
            raise ValueError(f"非法 grid cell: {item}")
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


def _run_trigger_matrix(args, start_dt, end_dt, out_dir: Path, grid: TriggerGrid, progress) -> int:
    hold_days = int(args.hold_days)
    period_key = str(getattr(args, "period_key", "") or "").strip()
    rows: list[dict] = []
    for value in grid.values:
        request = request_from_args(args, start_dt, end_dt, hold_days)
        request = replace(request, funnel_overrides=((grid.param, value),))
        trades_df, summary = run_backtest_request(request, progress=progress)
        value_dir = out_dir / trigger_value_dir(getattr(args, "grid_prefix", "backtest-trigger"), grid.param, value)
        value_dir.mkdir(parents=True, exist_ok=True)
        _write_result(args, start_dt, end_dt, value_dir, hold_days, trades_df, summary)
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
    )
    return Namespace(**values)


def _grid_cell_dir(prefix: str, cell: GridCell) -> str:
    return f"{prefix}-h{cell.hold_days}-sl{abs(cell.stop_loss):g}-tp{cell.take_profit:g}-tr{abs(cell.trailing_stop):g}"


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
