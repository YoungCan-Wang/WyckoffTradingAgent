from workflows.daily_job_common import Step2StageResult
from workflows.daily_job_runtime import DailyJobConfig
from workflows.daily_job_step2 import persist_step2_outputs
from workflows.daily_job_step3 import run_step3_stage


def _cfg() -> DailyJobConfig:
    return DailyJobConfig(
        webhook="",
        wecom_webhook="",
        dingtalk_webhook="",
        provider="gemini",
        api_key="",
        model="",
        llm_base_url="",
        base_url_env_key="GEMINI_BASE_URL",
        step4_provider="efficiency",
        step4_api_key="",
        step4_model="",
        step4_base_url="",
        step3_skip_llm=True,
        skip_step4=True,
        preview_only=True,
        logs_path="",
    )


def test_persist_step2_outputs_tracks_candidates_when_trade_gate_blocks_recommendations(monkeypatch) -> None:
    import workflows.daily_job_step2 as step2_module

    persisted: list[dict] = []
    monkeypatch.setattr(step2_module, "persist_step2_observations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(step2_module, "run_signal_confirmation", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(step2_module, "run_springboard_scoring", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(step2_module.daily_persistence, "persist_benchmark_context", lambda *_args, **_kwargs: None)

    def fake_step3_review_symbols(symbols, **kwargs):
        persisted.append(
            {
                "step": "review",
                "symbols": symbols,
                "trade_mode": kwargs["trade_mode"].mode,
                "step2_details": kwargs["step2_details"],
            }
        )
        return symbols

    monkeypatch.setattr(
        step2_module.daily_persistence,
        "step3_review_symbols",
        fake_step3_review_symbols,
    )

    def fake_persist_recommendations(*_args, **kwargs):
        persisted.append(
            {
                "step": "persist",
                "trade_mode": kwargs["trade_mode"].mode,
                "step2_details": kwargs["step2_details"],
            }
        )
        return 20260601, []

    monkeypatch.setattr(
        step2_module.daily_persistence,
        "persist_recommendations",
        fake_persist_recommendations,
    )

    result = Step2StageResult(
        ok=True,
        symbols_info=[{"code": "000001", "name": "平安银行"}],
        benchmark_context={"regime": "BEAR_REBOUND"},
        details={"triggers": {}, "all_df_map": {}},
        summary_item={},
        blocking_failure=False,
    )

    recommend_date, payload = persist_step2_outputs(result, _cfg())

    assert recommend_date == 20260601
    assert payload == []
    assert persisted[0]["step"] == "review"
    assert persisted[0]["symbols"] == [{"code": "000001", "name": "平安银行"}]
    assert persisted[0]["trade_mode"] == "repair_review"
    assert persisted[0]["step2_details"] is result.details
    assert persisted[1]["step"] == "persist"
    assert persisted[1]["trade_mode"] == "repair_review"
    assert persisted[1]["step2_details"] is result.details
    assert result.details["step3_symbols_info"] == [{"code": "000001", "name": "平安银行"}]
    assert result.details["trade_mode"]["mode"] == "repair_review"


def test_step3_stage_still_sends_empty_report_when_no_symbols() -> None:
    captured: dict[str, object] = {}

    def fake_run_step3(symbols_info, webhook_url, *_args, benchmark_context=None, **_kwargs):
        captured["symbols_info"] = symbols_info
        captured["webhook_url"] = webhook_url
        captured["benchmark_context"] = benchmark_context
        return True, "ok", "# 空研报"

    result = run_step3_stage(
        symbols_info=[],
        benchmark_context={"regime": "CRASH"},
        run_step3=fake_run_step3,
        cfg=_cfg(),
    )

    assert captured["symbols_info"] == []
    assert captured["benchmark_context"] == {"regime": "CRASH"}
    assert result.report_text == "# 空研报"
    assert result.summary_item["ok"] is True
    assert result.summary_item["output"] == "0 symbols"
