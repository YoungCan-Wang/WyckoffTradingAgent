from __future__ import annotations

from workflows.step3_runtime_config import Step3RuntimeConfig, step3_runtime_config_from_env


def test_compliance_brief_is_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("STEP3_SEND_COMPLIANCE_BRIEF", raising=False)

    assert Step3RuntimeConfig().send_compliance_brief is True
    assert step3_runtime_config_from_env().send_compliance_brief is True


def test_compliance_brief_can_be_disabled_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("STEP3_SEND_COMPLIANCE_BRIEF", "0")

    assert step3_runtime_config_from_env().send_compliance_brief is False
