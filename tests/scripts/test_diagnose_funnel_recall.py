from __future__ import annotations

import json

from scripts import diagnose_funnel_recall as diagnose
from workflows.review_trace import dump_review_trace_artifact


def test_trace_diagnosis_is_offline_and_retains_all_layer_and_risk_flags(monkeypatch, tmp_path):
    payload = {
        "schema_version": "review_trace_v1",
        "market": "cn",
        "trade_date": "2026-09-04",
        "run": {"git_sha": "production-sha"},
        "symbols": {
            "300308": {
                "name": "中际旭创",
                "stage": "题材共振不足",
                "reason": "行业回撤",
                "l1_eligible": True,
                "l2_eligible": True,
                "l3_eligible": False,
                "risk_signal": "stop_loss",
                "trigger_labels": ["TrendPB"],
            },
        },
    }
    trace_path = dump_review_trace_artifact(payload, str(tmp_path))
    output = tmp_path / "diagnosis.json"
    monkeypatch.setattr(
        "sys.argv",
        ["diagnose", "300308", "--date", "2026-09-04", "--trace", str(trace_path), "--json-out", str(output)],
    )
    monkeypatch.setattr(
        diagnose, "_resolve_signal_date", lambda *_a: (_ for _ in ()).throw(AssertionError("network calendar called"))
    )
    monkeypatch.setattr(diagnose, "_replay", lambda *_a: (_ for _ in ()).throw(AssertionError("replay called")))

    assert diagnose.main() == 0
    result = json.loads(output.read_text())
    assert result["context_source"] == "production_artifact:production-s"
    assert result["rows"][0]["layers"] == {
        "in_universe": True,
        "l1": True,
        "l2": True,
        "l3": False,
        "buy_triggers": ["TrendPB"],
        "exit_signal": "stop_loss",
        "candidate_entry": False,
    }
