"""港美股回测必须逐日复用生产漏斗，而不是另维护一份会漂移的副本。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.backtest_replay import build_signal_ledger
from core.backtest_run import _base_funnel_config
from workflows.backtest import BacktestWorkflowRequest, _build_run_config
from workflows.market_funnel_asof import run_asof_market_funnel
from workflows.market_funnel_runtime import runtime_config_from_env

ROWS = 340
SYMBOLS = [f"S{i:02d}.US" for i in range(12)]


def _frame(dates: pd.DatetimeIndex, seed: int, drift: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 20.0 * np.cumprod(1.0 + drift + rng.normal(0.0, 0.02, len(dates)))
    volume = rng.uniform(1e6, 3e6, len(dates))
    return pd.DataFrame(
        {
            "date": [d.date() for d in dates],
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "pct_chg": pd.Series(close).pct_change().fillna(0.0).to_numpy() * 100.0,
        }
    )


@pytest.fixture
def market_data(monkeypatch):
    for name in ("MARKET_FUNNEL_KLINE_COUNT", "MARKET_FUNNEL_MIN_HISTORY_ROWS", "MARKET_FUNNEL_MIN_AVG_AMOUNT"):
        monkeypatch.delenv(name, raising=False)
    dates = pd.bdate_range("2025-06-02", periods=ROWS)
    all_df_map = {symbol: _frame(dates, i, 0.0005 * (i % 4)) for i, symbol in enumerate(SYMBOLS)}
    bench = _frame(dates, 99, 0.0004)
    trade_dates = [d.date() for d in dates[-8:]]
    return all_df_map, bench, trade_dates


def _run_config(**overrides):
    req = BacktestWorkflowRequest(
        start_dt=date(2026, 7, 1),
        end_dt=date(2026, 9, 1),
        hold_days=2,
        top_n=overrides.pop("top_n", 0),
        board="us",
        sample_size=0,
        trading_days=320,
        max_workers=1,
        **overrides,
    )
    return _build_run_config(req)


def _ledger(run_config, market_data):
    all_df_map, bench, trade_dates = market_data

    def boom(*_args, **_kwargs):
        raise AssertionError("港美股回测不应再走 A 股水温 analyzer")

    replay = replace(run_config.replay, market_regime_analyzer=boom)
    return build_signal_ledger(
        all_df_map=all_df_map,
        bench_df=bench,
        trade_dates=trade_dates,
        name_map={},
        market_cap_map={},
        sector_map={},
        base_cfg=_base_funnel_config(run_config),
        config=replay,
        max_idx=len(trade_dates) - 2,
    )


def test_backtest_day_selection_equals_production_asof_funnel(market_data) -> None:
    all_df_map, bench, trade_dates = market_data
    ledger = _ledger(_run_config(), market_data)
    runtime = runtime_config_from_env("us", None)

    # 水温需有变化，否则「水温逐日一致」退化成恒等比较。
    assert len({day.context.regime for day in ledger.days}) > 1
    for day in ledger.days:
        prod = run_asof_market_funnel(all_df_map, {}, runtime, bench, day.context.signal_date)
        assert day.context.regime == prod.metrics["benchmark"]["regime"]
        assert (day.selected.codes if day.selected else []) == [c["symbol"] for c in prod.candidates]


def _fake_layers(regime_by_date: dict[date, tuple[str, int | None]]):
    def fake_run_layers(symbols, name_map, df_map, cfg, bench_df=None, sector_map=None, cfg_overrides=None):
        as_of = pd.to_datetime(bench_df["date"]).dt.date.iloc[-1]
        regime, cap = regime_by_date[as_of]
        hits = [(symbol, float(symbol[1:3])) for symbol in symbols]
        metrics = {"layer1": len(symbols), "benchmark": {"regime": regime, "candidate_cap": cap}}
        return {"spring": hits}, metrics

    return fake_run_layers


def test_market_path_uses_production_cap_not_a_share_gates(monkeypatch, market_data) -> None:
    _, _, trade_dates = market_data
    regimes = dict.fromkeys(trade_dates, ("NEUTRAL", None))
    regimes[trade_dates[0]] = ("CRASH", 0)
    regimes[trade_dates[1]] = ("RISK_OFF", 3)
    monkeypatch.setattr("workflows.market_funnel_job._run_layers", _fake_layers(regimes))

    days = {day.context.signal_date: day for day in _ledger(_run_config(top_n=5), market_data).days}

    # 生产 CRASH 上限 0：候选为空，当日不下单。
    assert days[trade_dates[0]].selected is None
    # 生产按分数降序再截到水温上限 3；回测的 top_n=5 不能把它放宽。
    assert days[trade_dates[1]].selected.codes == ["S11.US", "S10.US", "S09.US"]
    # A 股 Step4 禁买 NEUTRAL；港美股生产没有这道闸，不能被它挡掉。
    neutral = days[trade_dates[2]].selected
    assert neutral.codes == ["S11.US", "S10.US", "S09.US", "S08.US", "S07.US"]
    assert neutral.trigger_name_map["S11.US"][1] == "Spring（终极震仓）"


def test_control_group_shares_production_regime_cap(monkeypatch, market_data) -> None:
    _, _, trade_dates = market_data
    regimes = dict.fromkeys(trade_dates, ("RISK_OFF", 2))
    regimes[trade_dates[0]] = ("CRASH", 0)
    monkeypatch.setattr("workflows.market_funnel_job._run_layers", _fake_layers(regimes))

    days = {
        day.context.signal_date: day
        for day in _ledger(_run_config(top_n=5, selection_strategy="random"), market_data).days
    }

    assert days[trade_dates[0]].selected is None
    assert days[trade_dates[0]].blocked_signal_day
    assert len(days[trade_dates[1]].selected.codes) == 2


def test_research_overrides_reach_production_funnel(monkeypatch, market_data) -> None:
    all_df_map, bench, trade_dates = market_data
    seen: list[bool] = []

    def spy_layers(symbols, name_map, df_map, cfg, bench_df=None, sector_map=None, cfg_overrides=None):
        from workflows.market_funnel_job import _funnel_config

        seen.append(_funnel_config(cfg, cfg_overrides).enable_evr_trigger)
        return {}, {"benchmark": {"regime": "NEUTRAL", "candidate_cap": None}}

    monkeypatch.setattr("workflows.market_funnel_job._run_layers", spy_layers)
    _ledger(_run_config(funnel_overrides=(("enable_evr_trigger", False),)), market_data)

    assert seen and not any(seen)


def test_market_backtest_rejects_trading_days_that_differ_from_production() -> None:
    with pytest.raises(ValueError, match="trading_days"):
        _build_run_config(
            BacktestWorkflowRequest(
                start_dt=date(2026, 7, 1),
                end_dt=date(2026, 9, 1),
                hold_days=5,
                top_n=5,
                board="us",
                sample_size=0,
                trading_days=220,
                max_workers=1,
            )
        )
