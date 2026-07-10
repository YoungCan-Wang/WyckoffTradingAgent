from __future__ import annotations

import json
from dataclasses import dataclass

from workflows.single_symbol_diagnosis_outputs import build_single_symbol_report, write_single_symbol_outputs


@dataclass(frozen=True)
class Spec:
    market: str = "cn"
    symbol: str = "603390"
    label: str = "A股"


@dataclass
class Row:
    date: str = "2026-06-01"
    status: str = "SELECTED"
    failed_layer: str = "-"
    reason: str = "触发 SOS"
    triggers: str = "SOS"
    trigger_scores: str = "sos=12.50"
    abc_grade: str = "A"
    abc_count: int = 3
    channel: str = "main"
    close: float | None = 12.34
    pct_chg: float | None = 8.91
    vol_ratio: float | None = 2.5
    amount_avg_wan: float | None = 8000.0
    ma50: float | None = 11.0
    ma200: float | None = 10.0


def _summary() -> dict:
    return {
        "total_days": 1,
        "selected_days": 1,
        "counts": {"SELECTED": 1},
        "first_selected": "2026-06-01",
        "last_selected": "2026-06-01",
    }


def test_build_single_symbol_report_renders_daily_rows():
    report = build_single_symbol_report(Spec(), [Row()], _summary())

    assert "# 单票漏斗复盘诊断：603390" in report
    assert "- 层级分布: SELECTED=1" in report
    assert "| 2026-06-01 | SELECTED | - | SOS | A | 12.34 | 8.91% | 2.50x | 触发 SOS |" in report


def test_write_single_symbol_outputs_writes_csv_json_and_markdown(tmp_path):
    paths = write_single_symbol_outputs(tmp_path, Spec(), [Row()], _summary())

    assert sorted(paths) == ["csv", "json", "md"]
    assert "603390" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "2026-06-01" in (tmp_path / "daily_diagnostics.csv").read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["symbol"]["symbol"] == "603390"
    assert payload["summary"]["selected_days"] == 1
