from __future__ import annotations

from workflows.market_funnel_config import funnel_config_for_market


def test_funnel_config_for_market_has_no_etf_special_case() -> None:
    etf_cfg = funnel_config_for_market("etf")
    generic_cfg = funnel_config_for_market("unknown")

    assert etf_cfg.sos_pct_min == generic_cfg.sos_pct_min == 6.0
    assert etf_cfg.sos_vol_ratio == generic_cfg.sos_vol_ratio
    assert etf_cfg.evr_min_turnover == generic_cfg.evr_min_turnover
    assert etf_cfg.spring_vol_ratio == generic_cfg.spring_vol_ratio
