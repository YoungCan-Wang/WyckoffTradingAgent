from __future__ import annotations

from tools import symbol_pool
from workflows.backtest_data import board_match, normalize_backtest_board, resolve_backtest_universe


def test_default_symbol_pool_includes_bse(monkeypatch) -> None:
    boards = {
        "main": [{"code": "000001", "name": "平安银行"}],
        "chinext": [{"code": "300001", "name": "特锐德"}],
        "star": [{"code": "688001", "name": "华兴源创"}],
        "bse": [{"code": "830000", "name": "北交样本"}],
    }
    monkeypatch.setattr(symbol_pool, "get_stocks_by_board", lambda board: boards[board])

    symbols, name_map, stats = symbol_pool.resolve_symbol_pool()

    assert symbols == ["000001", "300001", "688001", "830000"]
    assert name_map["830000"] == "北交样本"
    assert stats["pool_bse"] == 1


def test_backtest_all_includes_bse_but_legacy_board_excludes_it(tmp_path) -> None:
    (tmp_path / "name_map.json").write_text(
        '{"000001":"平安银行","300001":"特锐德","688001":"华兴源创","830000":"北交样本"}',
        encoding="utf-8",
    )

    assert normalize_backtest_board("main_chinext") == "main_chinext_star"
    assert board_match("830000", "all")
    assert not board_match("830000", "main_chinext_star")
    assert resolve_backtest_universe("all", 0, tmp_path).symbols == ["000001", "300001", "688001", "830000"]
