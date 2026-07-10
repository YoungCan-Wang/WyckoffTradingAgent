from __future__ import annotations


def test_send_holding_report_uses_telegram(monkeypatch):
    import workflows.holding_diagnosis_job as mod

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        mod,
        "send_to_telegram",
        lambda text, *, tg_bot_token, tg_chat_id: (
            captured.update({"text": text, "token": tg_bot_token, "chat_id": tg_chat_id}) or True
        ),
    )

    ok = mod._send_holding_report(
        "# holding report",
        mod.HoldingDiagnosisRuntime(
            tickflow_api_key="tf-key",
            tg_bot_token="tg-token",
            tg_chat_id="tg-chat",
            portfolio_id="USER_LIVE",
        ),
    )

    assert ok is True
    assert captured == {
        "text": "📊 持仓诊断\n\n# holding report",
        "token": "tg-token",
        "chat_id": "tg-chat",
    }
