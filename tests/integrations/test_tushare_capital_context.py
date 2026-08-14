from __future__ import annotations

import pandas as pd

from integrations.tushare_capital_context import attach_tushare_capital_context


class _FakePro:
    def top_list(self, *, trade_date: str):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "net_amount": 120.0,
                    "l_buy": 500.0,
                    "l_sell": 380.0,
                    "l_amount": 880.0,
                    "reason": "测试",
                }
            ]
        )

    def top_inst(self, *, trade_date: str):
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "exalter": "机构专用", "net_buy": 50.0},
                {"ts_code": "000001.SZ", "exalter": "深股通专用", "net_buy": -20.0},
            ]
        )

    def margin_detail(self, *, trade_date: str):
        if trade_date == "20260612":
            return pd.DataFrame()
        return pd.DataFrame([{"ts_code": "000001.SZ", "rzye": 1_000.0, "rzmre": 80.0, "rzche": 60.0}])

    def block_trade(self, *, trade_date: str):
        return pd.DataFrame([{"ts_code": "000001.SZ", "amount": 300.0, "buyer": "买方", "seller": "卖方"}])

    def moneyflow(self, *, trade_date: str):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "net_mf_amount": 10.0,
                    "buy_lg_amount": 30.0,
                    "sell_lg_amount": 20.0,
                    "buy_elg_amount": 40.0,
                    "sell_elg_amount": 15.0,
                }
            ]
        )

    def moneyflow_hsgt(self, *, start_date: str, end_date: str):
        return pd.DataFrame([{"trade_date": end_date, "north_money": 100.0, "hgt": 40.0, "sgt": 60.0}])

    def hsgt_top10(self, *, trade_date: str):
        return pd.DataFrame([{"ts_code": "000001.SZ", "rank": 2, "amount": 500.0}])


def test_attach_tushare_capital_context_adds_point_in_time_features() -> None:
    contexts = {"000001": {"source_status": {}}}

    attach_tushare_capital_context(contexts, _FakePro(), "2026-06-12")

    result = contexts["000001"]
    assert result["lhb"]["institution_net_buy"] == 50.0
    assert result["lhb"]["connect_seat_net_buy"] == -20.0
    assert result["margin"]["data_date"] == "20260611"
    assert result["stock_moneyflow"]["extra_large_net_amount_wan"] == 25.0
    assert result["northbound_market"]["semantic"] == "published_connect_amount_not_net_inflow"
    assert result["hsgt_top10"]["rank"] == 2
