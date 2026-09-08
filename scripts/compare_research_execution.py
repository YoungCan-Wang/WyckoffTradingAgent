"""Compare completed A-share trade ledgers without generating signals or exits.

Cash sizing and fees use the production simulator. Daily valuation reconstructs
its filled ledger on the observed benchmark calendar, not a new trading policy.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import inspect
import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.backtest_execution import cash_mark_price_fn
from core.cash_portfolio import CashPortfolioConfig, simulate_cash_portfolio
from core.trade_friction import round_trip_cost_pct
from workflows.backtest_data import load_snapshot_benchmark, load_snapshot_hist_map


class StrictOpenMarks:
    """Exact raw opens; deliberately bypass OHLC helpers that substitute closes."""

    def __init__(self, history: dict[str, pd.DataFrame]):
        self.prices = {
            code: dict(zip(frame.date, pd.to_numeric(frame.open, errors="coerce"), strict=True))
            for code, frame in history.items()
            if "open" in frame
        }
        self.queries: set[tuple[str, date]] = set()
        self.missing: set[tuple[str, date]] = set()

    def __call__(self, code: str, day: date) -> float | None:
        self.queries.add((code, day))
        value = self.prices.get(code, {}).get(day)
        if value is not None and math.isfinite(value) and value > 0:
            return float(value)
        self.missing.add((code, day))
        return None


def entry_contract(frame: pd.DataFrame, benchmark: pd.DataFrame, opens: StrictOpenMarks) -> dict:
    days = benchmark.date.tolist()
    delayed = missing_open = mismatched_open = unknown_target = 0
    for row in frame.itertuples():
        index = bisect.bisect_right(days, row.signal_date)
        target = days[index] if index < len(days) else None
        unknown_target += int(target is None)
        delayed += int(target is not None and row.entry_date != target)
        raw_open = opens.prices.get(str(row.code), {}).get(row.entry_date)
        if raw_open is None or not math.isfinite(raw_open) or raw_open <= 0:
            missing_open += 1
        elif not math.isclose(float(row.entry_close), raw_open, rel_tol=1e-8, abs_tol=1e-8):
            mismatched_open += 1
    sources = frame.get("entry_price_source", pd.Series("unknown", index=frame.index)).fillna("unknown")
    return {
        "trade_observations": len(frame),
        "entry_not_on_next_benchmark_day": delayed,
        "unknown_next_benchmark_day": unknown_target,
        "missing_raw_entry_open": missing_open,
        "entry_price_mismatches_raw_open": mismatched_open,
        "entry_price_source_counts": {str(key): int(value) for key, value in sources.value_counts().items()},
        "strict_next_open_contract_satisfied": not (delayed or missing_open or mismatched_open or unknown_target),
    }


def load_trades(path: Path, start: date, end: date) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype={"code": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    required = {"code", "entry_date", "exit_date", "signal_date", "entry_close", "exit_close", "regime"}
    if not required <= set(frame):
        raise ValueError(f"Missing ledger columns: {sorted(required - set(frame))}")
    for column in ("entry_date", "exit_date", "signal_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.date
    for column in ("entry_close", "exit_close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    invalid = frame[list(required)].isna().any(axis=1)
    for column in ("entry_close", "exit_close"):
        invalid |= ~frame[column].map(lambda value: math.isfinite(value) and value > 0)
    if invalid.any():
        raise ValueError(f"Unusable trade rows: {int(invalid.sum())}; no silent exclusions")
    if frame.duplicated(["signal_date", "code", "entry_date", "exit_date"]).any():
        raise ValueError("Duplicate trade identities")
    if not frame.empty and (
        (frame.signal_date < start).any()
        or (frame.exit_date > end).any()
        or (frame.entry_date <= frame.signal_date).any()
        or (frame.exit_date <= frame.entry_date).any()
    ):
        raise ValueError("Trade dates violate the evaluation window or next-day/T+1 execution")
    return frame


def validate_benchmark(benchmark: pd.DataFrame | None, start: date, end: date) -> pd.DataFrame:
    if benchmark is None or benchmark.empty:
        raise ValueError("Primary benchmark and its calendar are required")
    if benchmark.date.duplicated().any():
        raise ValueError("Duplicate benchmark dates")
    for column in ("open", "close"):
        if (
            column not in benchmark
            or not benchmark[column].map(lambda value: pd.notna(value) and math.isfinite(value) and value > 0).all()
        ):
            raise ValueError(f"Incomplete benchmark {column} prices")
    if not (benchmark.date < start).any() or not benchmark.date.between(start, end).any():
        raise ValueError("Benchmark needs a preceding close and in-window dates")
    return benchmark.sort_values("date").reset_index(drop=True)


def validate_cash_inputs(frame: pd.DataFrame, style: str) -> None:
    if frame.empty:
        return
    if "entry_weight_multiplier" in frame:
        weights = pd.to_numeric(frame.entry_weight_multiplier, errors="coerce")
        if not weights.map(lambda value: math.isfinite(value) and 0 <= value <= 1).all():
            raise ValueError("entry_weight_multiplier must be finite and within [0,1]")
    if style == "confirmation_only":
        if (
            "signal_confirmed" not in frame
            or not frame.signal_confirmed.map(
                lambda value: str(value).strip().lower() in {"true", "false", "1", "0"}
            ).all()
        ):
            raise ValueError("confirmation_only requires known signal_confirmed values")


def full_calendar_nav(closed: pd.DataFrame, days: list[date], mark, initial: float) -> pd.DataFrame:
    rows = []
    latest_prices: dict[str, float] = {}
    for day in days:
        if closed.empty:
            rows.append({"date": day, "equity": initial, "cash": initial, "missing_marks": 0})
            continue
        opened = closed[closed.entry_date <= day]
        exited = closed[closed.exit_date <= day]
        active = opened[opened.exit_date > day]
        cash = float(initial - opened.cost_total.sum() + (exited.sell_gross - exited.sell_fee).sum())
        equity, missing = cash, 0
        for row in active.itertuples():
            code = str(row.code)
            quote = mark(code, day)
            if quote is not None and math.isfinite(quote) and quote > 0:
                latest_prices[code] = quote
            else:
                missing += 1
            equity += float(row.shares) * latest_prices.get(code, float(row.entry_market_price))
        rows.append({"date": day, "equity": equity, "cash": cash, "missing_marks": missing})
    return pd.DataFrame(rows)


def reconcile_account(closed, event_nav, daily_nav, summary, initial: float) -> None:
    final = float(summary["cash_portfolio_final_cash"])
    ledger_cash = initial + (float(closed.pnl.sum()) if not closed.empty else 0.0)
    if abs(ledger_cash - final) > 0.01 or abs(float(daily_nav.equity.iloc[-1]) - final) > 0.01:
        raise ValueError("Filled ledger, final cash and daily NAV do not reconcile")
    if not event_nav.empty:
        aligned = event_nav.merge(daily_nav, on="date", suffixes=("_event", "_daily"), validate="one_to_one")
        if len(aligned) != len(event_nav) or (aligned.cash_event - aligned.cash_daily).abs().max() > 0.01:
            raise ValueError("Event cash and daily ledger cash do not reconcile")
        complete = aligned[aligned.missing_marks == 0]
        if (complete.equity_event - complete.equity_daily).abs().gt(0.01).any():
            raise ValueError("Event equity and fully priced daily equity do not reconcile")


def add_periods(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    frame = frame.copy()
    dates = pd.to_datetime(frame[column])
    frame["year"] = dates.dt.year.astype(str)
    frame["half_year"] = frame.year + "H" + ((dates.dt.month - 1) // 6 + 1).astype(str)
    frame["month"] = dates.dt.strftime("%Y-%m")
    return frame


def trade_group(frame: pd.DataFrame) -> dict:
    matched = frame[frame.excess_pct.notna()]
    daily = frame.groupby("signal_date").net_pct.mean()
    return {
        "trade_observations": len(frame),
        "unique_codes": int(frame.code.nunique()),
        "signal_days": len(daily),
        "daily_equal_weight_net_pct": float(daily.mean()),
        "trade_weight_net_pct": float(frame.net_pct.mean()),
        "stock_win_pct": float((frame.net_pct > 0).mean() * 100),
        "matched_benchmark_observations": len(matched),
        "missing_benchmark_observations": len(frame) - len(matched),
        "daily_equal_weight_excess_pct": float(matched.groupby("signal_date").excess_pct.mean().mean())
        if len(matched)
        else None,
    }


def trade_periods(frame: pd.DataFrame, benchmark: pd.DataFrame) -> dict:
    if frame.empty:
        return {"status": "no_trades", "trade_observations": 0}
    frame = add_periods(frame, "signal_date")
    indexed = benchmark.set_index("date")
    frame["net_pct"] = (frame.exit_close / frame.entry_close - 1.0) * 100 - round_trip_cost_pct()
    frame["excess_pct"] = (
        frame.net_pct - (frame.exit_date.map(indexed.close) / frame.entry_date.map(indexed.open) - 1.0) * 100
    )
    return {
        "aggregate": trade_group(frame),
        **{
            key: {str(label): trade_group(group) for label, group in frame.groupby(key)}
            for key in ("year", "half_year", "month", "regime")
        },
    }


def nav_group(group: pd.DataFrame) -> dict:
    start, end = float(group.previous_equity.iloc[0]), float(group.equity.iloc[-1])
    values = pd.Series([start, *group.equity.tolist()])
    ret = (end / start - 1) * 100
    bench_ret = (float(group.benchmark_close.iloc[-1]) / float(group.benchmark_previous_close.iloc[0]) - 1) * 100
    return {
        "return_pct": ret,
        "max_drawdown_pct": float((values / values.cummax() - 1).min() * 100),
        "benchmark_return_pct": bench_ret,
        "excess_pct": ret - bench_ret,
        "missing_mark_observations": int(group.missing_marks.sum()),
        "observed_calendar_days": len(group),
    }


def cash_periods(nav: pd.DataFrame, benchmark: pd.DataFrame, initial: float) -> dict:
    nav = add_periods(nav, "date")
    nav["previous_equity"] = nav.equity.shift(1).fillna(initial)
    indexed = benchmark.set_index("date")
    nav["benchmark_close"] = nav.date.map(indexed.close)
    nav["benchmark_previous_close"] = nav.date.map(indexed.close.shift(1))
    if nav[["benchmark_close", "benchmark_previous_close"]].isna().any().any():
        raise ValueError("Incomplete daily benchmark joins")
    return {
        "aggregate": nav_group(nav),
        **{
            key: {str(label): nav_group(group) for label, group in nav.groupby(key)}
            for key in ("year", "half_year", "month")
        },
    }


def evaluate_arm(frame, history, benchmark, config, start, end, output_dir: Path) -> dict:
    validate_cash_inputs(frame, config.portfolio_style)
    days = benchmark.loc[benchmark.date.between(start, end), "date"].tolist()
    if not frame.empty and not set(frame.entry_date).union(frame.exit_date) <= set(days):
        raise ValueError("Trade dates missing from benchmark calendar")
    mark = cash_mark_price_fn(history, {})
    opens = StrictOpenMarks(history)
    entry_audit = entry_contract(frame, benchmark, opens)
    closed, event_nav, summary = simulate_cash_portfolio(frame, config, mark_price_fn=mark, entry_mark_price_fn=opens)
    daily_nav = full_calendar_nav(closed, days, mark, config.initial_cash)
    reconcile_account(closed, event_nav, daily_nav, summary, config.initial_cash)
    diagnostic = trade_periods(frame, benchmark)
    missing_benchmark = diagnostic.get("aggregate", {}).get("missing_benchmark_observations", 0)
    complete = not (
        daily_nav.missing_marks.any()
        or missing_benchmark
        or opens.missing
        or entry_audit["missing_raw_entry_open"]
        or entry_audit["entry_price_mismatches_raw_open"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    closed.to_csv(output_dir / "cash_trades.csv", index=False)
    daily_nav.to_csv(output_dir / "daily_nav.csv", index=False)
    return {
        "accounting_reconciled": True,
        "observed_calendar_price_coverage_complete": bool(complete),
        "entry_contract": entry_audit,
        "cash_entry_open_mark_pairs": len(opens.queries),
        "cash_missing_entry_open_mark_pairs": len(opens.missing),
        "cash": summary,
        "cash_periods": cash_periods(daily_nav, benchmark, config.initial_cash),
        "pre_cash_signal_diagnostics": diagnostic,
        "filled_exposure": [
            {
                "key": f"{row.signal_date}:{row.code}:{row.entry_date}:{row.exit_date}",
                "shares": int(row.shares),
                "cost_total": float(row.cost_total),
            }
            for row in closed.itertuples()
        ],
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_run_contract(path: Path | None, hashes: dict, start: date, end: date, actual_inputs: dict) -> bool:
    if path is None:
        return False
    contracts = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "start",
        "end",
        "hold_days",
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "exit_mode",
        "sltp_priority",
        "execution_regime_gate",
        "pending_mode",
        "entry_price_mode",
        "top_n",
        "board",
        "sample_size",
        "trading_days",
        "snapshot_sha256",
        "universe_sha256",
        "benchmark_sha256",
        "metadata_mode",
        "policy_env",
        "buy_friction_pct",
        "sell_friction_pct",
    }
    for name in ("base", "candidate"):
        contract = contracts.get(name, {})
        settings = contract.get("settings", {})
        if not required <= settings.keys() or not contract.get("source_revision"):
            raise ValueError(f"Incomplete {name} run contract")
        if contract.get("trades_sha256") != hashes[name]:
            raise ValueError(f"Run contract is not bound to {name} trade ledger")
        if settings["start"] != start.isoformat() or settings["end"] != end.isoformat():
            raise ValueError("Run contract window differs from comparison window")
        if any(settings[key] != value for key, value in actual_inputs.items()):
            raise ValueError("Run contract snapshot/benchmark/universe differs from actual comparison inputs")
    if contracts["base"]["settings"] != contracts["candidate"]["settings"]:
        raise ValueError("Base and candidate run settings are not comparable")
    return True


def compare(args: argparse.Namespace) -> dict:
    if (args.output_dir / "comparison.json").exists():
        raise ValueError("Completed output already exists; choose a new output directory")
    benchmark = validate_benchmark(load_snapshot_benchmark(args.snapshot_dir), args.start, args.end)
    paths = {"base": args.base_trades, "candidate": args.candidate_trades}
    frames = {name: load_trades(path, args.start, args.end) for name, path in paths.items()}
    hashes = {name: file_sha256(path) for name, path in paths.items()}
    snapshot_files = {path.name: file_sha256(path) for path in sorted(args.snapshot_dir.iterdir()) if path.is_file()}
    actual_inputs = {
        "snapshot_sha256": hashlib.sha256(json.dumps(snapshot_files, sort_keys=True).encode()).hexdigest(),
        "benchmark_sha256": snapshot_files.get("benchmark_main.csv"),
        "universe_sha256": snapshot_files.get("name_map.json"),
    }
    comparable = verify_run_contract(args.run_contract, hashes, args.start, args.end, actual_inputs)
    symbols = set().union(*(set(frame.code) for frame in frames.values() if not frame.empty))
    history, _ = load_snapshot_hist_map(args.snapshot_dir, symbols_filter=symbols) if symbols else ({}, 0)
    config = CashPortfolioConfig(
        initial_cash=args.initial_cash,
        portfolio_style=args.style,
        buy_friction_pct=args.slippage_pct,
        sell_friction_pct=args.slippage_pct,
        cash_timing_mode="open_before_exit",
    )
    arms = {
        name: evaluate_arm(frame, history, benchmark, config, args.start, args.end, args.output_dir / name)
        for name, frame in frames.items()
    }
    base, candidate = (arms[name]["cash_periods"]["aggregate"] for name in ("base", "candidate"))
    fills = {name: {row["key"]: row for row in arms[name]["filled_exposure"]} for name in arms}
    shared_keys = fills["base"].keys() & fills["candidate"].keys()
    return {
        "window": [args.start.isoformat(), args.end.isoformat()],
        "input_sha256": hashes,
        "measurement_source_sha256": {
            "comparison_script": file_sha256(Path(__file__)),
            "cash_simulator": file_sha256(Path(inspect.getfile(simulate_cash_portfolio))),
        },
        "snapshot_file_sha256": snapshot_files,
        "actual_input_contract": actual_inputs,
        "cash_config": asdict(config),
        "trade_diagnostic_reference_cost_pct": round_trip_cost_pct(),
        "trade_diagnostic_reference_notional_yuan": 20000,
        "arms": arms,
        "cash_return_delta_pct": candidate["return_pct"] - base["return_pct"],
        "daily_drawdown_delta_pct": candidate["max_drawdown_pct"] - base["max_drawdown_pct"],
        "comparability_verified": comparable,
        "measurement_complete": comparable
        and all(arm["observed_calendar_price_coverage_complete"] for arm in arms.values()),
        "strict_next_open_contract_satisfied": all(
            arm["entry_contract"]["strict_next_open_contract_satisfied"] for arm in arms.values()
        ),
        "filled_exposure_difference": {
            "base_only_keys": sorted(fills["base"].keys() - fills["candidate"].keys()),
            "candidate_only_keys": sorted(fills["candidate"].keys() - fills["base"].keys()),
            "shared_keys": len(shared_keys),
            "shared_keys_with_different_shares": sorted(
                key for key in shared_keys if fills["base"][key]["shares"] != fills["candidate"][key]["shares"]
            ),
        },
        "strategy_acceptance": "not_established_by_accounting_comparison",
        "limitations": [
            "No signals or exits are regenerated; the input ledgers must be completed compatible production runs.",
            "Observed calendar comes from the snapshot benchmark; independent exchange-calendar completeness is not certified.",
            "Missing marks use last observed or entry price only for provisional valuation and prevent measurement_complete.",
            "Cash fees include actual commission/minimum, stamp duty and transfer fees plus specified slippage.",
            "Trade diagnostics deduct a fixed reference cost; account NAV uses actual per-fill fees.",
            "Open-before-exit cash sizing uses raw opens; same-day scheduled exit proceeds and slots cannot fund that open.",
            "Input production entries may defer across up to five available bars or fall back to close; entry_contract exposes this without deleting trades.",
            "Positive aggregate differences alone do not establish cross-regime, PIT, or live-policy validity.",
            "Run contracts are ledger-bound provenance assertions; absent contracts prevent comparability_verified.",
            "snapshot_sha256 binds the complete filename-to-SHA256 map; universe_sha256 binds snapshot name_map.json.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base-trades", "candidate-trades", "snapshot-dir", "output-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--style", choices=("confirmation_only", "slot_equal_4"), required=True)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--slippage-pct", type=float, default=0.05, help="Per side, excluding actual account fees")
    parser.add_argument("--run-contract", type=Path, help="Optional ledger-bound base/candidate settings JSON")
    args = parser.parse_args()
    if args.start > args.end or not math.isfinite(args.initial_cash) or args.initial_cash <= 0:
        parser.error("Invalid window or initial cash")
    if not math.isfinite(args.slippage_pct) or not 0 <= args.slippage_pct < 100:
        parser.error("Invalid per-side slippage")
    result = compare(args)
    destination = args.output_dir / "comparison.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
