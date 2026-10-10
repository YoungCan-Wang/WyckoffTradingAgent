from __future__ import annotations

import sys
from argparse import Namespace
from datetime import date

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
    assert cfg.suppress_shared_side_effects() is True
    assert cfg.preview_only is False


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
    assert cfg.suppress_shared_side_effects() is False


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


def test_historical_replay_skips_shared_writes_and_notifications(monkeypatch, tmp_path) -> None:
    import scripts.daily_job as daily_job
    import tools.report_parser as report_parser
    import workflows.daily_job_persistence as daily_persistence
    import workflows.daily_job_runtime as daily_runtime
    import workflows.daily_job_step2 as daily_step2
    import workflows.daily_job_step3 as daily_step3
    import workflows.step2_signal_confirmation as signal_confirmation
    import workflows.step3_batch_report as step3_batch_report
    import workflows.wyckoff_funnel as wyckoff_funnel
    from integrations.supabase_base import is_server_write_context

    captured: dict[str, object] = {}

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("historical replay must not write shared persistence tables")

    def fake_run_funnel(webhook_url, *, notify=True, return_details=False, include_financial_metrics=True):
        captured["step2_webhook"] = webhook_url
        captured["step2_notify"] = notify
        captured["server_write_allowed"] = is_server_write_context()
        return (
            True,
            [{"code": "000001", "name": "平安银行", "tag": "SOS"}],
            {"regime": "NEUTRAL"},
            {
                "triggers": {"sos": [("000002", 1.0)]},
                "all_df_map": {"000002": object()},
                "name_map": {"000002": "万科A"},
                "sector_map": {"000002": "房地产"},
                "selected_for_ai": ["000002"],
                "candidate_entries": [{"code": "000002"}],
            },
        )

    def fake_run_step2_5(*_args, dry_run=False, **_kwargs):
        captured["signal_dry_run"] = dry_run
        return []

    def fake_run_step3(symbols_info, webhook_url, *_args, **_kwargs):
        captured["step3_webhook"] = webhook_url
        return True, "ok", "# Step3 replay report\n操作池：000001"

    monkeypatch.setenv("END_CALENDAR_DAY", "2026-05-26")
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.delenv("DAILY_JOB_PREVIEW_ONLY", raising=False)
    monkeypatch.delenv("DAILY_JOB_SKIP_STEP4", raising=False)
    monkeypatch.delenv("STEP3_SKIP_LLM", raising=False)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "eff-model")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://efficiency.example/v1")
    monkeypatch.setattr(sys, "argv", ["daily_job.py", "--logs", str(tmp_path / "replay.log")])
    monkeypatch.setattr(daily_runtime, "resolve_end_calendar_day", lambda: date(2026, 5, 26))
    monkeypatch.setattr(daily_runtime, "is_a_share_trading_day", lambda d: d == date(2026, 5, 27))
    monkeypatch.setattr(daily_step2, "latest_trade_date_str", lambda: "2026-05-26")
    monkeypatch.setattr(daily_step3, "latest_trade_date_str", lambda: "2026-05-26")
    monkeypatch.setattr(daily_persistence, "upsert_market_signal_daily", forbidden_write)
    monkeypatch.setattr(daily_persistence, "prepare_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_persistence, "upsert_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_persistence, "mark_ai_recommendations", forbidden_write)
    monkeypatch.setattr(daily_step2, "run_springboard_scoring", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(wyckoff_funnel, "run", fake_run_funnel)
    monkeypatch.setattr(step3_batch_report, "run", fake_run_step3)
    monkeypatch.setattr(report_parser, "extract_operation_pool_codes", lambda **_kwargs: ["000001"])
    monkeypatch.setattr(report_parser, "extract_operation_pool_springboards", lambda **_kwargs: {})
    monkeypatch.setattr(signal_confirmation, "run_step2_5", fake_run_step2_5)

    assert daily_job.main() == 0
    assert captured["step2_webhook"] == ""
    assert captured["step2_notify"] is False
    assert captured["signal_dry_run"] is True
    assert captured["server_write_allowed"] is False
    assert captured["step3_webhook"] == "https://example.invalid/webhook"
