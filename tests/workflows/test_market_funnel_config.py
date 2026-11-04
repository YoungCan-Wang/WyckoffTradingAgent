from __future__ import annotations

from dataclasses import asdict
from datetime import date

from core.backtest_run import _base_funnel_config
from core.market_funnel_config import funnel_config_for_market
from workflows.backtest import BacktestWorkflowRequest, _build_run_config


def test_funnel_config_for_market_has_no_etf_special_case() -> None:
    etf_cfg = funnel_config_for_market("etf")
    generic_cfg = funnel_config_for_market("unknown")

    assert etf_cfg.sos_pct_min == generic_cfg.sos_pct_min == 6.0
    assert etf_cfg.sos_vol_ratio == generic_cfg.sos_vol_ratio
    assert etf_cfg.evr_min_turnover == generic_cfg.evr_min_turnover
    assert etf_cfg.spring_vol_ratio == generic_cfg.spring_vol_ratio


def test_backtest_base_funnel_config_matches_market_funnel_config_for_us_and_hk() -> None:
    for market in ("us", "hk"):
        req = BacktestWorkflowRequest(
            start_dt=date(2026, 7, 1),
            end_dt=date(2026, 9, 1),
            hold_days=5,
            top_n=5,
            board=market,
            sample_size=0,
            trading_days=320,
            max_workers=1,
        )
        prod_cfg = asdict(funnel_config_for_market(market, trading_days=320))
        bt_cfg = asdict(_base_funnel_config(_build_run_config(req)))
        prod_cfg.pop("enable_leader_radar", None)
        bt_cfg.pop("enable_leader_radar", None)
        assert bt_cfg == prod_cfg
