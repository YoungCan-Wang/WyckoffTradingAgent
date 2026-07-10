from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

import pytest

from core.backtest_config import BacktestRunInput, build_backtest_run_config
from core.backtest_execution import ExitSimulationConfig
from core.cash_portfolio import CashPortfolioConfig


def _exit_config(**overrides) -> ExitSimulationConfig:
    base = ExitSimulationConfig(
        exit_mode="sltp",
        stop_loss_pct=-8.0,
        take_profit_pct=18.0,
        trailing_stop_pct=0.0,
        trailing_activate_pct=0.0,
        sltp_priority="stop_first",
        atr_period=14,
        atr_multiplier=2.0,
        atr_hard_stop_pct=-9.0,
    )
    return replace(base, **overrides)


def _build_config(**overrides):
    params = {
        "start_dt": date(2026, 1, 1),
        "end_dt": date(2026, 2, 1),
        "hold_days": 10,
        "board": "main_chinext_star",
        "top_n": 4,
        "trading_days": 320,
        "snapshot_dir": None,
        "exit_config": _exit_config(),
        "trailing_activate_pct": 0.0,
        "buy_friction_pct": 0.5,
        "sell_friction_pct": 0.5,
        "regime_filter": True,
        "pending_mode": "both",
        "pending_merge_order": "funnel_first",
        "metrics_engine": "legacy",
        "wbt_fee_rate": 0.0,
        "wbt_n_jobs": 1,
        "abc_filter": False,
        "entry_price_mode": "open",
        "entry_price_time": "14:55",
        "entry_price_fallback": "close",
        "cash_portfolio": True,
        "cash_config": CashPortfolioConfig(initial_cash=100_000.0, max_positions=4),
        "portfolio_styles": "confirmation_only",
        "full_formal_l4_max": 25,
        "selection_mode": "tradeable_l4",
        "max_atr_hold_days": 120,
    }
    params.update(overrides)
    return build_backtest_run_config(BacktestRunInput(**params))


def test_build_backtest_run_config_normalizes_and_expands(tmp_path) -> None:
    snapshot = tmp_path / "snapshot"
    config = _build_config(
        metrics_engine=" BOTH ",
        pending_merge_order="CONFIRMED_FIRST",
        entry_price_mode="TAIL_1455",
        entry_price_fallback="SKIP",
        snapshot_dir=snapshot,
        portfolio_styles="confirmation_only,trend_pyramid",
    )

    assert config.metrics_engine == "both"
    assert config.pending_merge_order == "confirmed_first"
    assert config.entry_price_mode == "tail_1455"
    assert config.entry_price_fallback == "skip"
    assert config.snapshot_dir == snapshot.resolve()
    assert config.replay.board == "main_chinext_star"
    assert config.replay.top_n == 4
    assert config.replay.regime_filter is False
    assert config.replay.exit.take_profit_pct == 18.0
    assert config.performance.cash_portfolio is True
    assert [c.portfolio_style for c in config.performance.cash_config_by_style] == [
        "confirmation_only",
        "trend_pyramid",
    ]


def test_build_backtest_run_config_accepts_close_entry_price_mode() -> None:
    config = _build_config(entry_price_mode="CLOSE")

    assert config.entry_price_mode == "close"
    assert config.replay.entry_price_mode == "close"


def test_build_backtest_run_config_preserves_signal_weight_map() -> None:
    meta = {"source": "远端", "active_scope": "尾盘+正式漏斗", "report_date": "2026-07-04"}
    config = _build_config(signal_weight_map={"lps": 0.5, "sos": 1.15}, signal_weight_meta=meta)

    assert config.replay.signal_weight_map == {"lps": 0.5, "sos": 1.15}
    assert config.replay.signal_weight_meta == meta


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"metrics_engine": "bad"}, "metrics_engine 必须是 legacy / auto / both / wbt"),
        ({"entry_price_mode": "bad"}, "entry_price_mode 必须是 open / close / tail_1455"),
        ({"entry_price_fallback": "bad"}, "entry_price_fallback 必须是 close / skip / error"),
        ({"pending_mode": "bad"}, "pending_mode 必须是 off / only / both"),
        ({"hold_days": 0}, "hold_days 必须 >= 1"),
        ({"exit_config": _exit_config(stop_loss_pct=1.0)}, "stop_loss_pct 必须 <= 0，0 表示不设止损"),
        ({"buy_friction_pct": -0.1}, "buy_friction_pct / sell_friction_pct 必须 >= 0"),
        ({"cash_config": CashPortfolioConfig(initial_cash=0)}, "initial_cash 必须 > 0"),
    ],
)
def test_build_backtest_run_config_rejects_invalid_values(overrides, message) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _build_config(**overrides)
