from __future__ import annotations

import sys
from datetime import date

import pandas as pd


def test_preview_only_skips_persistence_and_keeps_llm_input_path(monkeypatch, tmp_path):
    import core.batch_report as batch_report
    import core.funnel_pipeline as funnel_pipeline
    import integrations.supabase_signal_pending as signal_pending
    import scripts.daily_job as daily_job

    captured: dict[str, object] = {}

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("preview-only job must not write persistence tables")

    def fake_run_funnel(webhook_url, *, notify=True, return_details=False):
        captured["step2_webhook"] = webhook_url
        captured["step2_notify"] = notify
        captured["step2_return_details"] = return_details
        return (
            True,
            [{"code": "000001", "name": "平安银行", "tag": "SOS"}],
            {"regime": "NEUTRAL"},
            {
                "triggers": {"sos": [("000002", 1.0)]},
                "all_df_map": {"000002": object()},
                "name_map": {"000002": "万科A"},
                "sector_map": {"000002": "房地产"},
            },
        )

    def fake_run_step2_5(*_args, dry_run=False, **_kwargs):
        captured["signal_dry_run"] = dry_run
        return [{"code": "000002", "name": "万科A", "tag": "pending confirmed"}]

    def fake_run_step3(symbols_info, webhook_url, *_args, **_kwargs):
        captured["step3_symbols"] = [item["code"] for item in symbols_info]
        captured["step3_webhook"] = webhook_url
        return True, "ok_preview", "# Step3 模型输入预演"

    monkeypatch.setenv("STEP3_SKIP_LLM", "1")
    monkeypatch.setenv("DAILY_JOB_SKIP_STEP4", "1")
    monkeypatch.setenv("DAILY_JOB_PREVIEW_ONLY", "1")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(sys, "argv", ["daily_job.py", "--logs", str(tmp_path / "preview.log")])
    monkeypatch.setattr(daily_job, "next_trading_day", lambda today: today)
    monkeypatch.setattr(daily_job, "_latest_trade_date_str", lambda: "2026-05-19")
    monkeypatch.setattr(daily_job, "upsert_market_signal_daily", forbidden_write)
    monkeypatch.setattr(daily_job, "prepare_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_job, "upsert_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_job, "mark_ai_recommendations", forbidden_write)
    monkeypatch.setattr(daily_job, "_run_springboard_scoring", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(funnel_pipeline, "run_funnel", fake_run_funnel)
    monkeypatch.setattr(batch_report, "run_step3", fake_run_step3)
    monkeypatch.setattr(batch_report, "extract_operation_pool_codes", lambda **_kwargs: ["000001"])
    monkeypatch.setattr(signal_pending, "run_step2_5", fake_run_step2_5)

    assert daily_job.main() == 0
    assert captured["step2_webhook"] == ""
    assert captured["step2_notify"] is False
    assert captured["step2_return_details"] is True
    assert captured["signal_dry_run"] is True
    assert captured["step3_webhook"] == "https://example.invalid/webhook"
    assert captured["step3_symbols"] == ["000001", "000002"]


def test_springboard_scoring_uses_item_signal_type(monkeypatch):
    import core.signal_confirmation as signal_confirmation
    import scripts.daily_job as daily_job

    def fake_score(_df, signal_type):
        return {
            "a": signal_type == "spring",
            "b": False,
            "c": False,
            "grade": signal_type,
            "met_count": 1,
            "support": 1.0,
            "touch_count": 1,
            "evidence": {},
        }

    monkeypatch.setattr(signal_confirmation, "score_springboard_abc", fake_score)
    df = pd.DataFrame({"close": [1.0]})
    symbols = [{"code": "000001", "signal_type": "spring"}]
    details = {
        "triggers": {"sos": [("000001", 1.0)], "spring": [("000001", 1.0)]},
        "all_df_map": {"000001": df},
    }

    assert daily_job._run_springboard_scoring(symbols, details) == 1
    assert symbols[0]["springboard_grade"] == "spring"


def test_springboard_scoring_scores_confirmed_extra_without_trigger(monkeypatch):
    import core.signal_confirmation as signal_confirmation
    import scripts.daily_job as daily_job

    monkeypatch.setattr(
        signal_confirmation,
        "score_springboard_abc",
        lambda _df, signal_type: {
            "a": True,
            "b": False,
            "c": False,
            "grade": signal_type,
            "met_count": 1,
            "support": 1.0,
            "touch_count": 1,
            "evidence": {},
        },
    )
    symbols = [{"code": "000001", "signal_type": "lps"}]
    details = {"triggers": {}, "all_df_map": {"000001": pd.DataFrame({"close": [1.0]})}}

    assert daily_job._run_springboard_scoring(symbols, details) == 1
    assert symbols[0]["springboard_grade"] == "lps"


def _patch_daily_job_step2(monkeypatch, tmp_path, daily_job):
    import core.funnel_pipeline as funnel_pipeline

    monkeypatch.delenv("DAILY_JOB_PREVIEW_ONLY", raising=False)
    monkeypatch.setenv("STEP3_SKIP_LLM", "1")
    monkeypatch.setenv("DAILY_JOB_SKIP_STEP4", "1")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(sys, "argv", ["daily_job.py", "--logs", str(tmp_path / "daily.log")])
    monkeypatch.setattr(daily_job, "is_a_share_trading_day", lambda _today: True)
    monkeypatch.setattr(daily_job, "_persist_benchmark_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daily_job, "_persist_theme_radar", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daily_job, "_run_signal_confirmation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daily_job, "_run_springboard_scoring", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(daily_job, "_persist_signal_observations", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daily_job, "_write_recommendation_backup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        daily_job,
        "_persist_recommendations",
        lambda *_args, **_kwargs: (20260519, [{"code": "000001"}]),
    )
    monkeypatch.setattr(
        funnel_pipeline,
        "run_funnel",
        lambda *_args, **_kwargs: (True, [{"code": "000001"}], {"regime": "NEUTRAL"}, {"triggers": {}}),
    )


def test_daily_job_skips_ai_mark_when_step3_parse_fails(monkeypatch, tmp_path):
    import core.batch_report as batch_report
    import scripts.daily_job as daily_job

    calls = []
    _patch_daily_job_step2(monkeypatch, tmp_path, daily_job)
    monkeypatch.setattr(daily_job, "mark_ai_recommendations", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(batch_report, "run_step3", lambda *_args, **_kwargs: (True, "ok", "# report"))
    monkeypatch.setattr(
        batch_report,
        "extract_operation_pool_codes",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )

    assert daily_job.main() == 0
    assert calls == []


def test_daily_job_skips_ai_mark_when_step3_fails(monkeypatch, tmp_path):
    import core.batch_report as batch_report
    import scripts.daily_job as daily_job

    calls = []
    _patch_daily_job_step2(monkeypatch, tmp_path, daily_job)
    monkeypatch.setattr(daily_job, "mark_ai_recommendations", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(batch_report, "run_step3", lambda *_args, **_kwargs: (False, "llm_failed", ""))

    assert daily_job.main() == 0
    assert calls == []


def test_non_trading_skip_message_keeps_trading_friday(monkeypatch):
    import scripts.daily_job as daily_job

    monkeypatch.setattr(daily_job, "is_a_share_trading_day", lambda _today: True)
    monkeypatch.setattr(
        daily_job,
        "next_trading_day",
        lambda _today: (_ for _ in ()).throw(AssertionError("next day should not be checked")),
    )

    assert daily_job._non_trading_skip_message(date(2026, 5, 29)) is None


def test_non_trading_skip_message_allows_weekend_pre_run(monkeypatch):
    import scripts.daily_job as daily_job

    monkeypatch.setattr(daily_job, "is_a_share_trading_day", lambda _today: False)
    monkeypatch.setattr(daily_job, "next_trading_day", lambda _today: date(2026, 6, 1))

    assert daily_job._non_trading_skip_message(date(2026, 5, 31)) is None


def test_non_trading_skip_message_skips_long_break(monkeypatch):
    import scripts.daily_job as daily_job

    monkeypatch.setattr(daily_job, "is_a_share_trading_day", lambda _today: False)
    monkeypatch.setattr(daily_job, "next_trading_day", lambda _today: date(2026, 6, 3))

    msg = daily_job._non_trading_skip_message(date(2026, 5, 30))

    assert msg == "📅 今日 2026-05-30 非交易日，下一交易日 2026-06-03 距今超过 2 天，任务跳过"


def test_signal_confirmation_dry_run_does_not_write(monkeypatch):
    import integrations.supabase_signal_pending as signal_pending

    writes: list[str] = []
    monkeypatch.setattr(signal_pending, "write_pending_signals", lambda *_args, **_kwargs: writes.append("insert"))
    monkeypatch.setattr(signal_pending, "load_pending_signals", lambda: [{"id": 1, "code": 1}])
    monkeypatch.setattr(
        signal_pending,
        "run_confirmation_cycle",
        lambda *_args, **_kwargs: ([{"id": 1, "status": "confirmed"}], [{"code": "000001"}]),
    )
    monkeypatch.setattr(signal_pending, "batch_update_signals", lambda *_args, **_kwargs: writes.append("update"))

    confirmed = signal_pending.run_step2_5(
        signal_date="2026-05-19",
        triggers={"sos": [("000001", 1.0)]},
        df_map={"000001": object()},
        dry_run=True,
    )

    assert confirmed == [{"code": "000001"}]
    assert writes == []


def test_signal_confirmation_update_failure_returns_no_confirmed(monkeypatch):
    import integrations.supabase_signal_pending as signal_pending

    monkeypatch.setattr(signal_pending, "write_pending_signals", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(signal_pending, "load_pending_signals", lambda: [{"id": 1, "code": 1}])
    monkeypatch.setattr(
        signal_pending,
        "run_confirmation_cycle",
        lambda *_args, **_kwargs: ([{"id": 1, "status": "confirmed"}], [{"code": "000001"}]),
    )
    monkeypatch.setattr(signal_pending, "batch_update_signals", lambda *_args, **_kwargs: False)

    confirmed = signal_pending.run_step2_5(
        signal_date="2026-05-19",
        triggers={"sos": [("000001", 1.0)]},
        df_map={"000001": object()},
    )

    assert confirmed == []


def test_confirmation_cycle_expires_missing_ohlcv_at_ttl():
    from core.signal_confirmation import run_confirmation_cycle

    updates, confirmed = run_confirmation_cycle(
        [
            {
                "id": 1,
                "code": 1,
                "signal_type": "sos",
                "signal_date": "2026-05-17",
                "days_elapsed": 1,
                "ttl_days": 2,
            }
        ],
        {},
        "2026-05-19",
    )

    assert confirmed == []
    assert updates == [
        {
            "id": 1,
            "status": "expired",
            "days_elapsed": 2,
            "confirm_reason": "OHLCV 缺失，TTL 2天已到",
            "expire_date": "2026-05-19",
        }
    ]


def test_persist_signal_observations_reports_write_failure(monkeypatch):
    import integrations.supabase_signal_feedback as signal_feedback
    import scripts.daily_job as daily_job

    monkeypatch.setattr(daily_job, "_latest_trade_date_str", lambda: "2026-05-25")
    monkeypatch.setattr(
        signal_feedback,
        "upsert_signal_observations",
        lambda _rows: (_ for _ in ()).throw(RuntimeError("upsert failed")),
    )

    ok = daily_job._persist_signal_observations(
        {"triggers": {"sos": [("000001", 1.0)]}},
        {"regime": "NEUTRAL"},
        [],
        None,
    )

    assert ok is False


def test_step3_input_preview_sends_summary_and_writes_artifact(monkeypatch, tmp_path):
    import scripts.step3_batch_report as step3

    sent: dict[str, str] = {}
    artifact_path = tmp_path / "step3_llm_input_preview.md"
    monkeypatch.setenv("STEP3_INPUT_PREVIEW_PATH", str(artifact_path))
    monkeypatch.setenv("FEISHU_INPUT_PREVIEW_AS_FILE", "1")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "YoungCan-Wang/WyckoffTradingAgent")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "456")

    def fake_send_feishu(_webhook_url, title, content):
        sent["title"] = title
        sent["content"] = content
        return True

    monkeypatch.setattr(step3, "send_feishu_notification", fake_send_feishu)
    monkeypatch.setattr(step3, "send_feishu_file", lambda path: path == str(artifact_path))

    ok, report = step3._send_input_preview(
        webhook_url="https://example.invalid/hook",
        model="gemini-test",
        system_prompt="SYSTEM PROMPT BODY",
        previews=[{"track": "Trend", "selected_count": 2, "user_message": "VERY LONG USER MESSAGE"}],
    )

    assert ok is True
    assert report == artifact_path.read_text(encoding="utf-8")
    assert "SYSTEM PROMPT BODY" in report
    assert "VERY LONG USER MESSAGE" in report
    assert "SYSTEM PROMPT BODY" not in sent["content"]
    assert "VERY LONG USER MESSAGE" not in sent["content"]
    assert "step3_llm_input_preview.md" in sent["content"]
    assert "input-preview-logs-456" in sent["content"]
    assert "https://github.com/YoungCan-Wang/WyckoffTradingAgent/actions/runs/123" in sent["content"]


def test_step3_input_preview_falls_back_to_original_when_file_send_fails(monkeypatch, tmp_path):
    import scripts.step3_batch_report as step3

    sent: dict[str, str] = {}
    monkeypatch.setenv("STEP3_INPUT_PREVIEW_PATH", str(tmp_path / "step3_llm_input_preview.md"))
    monkeypatch.setenv("FEISHU_INPUT_PREVIEW_AS_FILE", "1")
    monkeypatch.setattr(step3, "send_feishu_file", lambda _path: False)
    monkeypatch.setattr(
        step3,
        "send_feishu_notification",
        lambda _webhook_url, _title, content: sent.setdefault("content", content) is not None,
    )

    ok, _report = step3._send_input_preview(
        webhook_url="https://example.invalid/hook",
        model="gemini-test",
        system_prompt="SYSTEM PROMPT BODY",
        previews=[{"track": "Trend", "selected_count": 2, "user_message": "VERY LONG USER MESSAGE"}],
    )

    assert ok is True
    assert "SYSTEM PROMPT BODY" in sent["content"]
    assert "VERY LONG USER MESSAGE" in sent["content"]
