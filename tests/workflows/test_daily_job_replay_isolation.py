from __future__ import annotations

from argparse import Namespace

from workflows.daily_job_runtime import DailyJobConfig, resolve_daily_job_config
from workflows.daily_job_step4 import run_step4_stage


def _stub_provider_config(monkeypatch) -> None:
    import workflows.daily_job_runtime as runtime

    monkeypatch.setattr(runtime, "resolve_provider_name", lambda _key, default: default)
    monkeypatch.setattr(runtime, "get_provider_credentials", lambda _provider: ("key", "model", "base"))


def _step4_config(*, historical_replay: bool, skip_step4: bool) -> DailyJobConfig:
    return DailyJobConfig(
        webhook="",
        wecom_webhook="",
        dingtalk_webhook="",
        provider="gemini",
        api_key="key",
        model="model",
        llm_base_url="",
        base_url_env_key="GEMINI_BASE_URL",
        step4_provider="efficiency",
        step4_api_key="key",
        step4_model="model",
        step4_base_url="base",
        step3_skip_llm=False,
        skip_step4=skip_step4,
        historical_replay=historical_replay,
        preview_only=False,
        logs_path="",
    )


def test_explicit_end_calendar_day_forces_step4_off(monkeypatch, tmp_path) -> None:
    _stub_provider_config(monkeypatch)
    monkeypatch.setenv("END_CALENDAR_DAY", "2026-05-26")
    monkeypatch.delenv("DAILY_JOB_SKIP_STEP4", raising=False)
    monkeypatch.delenv("DAILY_JOB_PREVIEW_ONLY", raising=False)

    cfg = resolve_daily_job_config(Namespace(logs=str(tmp_path / "daily.log")))

    assert cfg.historical_replay is True
    assert cfg.skip_step4 is True


def test_resolve_daily_job_config_defaults_step3_to_efficiency(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STEP3_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("STEP4_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://efficiency.example/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-main")

    cfg = resolve_daily_job_config(Namespace(logs=str(tmp_path / "daily.log")))

    assert cfg.provider == "efficiency"
    assert cfg.step4_provider == "efficiency"


def test_log_llm_config_announces_gemini_fallback(monkeypatch) -> None:
    from workflows.daily_job_runtime import log_llm_config

    logs: list[str] = []
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-main")

    log_llm_config(
        "efficiency",
        "https://efficiency.example/v1",
        "EFFICIENCY_BASE_URL",
        None,
        lambda msg, _path: logs.append(msg),
    )

    assert any("Gemini 兜底已配置: model=gemini-main" in msg for msg in logs)


def test_live_job_keeps_step4_enabled(monkeypatch, tmp_path) -> None:
    _stub_provider_config(monkeypatch)
    monkeypatch.delenv("END_CALENDAR_DAY", raising=False)
    monkeypatch.delenv("DAILY_JOB_SKIP_STEP4", raising=False)
    monkeypatch.delenv("DAILY_JOB_PREVIEW_ONLY", raising=False)

    cfg = resolve_daily_job_config(Namespace(logs=str(tmp_path / "daily.log")))

    assert cfg.historical_replay is False
    assert cfg.skip_step4 is False


def test_historical_replay_never_loads_live_step4_target(monkeypatch) -> None:
    import workflows.daily_job_step4 as step4

    def forbidden_live_read() -> None:
        raise AssertionError("historical replay must not load live Step4 state")

    monkeypatch.setattr(step4, "load_step4_target", forbidden_live_read)

    summary = run_step4_stage(
        cfg=_step4_config(historical_replay=True, skip_step4=True),
        symbols_info=[],
        step3_springboard_codes=[],
        step3_report_text="",
        benchmark_context={},
    )

    assert summary["ok"] is True
    assert summary["output"] == "skipped (END_CALENDAR_DAY 回放隔离)"
