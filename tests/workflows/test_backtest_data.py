from __future__ import annotations

import json
from datetime import date

import pandas as pd

from workflows.backtest_data import (
    board_match,
    load_backtest_history,
    load_backtest_metadata,
    load_snapshot_concept_heat,
    load_snapshot_concept_map,
    load_snapshot_financial_map,
    load_snapshot_hist_map,
    load_snapshot_market_cap_map,
    load_snapshot_sector_map,
    normalize_backtest_board,
    resolve_backtest_universe,
)


def test_all_board_aliases_include_main_chinext_and_star() -> None:
    assert normalize_backtest_board("main_chinext") == "main_chinext_star"
    assert normalize_backtest_board("main_chinext_star") == "main_chinext_star"
    assert board_match("600000", "all")
    assert board_match("300001", "all")
    assert board_match("688001", "all")
    assert board_match("830000", "all")
    assert not board_match("830000", "main_chinext")


def test_resolve_universe_from_snapshot_filters_st_and_board(tmp_path) -> None:
    (tmp_path / "name_map.json").write_text(
        json.dumps(
            {
                "000001": "平安银行",
                "300001": "特锐德",
                "688001": "华兴源创",
                "000002": "ST测试",
                "830000": "北交样本",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    universe = resolve_backtest_universe(board="all", sample_size=0, snapshot_dir=tmp_path)

    assert universe.source == "快照 name_map"
    assert universe.symbols == ["000001", "300001", "688001", "830000"]
    assert universe.name_map["688001"] == "华兴源创"


def test_snapshot_meta_loaders(tmp_path) -> None:
    (tmp_path / "sector_map.json").write_text(json.dumps({"688001": "半导体"}), encoding="utf-8")
    (tmp_path / "market_cap_map.json").write_text(json.dumps({"688001": "123.4"}), encoding="utf-8")
    (tmp_path / "concept_map.json").write_text(json.dumps({"688001": ["CPO"]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "concept_heat.json").write_text(
        json.dumps([{"name": "CPO", "pct": 3.2}], ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "financial_map.json").write_text(json.dumps({"688001": {"roe": 12}}), encoding="utf-8")

    assert load_snapshot_sector_map(tmp_path) == {"688001": "半导体"}
    assert load_snapshot_market_cap_map(tmp_path) == {"688001": 123.4}
    assert load_snapshot_concept_map(tmp_path) == {"688001": ["CPO"]}
    assert load_snapshot_concept_heat(tmp_path) == [{"name": "CPO", "pct": 3.2}]
    assert load_snapshot_financial_map(tmp_path) == {"688001": {"roe": 12}}
    metadata = load_backtest_metadata(use_current_meta=True, snapshot_dir=tmp_path)
    assert metadata.source == "snapshot"
    assert metadata.sector_map == {"688001": "半导体"}
    assert metadata.market_cap_map == {"688001": 123.4}
    assert metadata.concept_map == {"688001": ["CPO"]}
    assert metadata.concept_heat == [{"name": "CPO", "pct": 3.2}]
    assert metadata.financial_map == {"688001": {"roe": 12}}


def test_backtest_metadata_disabled_returns_empty_maps(tmp_path) -> None:
    metadata = load_backtest_metadata(use_current_meta=False, snapshot_dir=tmp_path)

    assert metadata.source == "disabled"
    assert metadata.sector_map == {}
    assert metadata.market_cap_map == {}


def test_load_snapshot_hist_map_normalizes_symbols_and_dates(tmp_path) -> None:
    pd.DataFrame(
        {
            "symbol": ["1", "688001", "AAPL.US"],
            "date": ["2026-01-02", "2026-01-01", "2026-01-01"],
            "open": ["10.0", "20.0", "30.0"],
            "high": ["11.0", "21.0", "31.0"],
            "low": ["9.0", "19.0", "29.0"],
            "close": ["10.5", "20.5", "30.5"],
            "volume": ["100", "200", "300"],
            "amount": ["1000", "2000", "3000"],
            "pct_chg": ["1.0", "2.0", "3.0"],
        }
    ).to_csv(tmp_path / "hist_full.csv.gz", index=False, compression="gzip")

    hist_map, rows = load_snapshot_hist_map(tmp_path, symbols_filter={"000001", "688001"})

    assert rows == 2
    assert set(hist_map) == {"000001", "688001"}
    assert hist_map["000001"].iloc[0]["date"] == date(2026, 1, 2)
    assert hist_map["688001"].iloc[0]["close"] == 20.5


def test_load_backtest_history_uses_snapshot_benchmark_without_network(tmp_path) -> None:
    pd.DataFrame(
        {
            "symbol": ["000001"],
            "date": ["2026-01-01"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
        }
    ).to_csv(tmp_path / "hist_full.csv.gz", index=False, compression="gzip")
    pd.DataFrame(
        {
            "date": ["2026-01-01", "2026-01-02"],
            "open": ["100", "101"],
            "high": ["101", "102"],
            "low": ["99", "100"],
            "close": ["100.5", "101.5"],
            "volume": ["1000", "1100"],
            "pct_chg": ["0.5", "1.0"],
        }
    ).to_csv(tmp_path / "benchmark_main.csv", index=False)

    history = load_backtest_history(
        symbols=["000001"],
        snapshot_dir=tmp_path,
        benchmark="000001",
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 31),
        max_workers=1,
    )

    assert history.snapshot_used is True
    assert history.snapshot_rows_total == 1
    assert history.failures == []
    assert list(history.all_df_map) == ["000001"]
    assert history.bench_df["close"].tolist() == [100.5, 101.5]
