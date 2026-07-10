from __future__ import annotations


def test_step3_run_empty_input_sends_empty_report(monkeypatch) -> None:
    import workflows.step3_batch_report as step3

    captured: dict[str, object] = {}

    def fake_send_empty_step3_report(**kwargs):
        captured.update(kwargs)
        return True, "ok", "# 空研报"

    monkeypatch.setattr(step3, "send_empty_step3_report", fake_send_empty_step3_report)

    ok, reason, report = step3.run(
        [],
        webhook_url="https://example.invalid/webhook",
        api_key="",
        model="",
        benchmark_context={"regime": "CRASH"},
        notify=True,
    )

    assert (ok, reason, report) == (True, "ok", "# 空研报")
    assert captured["items"] == []
    assert captured["benchmark_context"] == {"regime": "CRASH"}
    assert captured["selected_df"].empty
