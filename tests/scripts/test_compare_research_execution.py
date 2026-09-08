import json
from argparse import Namespace
from datetime import date

import pandas as pd
import pytest

from core.cash_portfolio import CashPortfolioConfig
from scripts import compare_research_execution as mod

DAYS = [date(2025, 1, day) for day in (2, 3, 6)]


def _benchmark():
    return pd.DataFrame({"date": [date(2024, 12, 31), *DAYS], "open": [100.0] * 4, "close": [100.0] * 4})


def _ledger():
    return pd.DataFrame(
        [
            {
                "code": "000001",
                "signal_date": date(2025, 1, 1),
                "entry_date": DAYS[0],
                "exit_date": DAYS[2],
                "entry_close": 10.0,
                "exit_close": 11.0,
                "regime": "RISK_ON",
                "signal_confirmed": True,
            }
        ]
    )


def test_full_calendar_includes_non_event_drawdown():
    closed = pd.DataFrame(
        [
            {
                "code": "000001",
                "entry_date": DAYS[0],
                "exit_date": DAYS[2],
                "shares": 10,
                "cost_total": 101.0,
                "sell_gross": 111.0,
                "sell_fee": 1.0,
                "entry_market_price": 10.0,
            }
        ]
    )
    quotes = dict(zip(DAYS, (10.0, 8.0, 11.0), strict=True))
    nav = mod.full_calendar_nav(closed, DAYS, lambda code, day: quotes[day], 1000)
    assert nav.equity.tolist() == [999.0, 979.0, 1009.0]
    report = mod.cash_periods(nav, _benchmark(), 1000)
    assert report["aggregate"]["return_pct"] == pytest.approx(0.9)
    assert report["aggregate"]["max_drawdown_pct"] == pytest.approx(-2.1)


def test_production_simulator_reconciles_and_uses_actual_fees(tmp_path):
    history = {
        "000001": pd.DataFrame(
            {"date": DAYS, "open": [10, 8, 11], "high": [10, 8, 11], "low": [10, 8, 11], "close": [10, 8, 11]}
        )
    }
    config = CashPortfolioConfig(buy_friction_pct=0.05, sell_friction_pct=0.05)
    report = mod.evaluate_arm(_ledger(), history, _benchmark(), config, DAYS[0], DAYS[-1], tmp_path)
    assert report["accounting_reconciled"]
    assert report["observed_calendar_price_coverage_complete"]
    assert report["cash"]["cash_portfolio_trade_cost_total"] > 0
    assert report["cash"]["cash_portfolio_friction_total"] > 0
    nav = pd.read_csv(tmp_path / "daily_nav.csv")
    assert len(nav) == 3
    assert nav.equity.iloc[-1] == pytest.approx(report["cash"]["cash_portfolio_final_cash"])
    assert report["cash_periods"]["aggregate"]["max_drawdown_pct"] < -4


def test_missing_held_day_prevents_complete_measurement(tmp_path):
    history = {
        "000001": pd.DataFrame(
            {"date": [DAYS[0], DAYS[2]], "open": [10, 11], "high": [10, 11], "low": [10, 11], "close": [10, 11]}
        )
    }
    report = mod.evaluate_arm(_ledger(), history, _benchmark(), CashPortfolioConfig(), DAYS[0], DAYS[-1], tmp_path)
    assert not report["observed_calendar_price_coverage_complete"]
    assert report["cash_periods"]["aggregate"]["missing_mark_observations"] == 1


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), 0, -1])
def test_rejects_unusable_trade_prices(tmp_path, bad_price):
    frame = _ledger()
    frame["entry_close"] = bad_price
    path = tmp_path / "trades.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="Unusable"):
        mod.load_trades(path, date(2025, 1, 1), DAYS[-1])


def test_rejects_incomplete_benchmark_and_duplicated_trades(tmp_path):
    bench = _benchmark()
    bench.loc[1, "open"] = float("nan")
    with pytest.raises(ValueError, match="Incomplete benchmark"):
        mod.validate_benchmark(bench, DAYS[0], DAYS[-1])
    path = tmp_path / "duplicate.csv"
    pd.concat([_ledger(), _ledger()]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="Duplicate trade"):
        mod.load_trades(path, date(2025, 1, 1), DAYS[-1])


def test_reconciliation_rejects_mismatched_cash():
    nav = pd.DataFrame({"date": DAYS, "equity": [1000.0] * 3, "cash": [1000.0] * 3, "missing_marks": [0] * 3})
    with pytest.raises(ValueError, match="do not reconcile"):
        mod.reconcile_account(pd.DataFrame(), pd.DataFrame(), nav, {"cash_portfolio_final_cash": 999.0}, 1000)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, "unknown"])
def test_rejects_invalid_cash_weight(value):
    frame = _ledger().assign(entry_weight_multiplier=value)
    with pytest.raises(ValueError, match="entry_weight_multiplier"):
        mod.validate_cash_inputs(frame, "slot_equal_4")


def test_confirmation_requires_known_confirmation_state():
    with pytest.raises(ValueError, match="known signal_confirmed"):
        mod.validate_cash_inputs(_ledger().drop(columns="signal_confirmed"), "confirmation_only")
    with pytest.raises(ValueError, match="known signal_confirmed"):
        mod.validate_cash_inputs(_ledger().assign(signal_confirmed="unknown"), "confirmation_only")
    mod.validate_cash_inputs(_ledger().assign(signal_confirmed=False), "confirmation_only")


def test_unproven_run_comparability_stays_false(tmp_path):
    assert not mod.verify_run_contract(None, {"base": "a", "candidate": "b"}, DAYS[0], DAYS[-1], {})
    path = tmp_path / "incomplete.json"
    path.write_text('{"base": {}, "candidate": {}}')
    with pytest.raises(ValueError, match="Incomplete base"):
        mod.verify_run_contract(path, {"base": "a", "candidate": "b"}, DAYS[0], DAYS[-1], {})


def test_run_contract_rejects_mismatched_settings_or_ledger(tmp_path):
    settings = {
        "start": DAYS[0].isoformat(),
        "end": DAYS[-1].isoformat(),
        "hold_days": 5,
        "stop_loss_pct": -5,
        "take_profit_pct": 0,
        "trailing_stop_pct": 0,
        "exit_mode": "sltp",
        "sltp_priority": "stop_first",
        "execution_regime_gate": "live",
        "pending_mode": "off",
        "entry_price_mode": "open",
        "top_n": 0,
        "board": "all",
        "sample_size": 0,
        "trading_days": 320,
        "snapshot_sha256": "snapshot",
        "universe_sha256": "universe",
        "benchmark_sha256": "benchmark",
        "metadata_mode": "static_no_heat",
        "policy_env": {},
        "buy_friction_pct": 0.026,
        "sell_friction_pct": 0.176,
    }
    contracts = {
        name: {"settings": dict(settings), "source_revision": name, "trades_sha256": name}
        for name in ("base", "candidate")
    }
    hashes = {name: name for name in contracts}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contracts))
    actual = {key: settings[key] for key in ("snapshot_sha256", "benchmark_sha256", "universe_sha256")}
    assert mod.verify_run_contract(path, hashes, DAYS[0], DAYS[-1], actual)
    with pytest.raises(ValueError, match="differs from actual"):
        mod.verify_run_contract(path, hashes, DAYS[0], DAYS[-1], {**actual, "benchmark_sha256": "wrong"})
    contracts["candidate"]["settings"]["hold_days"] = 10
    path.write_text(json.dumps(contracts))
    with pytest.raises(ValueError, match="not comparable"):
        mod.verify_run_contract(path, hashes, DAYS[0], DAYS[-1], actual)
    with pytest.raises(ValueError, match="not bound"):
        mod.verify_run_contract(path, {**hashes, "candidate": "wrong"}, DAYS[0], DAYS[-1], actual)


def test_complete_comparison_is_serializable_and_never_infers_strategy_acceptance(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _benchmark().to_csv(snapshot / "benchmark_main.csv", index=False)
    pd.DataFrame(
        {
            "symbol": ["000001"] * 3,
            "date": DAYS,
            "open": [10, 8, 11],
            "high": [10, 8, 11],
            "low": [10, 8, 11],
            "close": [10, 8, 11],
        }
    ).to_csv(snapshot / "hist_full.csv.gz", index=False)
    (snapshot / "name_map.json").write_text('{"000001":"Test"}')
    path = tmp_path / "trades.csv"
    _ledger().to_csv(path, index=False)
    result = mod.compare(
        Namespace(
            snapshot_dir=snapshot,
            start=date(2025, 1, 1),
            end=DAYS[-1],
            base_trades=path,
            candidate_trades=path,
            initial_cash=100000,
            style="slot_equal_4",
            slippage_pct=0.05,
            run_contract=None,
            output_dir=tmp_path / "result",
        )
    )
    json.dumps(result, allow_nan=False)
    assert result["cash_return_delta_pct"] == 0
    assert not result["comparability_verified"]
    assert not result["measurement_complete"]
    assert result["strategy_acceptance"] == "not_established_by_accounting_comparison"
    assert result["filled_exposure_difference"]["shared_keys"] == 1


def test_failed_contract_writes_no_output(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_snapshot_benchmark", lambda path: _benchmark())
    ledger = tmp_path / "trades.csv"
    _ledger().to_csv(ledger, index=False)
    contract = tmp_path / "invalid.json"
    contract.write_text("{}")
    output = tmp_path / "output"
    args = Namespace(
        snapshot_dir=tmp_path,
        start=date(2025, 1, 1),
        end=DAYS[-1],
        base_trades=ledger,
        candidate_trades=ledger,
        run_contract=contract,
        output_dir=output,
    )
    with pytest.raises(ValueError, match="Incomplete base"):
        mod.compare(args)
    assert not output.exists()


def test_completed_output_is_not_overwritten(tmp_path):
    path = tmp_path / "comparison.json"
    path.write_text('{"previous": "completed"}')
    with pytest.raises(ValueError, match="Completed output already exists"):
        mod.compare(Namespace(output_dir=tmp_path))
    assert path.read_text() == '{"previous": "completed"}'
