from __future__ import annotations

import pandas as pd


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


def test_step3_preview_empty_input_does_not_notify(monkeypatch) -> None:
    import workflows.step3_reporting as reporting
    from workflows.step3_models import Step3RunOptions
    from workflows.step3_runtime_config import Step3RuntimeConfig

    monkeypatch.setattr(
        reporting,
        "notify_step3_channels",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preview must stay silent")),
    )
    options = Step3RunOptions(
        webhook_url="https://example.invalid/webhook",
        api_key="",
        model="",
        notify=True,
        provider="gemini",
        llm_base_url="",
        wecom_webhook="",
        dingtalk_webhook="",
        runtime_config=Step3RuntimeConfig(skip_llm=True),
    )

    ok, reason, report = reporting.send_empty_step3_report(
        options,
        [],
        {"regime": "BEAR_REBOUND"},
        pd.DataFrame(),
        "",
        [],
    )

    assert (ok, reason) == (True, "ok_preview")
    assert "未执行三阵营模型审判" in report
