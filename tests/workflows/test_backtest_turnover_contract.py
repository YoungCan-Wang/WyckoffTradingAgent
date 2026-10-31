import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from core.backtest_config import _live_buy_block_regimes
from core.wyckoff_engine import FunnelConfig, _evr_turnover_ok
from workflows import backtest
from workflows import backtest_turnover as turnover
from workflows.backtest_data import load_snapshot_hist_map
from workflows.backtest_snapshot_fetch import _frame_from_tickflow_batch


def test_batch_missing_turnover_stays_missing_not_zero():
    raw = pd.DataFrame(
        {
            "date": ["2026-09-30"],
            "open": [10],
            "high": [11],
            "low": [9],
            "close": [10],
            "prev_close": [10],
            "volume": [10],
            "amount": [10000],
        }
    )
    frame, error = _frame_from_tickflow_batch("000001", raw, "2026-09-01", "2026-09-30")
    assert error is None
    assert frame["turnover"].isna().all()


def _write_snapshot(path, *, contract):
    pd.DataFrame(
        {
            "symbol": ["000001"],
            "date": ["2026-09-30"],
            "close": [10],
            "turnover": [0.4],
            "turnover_source": ["tushare.daily_basic"],
        }
    ).to_csv(path / "hist_full.csv.gz", index=False, compression="gzip")
    if contract:
        (path / "metadata.json").write_text(json.dumps({"turnover_pit": {"contract": turnover.TURNOVER_CONTRACT}}))


def test_contract_snapshot_preserves_turnover_and_reaches_evr_gate(tmp_path):
    _write_snapshot(tmp_path, contract=True)
    frames, rows = load_snapshot_hist_map(tmp_path)
    assert rows == 1
    assert frames["000001"].iloc[0]["turnover"] == 0.4
    assert frames["000001"].iloc[0]["turnover_source"] == "tushare.daily_basic"
    assert not _evr_turnover_ok(frames["000001"], -1, FunnelConfig(evr_min_turnover=1.5))


def test_snapshot_without_contract_keeps_default_baseline(tmp_path):
    _write_snapshot(tmp_path, contract=False)
    frames, _ = load_snapshot_hist_map(tmp_path)
    assert "turnover" not in frames["000001"].columns
    assert "turnover_source" not in frames["000001"].columns
    assert _evr_turnover_ok(frames["000001"], -1, FunnelConfig(evr_min_turnover=1.5))


def test_pit_join_is_date_keyed_and_preserves_native(monkeypatch):
    calls = []

    def fetch(**kw):
        calls.append(kw["trade_date"])
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [kw["trade_date"]],
                "turnover_rate": [2.0 if kw["trade_date"] == "20260929" else 3.0],
            }
        )

    monkeypatch.setattr(turnover, "get_pro", lambda: SimpleNamespace(daily_basic=fetch))
    frame = pd.DataFrame(
        {"symbol": ["000001", "000001"], "date": ["2026-09-29", "2026-09-30"], "turnover": [1.0, None]}
    )
    meta = turnover.attach_snapshot_pit_turnover([frame])
    assert list(frame.turnover) == [1, 3]
    assert list(frame.turnover_source) == ["native", "tushare.daily_basic"]
    assert calls == ["20260929", "20260930"]
    assert meta["contract"] == turnover.TURNOVER_CONTRACT


def test_pit_query_rejects_wrong_date(monkeypatch):
    monkeypatch.setattr(
        turnover,
        "get_pro",
        lambda: SimpleNamespace(
            daily_basic=lambda **kw: pd.DataFrame(
                {"ts_code": ["000001.SZ"], "trade_date": ["20261009"], "turnover_rate": [1.0]}
            )
        ),
    )
    frame = pd.DataFrame({"symbol": ["000001"], "date": ["2026-09-30"]})
    with pytest.raises(ValueError, match="非请求日"):
        turnover.attach_snapshot_pit_turnover([frame])


def test_strict_guard_rejects_legacy_even_with_zero_values():
    frames = {"000001": pd.DataFrame({"date": ["2026-09-30"], "turnover": [0]})}
    with pytest.raises(ValueError, match="旧快照"):
        turnover.validate_snapshot_turnover(frames, {}, date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.parametrize("value", [None, float("inf"), -1])
def test_strict_guard_refuses_invalid_values(value):
    frames = {"000001": pd.DataFrame({"date": ["2026-09-30"], "turnover": [value]})}
    meta = {"turnover_pit": {"contract": turnover.TURNOVER_CONTRACT}}
    with pytest.raises(ValueError, match="不足95%"):
        turnover.validate_snapshot_turnover(frames, meta, date(2026, 9, 1), date(2026, 9, 30))


def test_strict_guard_accepts_real_zero_and_excludes_warmup():
    frames = {"000001": pd.DataFrame({"date": ["2026-08-01", "2026-09-30"], "turnover": [None, 0]})}
    meta = {"turnover_pit": {"contract": turnover.TURNOVER_CONTRACT}}
    assert turnover.validate_snapshot_turnover(frames, meta, date(2026, 9, 1), date(2026, 9, 30))["min_coverage"] == 1


def _prepare(monkeypatch, tmp_path, flag):
    history = SimpleNamespace(all_df_map={"000001": pd.DataFrame({"date": [date(2026, 9, 30)], "turnover": [0]})})
    monkeypatch.setattr(backtest, "load_backtest_history", lambda **kw: history)
    monkeypatch.setattr(backtest, "load_backtest_metadata", lambda *a, **kw: (_ for _ in ()).throw(LookupError("past")))
    monkeypatch.setattr(backtest, "load_snapshot_pit_meta", lambda path: {})
    monkeypatch.setenv("BACKTEST_REQUIRE_PIT_TURNOVER", flag)
    request = SimpleNamespace(
        start_dt=date(2026, 9, 1),
        end_dt=date(2026, 9, 30),
        trading_days=320,
        hold_days=15,
        benchmark="000001",
        max_workers=1,
        use_current_meta=False,
        allow_static_meta=False,
    )
    backtest._load_prepared_data(
        request, SimpleNamespace(snapshot_dir=tmp_path), SimpleNamespace(symbols=["000001"]), lambda *args: None
    )


def test_strict_replay_rejects_snapshot_without_contract(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="旧快照"):
        _prepare(monkeypatch, tmp_path, "1")


def test_default_replay_skips_turnover_guard(monkeypatch, tmp_path):
    with pytest.raises(LookupError, match="past"):
        _prepare(monkeypatch, tmp_path, "0")


def _workflow():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/backtest_grid.yml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_pit_turnover_is_opt_in():
    workflow = _workflow()
    assert workflow["on"]["workflow_dispatch"]["inputs"]["pit_turnover"]["default"] == "false"
    assert "pit_turnover" in workflow["jobs"]["fetch"]["env"]["BACKTEST_FETCH_PIT_TURNOVER"]
    assert "pit_turnover" in workflow["env"]["BACKTEST_REQUIRE_PIT_TURNOVER"]


def test_replay_jobs_share_one_policy_environment():
    workflow = _workflow()
    prefixes = ("STEP4_", "FUNNEL_")
    effective = [
        {
            key: value
            for key, value in {**workflow["env"], **workflow["jobs"][job].get("env", {})}.items()
            if key.startswith(prefixes)
        }
        for job in ["grid", "strategy_compare", "gate_baseline", "exit_baseline", "leader_baseline", "notify"]
    ]
    assert len({json.dumps(env, sort_keys=True) for env in effective}) == 1


def test_strategy_compare_market_gate_matches_production(monkeypatch):
    workflow = _workflow()
    effective = {**workflow["env"], **workflow["jobs"]["strategy_compare"].get("env", {})}
    for name in ["STEP4_BUY_BLOCK_REGIMES", "STEP4_BUY_ALLOW_REGIMES", "STEP4_BUY_PROBE_REGIMES"]:
        monkeypatch.setenv(name, effective[name].split("'")[1])
    blocked = _live_buy_block_regimes()
    assert {"NEUTRAL", "RISK_ON", "RISK_OFF"} <= blocked
    assert "BEAR_REBOUND" not in blocked
