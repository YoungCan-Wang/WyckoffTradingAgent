from __future__ import annotations

import sys
from datetime import date

import pandas as pd


def test_preview_only_skips_persistence_and_keeps_llm_input_path(monkeypatch, tmp_path):
    import scripts.daily_job as daily_job
    import tools.report_parser as report_parser
    import workflows.daily_job_persistence as daily_persistence
    import workflows.daily_job_runtime as daily_runtime
    import workflows.daily_job_step2 as daily_step2
    import workflows.step2_signal_confirmation as signal_confirmation
    import workflows.step3_batch_report as step3_batch_report
    import workflows.wyckoff_funnel as wyckoff_funnel

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
        return [
            {
                "code": "000002",
                "name": "万科A",
                "tag": "EVR(二次确认)",
                "selection_source": "signal_confirmed",
                "confirm_reason": "守住 10.00",
                "candidate_lane": "mainline",
                "candidate_status": "主线买点候选",
            }
        ]

    def fake_run_step3(symbols_info, webhook_url, *_args, **_kwargs):
        captured["step3_symbols"] = [item["code"] for item in symbols_info]
        captured["step3_items"] = symbols_info
        captured["step3_webhook"] = webhook_url
        return True, "ok_preview", "# Step3 模型输入预演"

    monkeypatch.setenv("STEP3_SKIP_LLM", "1")
    monkeypatch.setenv("DAILY_JOB_SKIP_STEP4", "1")
    monkeypatch.setenv("DAILY_JOB_PREVIEW_ONLY", "1")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(sys, "argv", ["daily_job.py", "--logs", str(tmp_path / "preview.log")])
    monkeypatch.setattr(daily_runtime, "resolve_end_calendar_day", lambda: date(2026, 5, 31))
    monkeypatch.setattr(daily_runtime, "is_a_share_trading_day", lambda d: d == date(2026, 6, 1))
    monkeypatch.setattr(daily_step2, "latest_trade_date_str", lambda: "2026-05-19")
    monkeypatch.setattr(daily_persistence, "upsert_market_signal_daily", forbidden_write)
    monkeypatch.setattr(daily_persistence, "prepare_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_persistence, "upsert_recommendation_payload", forbidden_write)
    monkeypatch.setattr(daily_persistence, "mark_ai_recommendations", forbidden_write)
    monkeypatch.setattr(daily_step2, "run_springboard_scoring", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(wyckoff_funnel, "run", fake_run_funnel)
    monkeypatch.setattr(step3_batch_report, "run", fake_run_step3)
    monkeypatch.setattr(report_parser, "extract_operation_pool_codes", lambda **_kwargs: ["000001"])
    monkeypatch.setattr(signal_confirmation, "run_step2_5", fake_run_step2_5)

    assert daily_job.main() == 0
    assert captured["step2_webhook"] == ""
    assert captured["step2_notify"] is False
    assert captured["step2_return_details"] is True
    assert captured["signal_dry_run"] is True
    assert captured["step3_webhook"] == "https://example.invalid/webhook"
    assert captured["step3_symbols"] == ["000002"]
    assert captured["step3_items"][0]["selection_source"] == "signal_confirmed"
    assert captured["step3_items"][0]["confirm_reason"] == "守住 10.00"


def test_non_trading_skip_message_allows_when_next_day_trades(monkeypatch):
    import workflows.daily_job_runtime as daily_runtime

    monkeypatch.setattr(daily_runtime, "is_a_share_trading_day", lambda d: d == date(2026, 6, 1))

    assert daily_runtime.non_trading_skip_message(date(2026, 5, 31)) is None


def test_non_trading_skip_message_skips_when_next_day_is_closed(monkeypatch):
    import workflows.daily_job_runtime as daily_runtime

    monkeypatch.setattr(daily_runtime, "is_a_share_trading_day", lambda _day: False)

    msg = daily_runtime.non_trading_skip_message(date(2026, 5, 29))

    assert msg == "📅 明日 2026-05-30 非 A 股交易日，任务跳过"


def test_non_trading_skip_message_skips_holiday_before_next_trade(monkeypatch):
    import workflows.daily_job_runtime as daily_runtime

    monkeypatch.setattr(daily_runtime, "is_a_share_trading_day", lambda d: d == date(2026, 6, 3))

    msg = daily_runtime.non_trading_skip_message(date(2026, 5, 30))

    assert msg == "📅 明日 2026-05-31 非 A 股交易日，任务跳过"


def test_step4_candidate_confirmation_gate_accepts_only_confirmed():
    from workflows.step4_pipeline import is_confirmed_step4_candidate

    assert is_confirmed_step4_candidate({"tag": "SOS(确认)"})
    assert is_confirmed_step4_candidate({"status": "confirmed"})
    assert is_confirmed_step4_candidate({"selection_source": "signal_confirmed"})
    assert not is_confirmed_step4_candidate({"tag": "SOS（量价点火）"})


def test_step3_codes_filter_keeps_only_confirmed_candidates():
    from workflows.daily_job_step3 import filter_confirmed_step3_codes

    kept, blocked = filter_confirmed_step3_codes(
        ["000001", "000002", "000003"],
        [
            {"code": "000001", "signal_status": "confirmed"},
            {"code": "000002", "tag": "SOS（量价点火）"},
            {"code": "000003", "tag": "LPS(确认)"},
        ],
    )

    assert kept == ["000001", "000003"]
    assert blocked == ["000002"]


def test_recommendation_write_symbols_tracks_all_confirmed_candidates():
    from workflows.daily_job_persistence import recommendation_write_symbols

    rows = [
        {"code": "000001", "tag": "SOS（量价点火）"},
        {"code": "000002", "signal_status": "confirmed"},
        {
            "code": "000003",
            "tag": "LPS(确认)",
            "candidate_lane": "mainline",
            "candidate_status": "主线买点候选",
        },
        {
            "code": "000004",
            "signal_status": "confirmed",
            "strategic_theme": "机器人",
            "strategic_theme_score": 0.72,
            "strategic_stock_score": 0.66,
        },
        {
            "code": "000005",
            "signal_status": "confirmed",
            "candidate_lane": "mainline",
            "candidate_status": "过热不追",
        },
        {
            "code": "000006",
            "signal_status": "confirmed",
            "candidate_lane": "mainline",
            "candidate_status": "事件主题修复候选",
        },
    ]

    got = recommendation_write_symbols(rows)

    assert [row["code"] for row in got] == ["000002", "000003", "000004", "000005", "000006"]


def test_step3_review_symbols_keeps_only_strict_trade_candidates():
    from workflows.daily_job_persistence import step3_review_symbols

    rows = [
        {"code": "000001", "tag": "SOS（量价点火）"},
        {"code": "000002", "signal_status": "confirmed"},
        {
            "code": "000003",
            "tag": "LPS(确认)",
            "candidate_lane": "mainline",
            "candidate_status": "主线买点候选",
        },
        {
            "code": "000004",
            "signal_status": "confirmed",
            "strategic_theme": "机器人",
            "strategic_theme_score": 0.72,
            "strategic_stock_score": 0.66,
        },
        {
            "code": "000005",
            "signal_status": "confirmed",
            "candidate_lane": "mainline",
            "candidate_status": "过热不追",
        },
        {
            "code": "000006",
            "signal_status": "confirmed",
            "candidate_lane": "mainline",
            "candidate_status": "事件主题修复候选",
        },
    ]

    got = step3_review_symbols(rows)

    assert [row["code"] for row in got] == ["000003", "000004", "000006"]


def test_step3_review_symbols_adds_capped_repair_springboards(monkeypatch):
    from core.market_trade_mode import resolve_market_trade_mode
    from workflows.daily_job_persistence import step3_review_symbols

    monkeypatch.setenv("STEP3_REPAIR_REVIEW_SPRINGBOARD_CAP", "1")
    details = {
        "formal_triggers": {"sos": [("000007", 9.0), ("000008", 8.0)]},
        "springboard_map": {
            "sos:000007": {
                "springboard_met_count": 2,
                "springboard_grade": "A+C",
                "springboard_scored": True,
            },
            "sos:000008": {
                "springboard_met_count": 2,
                "springboard_grade": "B+C",
                "springboard_scored": True,
            },
        },
        "metrics": {
            "latest_close_map": {"000007": 12.3, "000008": 8.6},
            "accum_stage_map": {"000007": "Markup", "000008": "Markup"},
        },
        "name_map": {"000007": "全新好", "000008": "测试股"},
        "sector_map": {"000007": "测试行业", "000008": "测试行业"},
    }

    got = step3_review_symbols([], step2_details=details, trade_mode=resolve_market_trade_mode("PANIC_REPAIR"))

    assert len(got) == 1
    assert got[0]["code"] == "000007"
    assert got[0]["selection_source"] == "l4_springboard"
    assert got[0]["source_type"] == "repair_springboard_review"
    assert got[0]["signal_status"] == "confirmed"
    assert got[0]["candidate_status"] == "修复复核候选"
    assert got[0]["springboard_grade"] == "A+C"


def test_recommendation_write_symbols_tracks_market_blocked_springboard_candidates():
    from core.market_trade_mode import resolve_market_trade_mode
    from workflows.daily_job_persistence import recommendation_write_symbols

    trade_mode = resolve_market_trade_mode("PANIC_REPAIR")
    details = {
        "formal_triggers": {"sos": [("000007", 9.0)]},
        "springboard_map": {
            "sos:000007": {
                "springboard_met_count": 2,
                "springboard_grade": "A+C",
                "springboard_scored": True,
            }
        },
        "metrics": {
            "latest_close_map": {"000007": 12.3},
            "accum_stage_map": {"000007": "Markup"},
        },
        "name_map": {"000007": "全新好"},
        "sector_map": {"000007": "测试行业"},
    }

    got = recommendation_write_symbols([], step2_details=details, trade_mode=trade_mode)

    assert len(got) == 1
    assert got[0]["code"] == "000007"
    assert got[0]["candidate_status"] == "市场拦截观察"
    assert got[0]["selection_source"] == "l4_springboard:market_blocked"
    assert got[0]["market_regime"] == "PANIC_REPAIR"
    assert got[0]["tag"] == "SOS二次确认(A+C)"


def test_step3_springboard_updates_patch_recommendation_payload():
    from workflows.daily_signal_observations import apply_step3_springboard_updates

    payload = [
        {"code": 603373, "springboard_combo": "none", "springboard_scored": False},
        {"code": 301348, "springboard_combo": "none", "springboard_scored": False},
    ]

    apply_step3_springboard_updates(
        payload,
        {
            "603373": {"springboard_combo": "A+C", "springboard_scored": True},
            "301348": {"springboard_combo": "A+C", "springboard_scored": True},
        },
    )

    assert [row["springboard_combo"] for row in payload] == ["A+C", "A+C"]
    assert all(row["springboard_scored"] for row in payload)


def test_signal_confirmation_dry_run_does_not_write(monkeypatch):
    import workflows.step2_signal_confirmation as signal_confirmation

    writes: list[str] = []
    monkeypatch.setattr(
        signal_confirmation, "insert_pending_signal_rows", lambda *_args, **_kwargs: writes.append("insert")
    )
    monkeypatch.setattr(signal_confirmation, "load_pending_signals", lambda: [{"id": 1, "code": 1}])
    monkeypatch.setattr(
        signal_confirmation,
        "run_confirmation_cycle",
        lambda *_args, **_kwargs: ([{"id": 1, "status": "confirmed"}], [{"code": "000001"}]),
    )
    monkeypatch.setattr(signal_confirmation, "batch_update_signals", lambda *_args, **_kwargs: writes.append("update"))

    confirmed = signal_confirmation.run_step2_5(
        signal_date="2026-05-19",
        triggers={"sos": [("000001", 1.0)]},
        df_map={"000001": object()},
        dry_run=True,
    )

    assert confirmed == [{"code": "000001"}]
    assert writes == []


def test_step3_confirmed_preview_lists_signal_pending_source():
    from workflows.step3_operation_gate import build_signal_confirmed_preview

    preview = build_signal_confirmed_preview(
        pd.DataFrame(
            [
                {
                    "code": "603039",
                    "name": "泛微网络",
                    "input_order": 0,
                    "signal_status": "confirmed",
                    "signal_type": "evr",
                    "signal_date": "2026-06-11",
                    "confirm_date": "2026-06-12",
                    "confirm_reason": "守住 44.01，收盘 47.61",
                }
            ]
        )
    )

    assert "二次确认补充" in preview
    assert "603039 泛微网络" in preview
    assert "2026-06-11 → 2026-06-12" in preview


def test_shadow_observation_inputs_build_added_and_removed_sources():
    from workflows.daily_signal_observations import shadow_observation_inputs

    triggers, source_map, score_map = shadow_observation_inputs(
        {
            "shadow_added": ["000001"],
            "shadow_removed": ["000002"],
            "shadow_score_map": {"000001": 3.5, "000002": 1.2},
        }
    )

    assert triggers == {"shadow_added": [("000001", 3.5)], "shadow_removed": [("000002", 1.2)]}
    assert source_map == {"000001": "shadow_added", "000002": "shadow_removed"}
    assert score_map["000001"] == 3.5


def test_shadow_observation_inputs_sanitizes_scores():
    from workflows.daily_signal_observations import shadow_observation_inputs

    triggers, source_map, score_map = shadow_observation_inputs(
        {
            "shadow_added": ["000001", "000002"],
            "shadow_score_map": {"000001": "bad", "000002": float("nan"), "000003": float("inf")},
        }
    )

    assert triggers == {"shadow_added": [("000001", 0.0), ("000002", 0.0)]}
    assert source_map == {"000001": "shadow_added", "000002": "shadow_added"}
    assert score_map == {"000001": 0.0, "000002": 0.0, "000003": 0.0}


def test_persist_signal_observations_reports_write_failure(monkeypatch):
    import integrations.supabase_signal_feedback as signal_feedback
    from workflows.daily_signal_observations import persist_signal_observations

    monkeypatch.setattr(
        signal_feedback,
        "upsert_signal_observations",
        lambda _rows: (_ for _ in ()).throw(RuntimeError("upsert failed")),
    )

    ok = persist_signal_observations(
        {"triggers": {"sos": [("000001", 1.0)]}},
        {"regime": "NEUTRAL"},
        [],
        None,
        trade_date="2026-05-25",
    )

    assert ok is False


def test_step3_input_preview_sends_summary_and_writes_artifact(monkeypatch, tmp_path):
    import workflows.step3_delivery as delivery

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

    monkeypatch.setattr(delivery, "send_feishu_notification", fake_send_feishu)
    monkeypatch.setattr(delivery, "send_feishu_file", lambda path: path == str(artifact_path))

    ok, report = delivery.send_step3_input_preview(
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
    import workflows.step3_delivery as delivery

    sent: dict[str, str] = {}
    monkeypatch.setenv("STEP3_INPUT_PREVIEW_PATH", str(tmp_path / "step3_llm_input_preview.md"))
    monkeypatch.setenv("FEISHU_INPUT_PREVIEW_AS_FILE", "1")
    monkeypatch.setattr(delivery, "send_feishu_file", lambda _path: False)
    monkeypatch.setattr(
        delivery,
        "send_feishu_notification",
        lambda _webhook_url, _title, content: sent.setdefault("content", content) is not None,
    )

    ok, _report = delivery.send_step3_input_preview(
        webhook_url="https://example.invalid/hook",
        model="gemini-test",
        system_prompt="SYSTEM PROMPT BODY",
        previews=[{"track": "Trend", "selected_count": 2, "user_message": "VERY LONG USER MESSAGE"}],
    )

    assert ok is True
    assert "SYSTEM PROMPT BODY" in sent["content"]
    assert "VERY LONG USER MESSAGE" in sent["content"]
