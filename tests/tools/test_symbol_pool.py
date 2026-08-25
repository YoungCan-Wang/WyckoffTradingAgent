from __future__ import annotations

import sys
from types import ModuleType


def test_load_stock_name_map_does_not_merge_etf_names(monkeypatch) -> None:
    sys.modules.setdefault("akshare", ModuleType("akshare"))
    from tools import symbol_pool

    monkeypatch.setattr(
        "integrations.fetch_a_share_csv.get_all_stocks",
        lambda: [{"code": "000001", "name": "平安银行"}],
    )

    def boom() -> dict[str, str]:
        raise AssertionError("漏斗 name_map 不得再合并 load_etf_name_map")

    monkeypatch.setattr("tools.market_universe_meta.load_etf_name_map", boom)

    result = symbol_pool.load_stock_name_map()

    assert result == {"000001": "平安银行"}
    assert "159915" not in result
