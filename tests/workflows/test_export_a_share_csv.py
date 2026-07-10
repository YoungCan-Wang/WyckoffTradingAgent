from __future__ import annotations

from datetime import date

import pandas as pd

from integrations.fetch_a_share_csv import TradingWindow
from workflows.export_a_share_csv import ExportAShareCsvRequest, build_export_frame, run_export_a_share_csv


def test_build_export_frame_adds_expected_columns() -> None:
    frame = pd.DataFrame({"日期": ["2026-06-22"], "成交量": [100], "成交额": [2500]})

    out = build_export_frame(frame, sector="银行")

    assert list(out.columns) == [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "Amount",
        "TurnoverRate",
        "Amplitude",
        "AvgPrice",
        "Sector",
    ]
    assert out.loc[0, "AvgPrice"] == 25
    assert out.loc[0, "Sector"] == "银行"


def test_run_export_a_share_csv_writes_hist_and_ohlcv(monkeypatch, tmp_path) -> None:
    import workflows.export_a_share_csv as workflow

    hist = pd.DataFrame(
        {
            "日期": ["2026-06-20"],
            "开盘": [10],
            "最高": [11],
            "最低": [9],
            "收盘": [10.5],
            "成交量": [100],
            "成交额": [1050],
            "换手率": [1.2],
            "振幅": [2.0],
        }
    )
    monkeypatch.setattr(workflow, "get_all_stocks", lambda: [{"code": "000001", "name": "平安银行"}])
    monkeypatch.setattr(
        workflow,
        "resolve_trading_window",
        lambda **_kwargs: TradingWindow(date(2026, 6, 1), date(2026, 6, 20)),
    )
    monkeypatch.setattr(workflow, "fetch_hist", lambda **_kwargs: hist)
    monkeypatch.setattr(workflow, "stock_sector_em", lambda _symbol: "银行")

    result = run_export_a_share_csv(ExportAShareCsvRequest(symbol="000001", out_dir=str(tmp_path)))

    assert result == 0
    assert (tmp_path / "000001_平安银行_hist_data.csv").exists()
    ohlcv = (tmp_path / "000001_平安银行_ohlcv.csv").read_text(encoding="utf-8-sig")
    assert "AvgPrice" in ohlcv
    assert "银行" in ohlcv
