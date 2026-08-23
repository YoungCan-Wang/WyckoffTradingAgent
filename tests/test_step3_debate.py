from __future__ import annotations

import pandas as pd

from core.step3_debate import build_debate_record, debate_block, shadow_or_apply_veto
from workflows.step3_reporting import _debate_overlay


def test_debate_is_shadow_and_cannot_veto_by_default(monkeypatch):
    monkeypatch.delenv("STEP3_RISK_VETO", raising=False)
    record = build_debate_record({"code": "000001", "signal_type": "utad", "score": -1.2, "close": 10})
    assert record["risk_veto"] is True
    assert record["shadow"] is True
    assert shadow_or_apply_veto([record]) == []
    assert _debate_overlay(pd.DataFrame([record])) == ""


def test_armed_veto_only_removes_risk_codes(monkeypatch):
    monkeypatch.setenv("STEP3_RISK_VETO", "1")
    risky = build_debate_record({"code": "000002", "signal_type": "spring", "score": 3.0})
    toxic = build_debate_record({"code": "000003", "signal_type": "upthrust", "score": 1.0})
    assert risky["shadow"] is False
    assert shadow_or_apply_veto([risky, toxic]) == ["000003"]
    block = debate_block([toxic])
    assert "不能升级交易许可" in block
    overlay = _debate_overlay(pd.DataFrame([{"code": "000003", "signal_type": "upthrust", "score": 1.0}]))
    assert "000003" in overlay
    assert "多空辩论" in overlay
