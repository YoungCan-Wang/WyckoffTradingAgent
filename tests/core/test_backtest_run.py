from __future__ import annotations

from datetime import date

import pandas as pd

from core.backtest_config import BacktestRunInput, build_backtest_run_config
from core.backtest_execution import ExitSimulationConfig
from core.backtest_replay import BacktestReplayResult
from core.backtest_run import BacktestPreparedData, BacktestRunContext, execute_backtest_run, parse_date
from core.cash_portfolio import CashPortfolioConfig


def _run_config():
    exit_config = ExitSimulationConfig(
        exit_mode="sltp",
        stop_loss_pct=-7.0,
        take_profit_pct=18.0,
        trailing_stop_pct=0.0,
        trailing_activate_pct=0.0,
        sltp_priority="stop_first",
        atr_period=14,
        atr_multiplier=2.0,
        atr_hard_stop_pct=-9.0,
    )
    return build_backtest_run_config(
        BacktestRunInput(
            start_dt=date(2026, 1, 1),
            end_dt=date(2026, 1, 8),
            hold_days=1,
            board="all",
            top_n=4,
            trading_days=320,
            snapshot_dir=None,
            exit_config=exit_config,
            trailing_activate_pct=0.0,
            buy_friction_pct=0.5,
            sell_friction_pct=0.5,
            regime_filter=True,
            pending_mode="both",
            pending_merge_order="funnel_first",
            metrics_engine="legacy",
            wbt_fee_rate=0.0,
            wbt_n_jobs=1,
            abc_filter=False,
            entry_price_mode="open",
            entry_price_time="14:55",
            entry_price_fallback="close",
            cash_portfolio=True,
            cash_config=CashPortfolioConfig(initial_cash=100_000.0, max_positions=4),
            portfolio_styles="confirmation_only",
            full_formal_l4_max=25,
            selection_mode="tradeable_l4",
            max_atr_hold_days=120,
            funnel_config_overrides={"min_avg_amount_wan": 12345.0},
        )
    )


def test_parse_date_accepts_compact_and_dashed() -> None:
    assert parse_date("20260102") == date(2026, 1, 2)
    assert parse_date("2026/01/02") == date(2026, 1, 2)


def test_execute_backtest_run_builds_summary_from_prepared_data(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_replay_backtest(**kwargs):
        calls["base_cfg"] = kwargs["base_cfg"]
        calls["trade_dates"] = kwargs["trade_dates"]
        return BacktestReplayResult(
            [],
            eval_days=2,
            signal_days=1,
            pending_confirmed_total=3,
            entry_price_missing_skipped=4,
            ohlc_lookup_cache={},
        )

    def fake_enrich(summary: dict, **kwargs):
        calls["performance_config"] = kwargs["config"]
        return {**summary, "enriched": True}

    monkeypatch.setattr("core.backtest_run.replay_backtest", fake_replay_backtest)
    monkeypatch.setattr("core.backtest_run.enrich_backtest_summary", fake_enrich)

    bench_df = pd.DataFrame({"date": [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]})
    data = BacktestPreparedData(
        all_df_map={"000001": pd.DataFrame()},
        bench_df=bench_df,
        name_map={"000001": "平安银行"},
        market_cap_map={"000001": 100.0},
        sector_map={"000001": "银行"},
        concept_map={"000001": ["CPO"]},
        concept_heat=[{"name": "CPO", "pct": 3.2}],
        financial_map={"000001": {"roe": 12}},
        failures=["000002:empty"],
        snapshot_rows_total=10,
        snapshot_used=True,
    )
    context = BacktestRunContext(
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 8),
        board="all",
        sample_size=0,
        use_current_meta=False,
    )

    trades_df, summary = execute_backtest_run(context=context, data=data, config=_run_config())

    assert trades_df.empty
    assert summary["enriched"] is True
    assert summary["universe_ok"] == 1
    assert summary["universe_fail"] == 1
    assert summary["snapshot_used"] is True
    assert summary["concept_map_loaded"] == 1
    assert summary["concept_heat_loaded"] == 1
    assert summary["financial_map_loaded"] == 1
    assert summary["pending_confirmed_total"] == 3
    assert summary["cash_portfolio_styles_requested"] == "confirmation_only"
    assert len(calls["trade_dates"]) == 4
    assert calls["base_cfg"].trading_days == 320
    assert calls["base_cfg"].min_avg_amount_wan == 12345.0
    assert calls["performance_config"].cash_portfolio is True
