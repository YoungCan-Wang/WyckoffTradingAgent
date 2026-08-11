"""快照抓取的股票池接线：PIT 生效、失败回落、开关可关。"""

from __future__ import annotations

import pytest

from integrations.pit_universe import PitSymbol
from workflows import backtest_snapshot_fetch as bsf


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKTEST_PIT_UNIVERSE", raising=False)


def _pit(monkeypatch: pytest.MonkeyPatch, symbols: list[PitSymbol]) -> None:
    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", lambda: symbols)


def _live_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bsf,
        "get_stocks_by_board",
        lambda _b: [{"code": "600519", "name": "贵州茅台"}, {"code": "000004", "name": "ST国华"}],
    )


def test_pit_universe_includes_delisted_and_st(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_pool(monkeypatch)
    _pit(
        monkeypatch,
        [
            PitSymbol("600519", "贵州茅台", "20010827", ""),
            PitSymbol("300104", "乐视网", "20100812", "20200721"),
            PitSymbol("000004", "ST国华", "19910114", ""),
        ],
    )

    symbols, pool = bsf._load_symbols("all", 0, as_of="20200101")

    assert symbols == ["000004", "300104", "600519"]
    assert {p["code"] for p in pool} == set(symbols)


def test_falls_back_to_live_pool_when_pit_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """拉取失败不能让回放空跑，但必须回落到存续名单（并在日志说明带偏差）。"""
    _live_pool(monkeypatch)

    def _boom():
        raise RuntimeError("tushare down")

    monkeypatch.setattr("integrations.pit_universe.fetch_pit_symbols", _boom)

    symbols, _ = bsf._load_symbols("all", 0, as_of="20200101")

    assert symbols == ["600519"]  # 回落口径排除 ST


def test_flag_off_keeps_legacy_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKTEST_PIT_UNIVERSE", "0")
    _live_pool(monkeypatch)
    _pit(monkeypatch, [PitSymbol("300104", "乐视网", "20100812", "20200721")])

    symbols, _ = bsf._load_symbols("all", 0, as_of="20200101")

    assert symbols == ["600519"]


def test_no_as_of_keeps_legacy_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_pool(monkeypatch)
    _pit(monkeypatch, [PitSymbol("300104", "乐视网", "20100812", "20200721")])

    symbols, _ = bsf._load_symbols("all", 0)

    assert symbols == ["600519"]


def test_sample_size_applies_to_pit_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_pool(monkeypatch)
    _pit(
        monkeypatch,
        [PitSymbol(f"60{i:04d}", f"股{i}", "20010101", "") for i in range(20)],
    )

    symbols, pool = bsf._load_symbols("all", 5, as_of="20200101")

    assert len(symbols) == 5
    assert len(pool) == 5
