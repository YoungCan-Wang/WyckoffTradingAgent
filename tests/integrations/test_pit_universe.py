"""PIT 股票池：窗口内可交易的判据，含历史退市与当时 ST。"""

from __future__ import annotations

from integrations.pit_universe import build_pit_symbols, tradable_on, universe_gap


def _row(symbol: str, name: str, list_date: str, delist_date: str = "") -> dict:
    return {"symbol": symbol, "name": name, "list_date": list_date, "delist_date": delist_date}


def test_delisted_stock_is_tradable_before_its_delist_date() -> None:
    """回归：快照池按拉取当时的存续名单生成，把窗口内还在交易的退市股全漏了。"""
    symbols = build_pit_symbols([_row("300104", "乐视网", "20100812", "20200721")])

    assert [s.code for s in tradable_on(symbols, "20200101")] == ["300104"]
    assert tradable_on(symbols, "20210101") == []


def test_st_stock_is_not_excluded() -> None:
    """ST 股在窗口内可交易；排除它们正是原偏差的另一半来源。"""
    symbols = build_pit_symbols([_row("000004", "ST国华", "19910114")])

    picked = tradable_on(symbols, "20240101")

    assert [s.code for s in picked] == ["000004"]
    assert picked[0].is_st is True


def test_not_yet_listed_is_excluded() -> None:
    symbols = build_pit_symbols([_row("301111", "新股", "20250601")])

    assert tradable_on(symbols, "20240101") == []
    assert [s.code for s in tradable_on(symbols, "20250901")] == ["301111"]


def test_delist_on_boundary_day_is_still_tradable() -> None:
    symbols = build_pit_symbols([_row("000003", "PT金田A", "19910114", "20020614")])

    assert [s.code for s in tradable_on(symbols, "20020614")] == ["000003"]
    assert tradable_on(symbols, "20020615") == []


def test_duplicate_code_keeps_the_delisted_record() -> None:
    """同代码同时出现在 L 与 D 表时须保留带退市日的那条，否则回放不知道它已摘牌。"""
    symbols = build_pit_symbols(
        [_row("000005", "ST星源", "19901210"), _row("000005", "ST星源(退)", "19901210", "20240426")]
    )

    assert len(symbols) == 1
    assert symbols[0].delist_date == "20240426"
    assert tradable_on(symbols, "20250101") == []


def test_bse_can_be_excluded_for_non_bse_boards() -> None:
    symbols = build_pit_symbols([_row("830799", "北交所股", "20210101"), _row("600519", "贵州茅台", "20010827")])

    assert [s.code for s in tradable_on(symbols, "20240101", include_bse=False)] == ["600519"]
    assert len(tradable_on(symbols, "20240101", include_bse=True)) == 2


def test_unsupported_board_is_dropped() -> None:
    assert build_pit_symbols([_row("999999", "非A股", "20200101")]) == []


def test_universe_gap_splits_delisted_and_st() -> None:
    symbols = build_pit_symbols(
        [
            _row("600519", "贵州茅台", "20010827"),
            _row("300104", "乐视网", "20100812", "20200721"),
            _row("000004", "ST国华", "19910114"),
        ]
    )
    should = tradable_on(symbols, "20200101")

    gap = universe_gap(should, {"600519"})

    assert gap["should"] == 3
    assert gap["missing"] == 2
    assert gap["missing_delisted"] == 1
    assert gap["missing_st"] == 1
