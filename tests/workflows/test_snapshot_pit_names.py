"""快照 name_map 的 PIT 名称接线：生效、失败回落、下游不再误滤。"""

from __future__ import annotations

import pytest

from integrations.pit_universe import NameSpan, PitSymbol
from workflows import backtest_snapshot_fetch as bsf


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKTEST_PIT_UNIVERSE", raising=False)
    monkeypatch.setattr(bsf, "get_stocks_by_board", lambda _b: [{"code": "600519", "name": "贵州茅台"}])


# 600393 今日名含 ST，2020 年当时名为「粤泰股份」
TRADABLE = [
    PitSymbol("600393", "ST粤泰(退)", "20010319", ""),
    PitSymbol("600519", "贵州茅台", "20010827", ""),
]
SPANS = {
    "600393": [
        NameSpan("600393", "粤泰股份", "20160512", "20210505"),
        NameSpan("600393", "ST粤泰", "20230505", ""),
    ]
}


def test_name_map_uses_as_of_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：今日 ST 名会让 2020 年可交易的 600393 被下游按名称剔除。"""
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: TRADABLE)
    monkeypatch.setattr("integrations.pit_universe.fetch_name_spans", lambda: SPANS)

    _symbols, pool = bsf._load_symbols("all", 0, as_of="20200101")
    names = {p["code"]: p["name"] for p in pool}

    assert names["600393"] == "粤泰股份"
    assert "ST" not in names["600393"].upper()


def test_st_at_as_of_stays_st(monkeypatch: pytest.MonkeyPatch) -> None:
    """当时确实是 ST 的，名称须保留 ST，让下游照常剔除。"""
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: TRADABLE)
    monkeypatch.setattr("integrations.pit_universe.fetch_name_spans", lambda: SPANS)

    _symbols, pool = bsf._load_symbols("all", 0, as_of="20260101")
    names = {p["code"]: p["name"] for p in pool}

    assert names["600393"] == "ST粤泰"


def test_falls_back_to_today_name_when_spans_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """改名记录拉取失败不能让快照空跑；回落到今日名称并保留其它 PIT 成果。"""
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: TRADABLE)

    def _boom():
        raise RuntimeError("tushare namechange down")

    monkeypatch.setattr("integrations.pit_universe.fetch_name_spans", _boom)

    symbols, pool = bsf._load_symbols("all", 0, as_of="20200101")
    names = {p["code"]: p["name"] for p in pool}

    assert symbols == ["600393", "600519"]
    assert names["600393"] == "ST粤泰(退)"


def test_empty_spans_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: TRADABLE)
    monkeypatch.setattr("integrations.pit_universe.fetch_name_spans", dict)

    _symbols, pool = bsf._load_symbols("all", 0, as_of="20200101")

    assert {p["code"]: p["name"] for p in pool}["600393"] == "ST粤泰(退)"


def test_code_without_change_history_keeps_today_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: TRADABLE)
    monkeypatch.setattr("integrations.pit_universe.fetch_name_spans", lambda: SPANS)

    _symbols, pool = bsf._load_symbols("all", 0, as_of="20200101")

    assert {p["code"]: p["name"] for p in pool}["600519"] == "贵州茅台"
