from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pandas as pd

import workflows.backtest_snapshot_fetch as snapshot


def test_snapshot_range_prefetches_double_trading_days() -> None:
    args = Namespace(start="2026-02-01", end="2026-06-18", trading_days=10)

    date_range = snapshot._snapshot_range(args)

    assert date_range.start == "20260201"
    assert date_range.end == "20260618"
    assert date_range.prefetch_start == "20260112"


def test_fetch_snapshot_frames_falls_back_when_batch_fails(monkeypatch) -> None:
    frame = pd.DataFrame({"symbol": ["000001"], "收盘": [10.0]})
    monkeypatch.setattr(snapshot, "_tickflow_batch_enabled", lambda: True)
    monkeypatch.setattr(snapshot, "_fetch_batch_tickflow", lambda *args: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(snapshot, "_fetch_concurrent", lambda *args: ([frame], 1, 0, []))

    frames, ok, fail, fail_samples = snapshot._fetch_snapshot_frames(["000001"], "20260101", "20260618", 2)

    assert frames == [frame]
    assert ok == 1
    assert fail == 0
    assert fail_samples == []


def test_write_snapshot_outputs_writes_core_artifacts(monkeypatch, tmp_path: Path) -> None:
    frame = pd.DataFrame({"symbol": ["000001"], "收盘": [10.0]})
    bench = pd.DataFrame({"date": ["2026-06-18"], "close": [4000.0]})
    meta = {"symbols": 1, "ok": 1, "fail": 0, "start": "20260101", "end": "20260618"}
    monkeypatch.setattr(snapshot, "fetch_sector_map", lambda: {"000001": "银行"})
    monkeypatch.setattr(snapshot, "fetch_market_cap_map", lambda: {"000001": 100.0})
    monkeypatch.setattr(snapshot, "fetch_concept_map", lambda: {"000001": ["CPO"]})
    monkeypatch.setattr(snapshot, "fetch_concept_heat", lambda: [{"name": "CPO", "pct": 3.2}])
    monkeypatch.setattr(
        snapshot,
        "fetch_ths_hot_events",
        lambda: {
            "events": [
                {
                    "event_id": "e1",
                    "theme": "人形机器人",
                    "title": "机器人催化",
                    "heat": 500000,
                    "rise_pct": 2.0,
                    "limit_up_count": 10,
                    "stocks": [{"code": "000001", "name": "平安银行"}],
                }
            ]
        },
    )

    snapshot._write_snapshot_outputs(
        tmp_path,
        [frame],
        [{"code": "000001", "name": "平安银行"}],
        bench,
        meta,
    )

    assert (tmp_path / "hist_full.csv.gz").exists()
    assert (tmp_path / "benchmark_main.csv").exists()
    assert json.loads((tmp_path / "name_map.json").read_text(encoding="utf-8")) == {"000001": "平安银行"}
    assert json.loads((tmp_path / "concept_map.json").read_text(encoding="utf-8")) == {"000001": ["CPO"]}
    heat = json.loads((tmp_path / "concept_heat.json").read_text(encoding="utf-8"))
    assert {row["name"] for row in heat} == {"机器人", "CPO"}
    assert json.loads((tmp_path / "ths_hot_events.json").read_text(encoding="utf-8"))["events"][0]["event_id"] == "e1"
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8")) == meta
