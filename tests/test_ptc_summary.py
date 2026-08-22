from __future__ import annotations

from core.ptc_summary import run_ptc


def test_ptc_returns_summary_not_raw_payload():
    source = 'values = data["closes"]\nresult = mean(values)'
    out = run_ptc(source, {"closes": [10.0, 11.0, 12.0]})
    assert out["ok"] is True
    assert "11" in out["summary"]
    assert "closes" not in out["summary"]


def test_ptc_rejects_import_and_attribute():
    assert run_ptc("import os\nresult = 1")["ok"] is False
    assert run_ptc("result = data.get('x')")["ok"] is False
