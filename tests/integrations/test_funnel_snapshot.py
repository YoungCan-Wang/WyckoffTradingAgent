from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from integrations.funnel_snapshot import dump_full_fetch_snapshot


def test_dump_full_fetch_snapshot_writes_expected_files(tmp_path) -> None:
    window = SimpleNamespace(start_trade_date=date(2026, 1, 1), end_trade_date=date(2026, 1, 3))
    df_map = {
        "000001": pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-01", "2026-01-03"]),
                "open": [10.0, 10.5],
                "close": [10.5, 11.0],
            }
        )
    }
    bench_df = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [3100.0], "ignore": [1]})

    run_dir_raw = dump_full_fetch_snapshot(
        enabled=True,
        export_dir=str(tmp_path),
        df_map=df_map,
        all_symbols=["000001", "000002"],
        window=window,
        fetch_stats={"fetch_ok": 1, "fetch_fail": 1},
        bench_df=bench_df,
    )

    assert run_dir_raw is not None
    run_dir = Path(run_dir_raw)
    assert (run_dir / "hist_full.csv.gz").is_file()
    assert (run_dir / "latest_quotes.csv").is_file()
    assert (run_dir / "fetch_status.csv").is_file()
    assert (run_dir / "benchmark_main.csv").is_file()
    assert (run_dir / "metadata.json").is_file()
    assert (tmp_path / "latest_run.txt").read_text(encoding="utf-8").strip() == str(run_dir)

    status = pd.read_csv(run_dir / "fetch_status.csv", dtype={"symbol": str, "latest_trade_date": str}).fillna("")
    assert status[["symbol", "fetched", "rows", "latest_trade_date"]].to_dict("records") == [
        {"symbol": "000001", "fetched": 1, "rows": 2, "latest_trade_date": "2026-01-03"},
        {"symbol": "000002", "fetched": 0, "rows": 0, "latest_trade_date": ""},
    ]
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["symbols_total"] == 2
    assert metadata["symbols_fetched"] == 1
    assert metadata["rows_total"] == 2
    assert metadata["has_benchmark_main"] is True
    assert metadata["has_benchmark_smallcap"] is False


def test_dump_full_fetch_snapshot_skips_when_disabled(tmp_path) -> None:
    window = SimpleNamespace(start_trade_date=date(2026, 1, 1), end_trade_date=date(2026, 1, 3))

    result = dump_full_fetch_snapshot(
        enabled=False,
        export_dir=str(tmp_path),
        df_map={},
        all_symbols=["000001"],
        window=window,
        fetch_stats={},
    )

    assert result is None
    assert not list(tmp_path.iterdir())
