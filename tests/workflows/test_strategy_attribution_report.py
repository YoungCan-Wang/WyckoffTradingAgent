from __future__ import annotations

import pytest


def test_attribution_report_no_write_prefers_user_client(monkeypatch):
    import workflows.strategy_attribution_report as report

    marker = object()
    monkeypatch.setattr(report, "create_user_read_client", lambda: marker)

    def fail_admin():
        raise AssertionError("admin client should not be used for no-write reports")

    monkeypatch.setattr(report, "create_admin_client", fail_admin)

    assert report.create_report_client(no_write=True) is marker


def test_attribution_report_no_write_falls_back_to_read_client(monkeypatch):
    import workflows.strategy_attribution_report as report

    marker = object()
    monkeypatch.setattr(report, "create_user_read_client", lambda: None)
    monkeypatch.setattr(report, "create_read_client", lambda: marker)

    assert report.create_report_client(no_write=True) is marker


def test_attribution_report_write_requires_server_context(monkeypatch):
    import workflows.strategy_attribution_report as report

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)
    with pytest.raises(PermissionError, match="server_job"):
        report.create_report_client(no_write=False)


def test_strategy_attribution_cli_loads_backtest_confirmation(tmp_path):
    import scripts.strategy_attribution_report as cli

    path = tmp_path / "backtest-confirmation.json"
    path.write_text('{"status":"pass","summary":"三周期回测通过"}', encoding="utf-8")

    assert cli.load_backtest_confirmation(str(path)) == {"status": "pass", "summary": "三周期回测通过"}


def test_attribution_report_groups_candidate_shadow_grade():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {
            "id": 1,
            "features_json": {
                "candidate_shadow_score": {
                    "score": 88.5,
                    "grade": "S",
                    "positive_tags": ["quality_breakout"],
                    "negative_tags": [],
                },
                "data_lineage": {
                    "coverage_score": 95,
                    "coverage_grade": "strong",
                    "evidence_keys": ["daily_signal", "price_action", "intraday_tail"],
                },
                "entry_quality": {
                    "score": 82.5,
                    "grade": "S",
                    "tag": "入场质量S(82.5)",
                    "risk_flags": [],
                },
            },
        },
        {
            "id": 2,
            "features_json": (
                '{"candidate_shadow_score":{"score":42,"grade":"D","negative_tags":["failed_breakout"]},'
                '"data_lineage":{"coverage_score":35,"coverage_grade":"thin","evidence_keys":["daily_signal"]},'
                '"entry_quality":{"score":38,"grade":"D","risk_flags":["弱于指数","追高延展"]}}'
            ),
        },
    ]
    outcomes = [
        {"observation_id": 1, "horizon_days": 5, "return_pct": 6.0, "max_drawdown_pct": -2.0},
        {"observation_id": 2, "horizon_days": 5, "return_pct": -5.0, "max_drawdown_pct": -7.0},
    ]

    joined = stats_mod.join_outcomes(outcomes, observations)
    stats = stats_mod.score_stats_json(joined, [5])

    assert joined[0]["candidate_shadow_score"] == 88.5
    assert joined[0]["candidate_shadow_grade"] == "S"
    assert joined[1]["candidate_shadow_grade"] == "D"
    assert joined[0]["entry_quality_score"] == 82.5
    assert joined[0]["entry_quality_grade"] == "S"
    assert joined[1]["entry_quality_risk_flags"] == ["弱于指数", "追高延展"]
    assert stats["_candidate_shadow_grade"]["5"]["S"]["win_rate_pct"] == 100.0
    assert stats["_candidate_shadow_grade"]["5"]["D"]["big_loss_rate_pct"] == 100.0
    assert stats["_entry_quality_grade"]["5"]["S"]["avg_return_pct"] == 6.0
    assert stats["_entry_quality_grade"]["5"]["D"]["big_loss_rate_pct"] == 100.0
    assert joined[0]["data_lineage_coverage_grade"] == "strong"
    assert joined[1]["data_lineage_evidence_keys"] == ["daily_signal"]
    assert stats["_data_lineage"]["coverage_grade"]["5"]["strong"]["win_rate_pct"] == 100.0
    assert stats["_data_lineage"]["coverage_grade"]["5"]["thin"]["big_loss_rate_pct"] == 100.0
    assert stats["_data_lineage"]["evidence_key"]["5"]["intraday_tail"]["avg_return_pct"] == 6.0
    assert stats["_data_lineage"]["coverage_summary"]["5"]["avg_coverage_score"] == 65.0


def test_attribution_shadow_latest_uses_compact_summary():
    import workflows.strategy_attribution_stats as stats_mod

    shadow_rows = [
        {
            "trade_date": "2026-06-30",
            "regime": "RISK_ON",
            "schema_version": "shadow_policy_v2",
            "snapshot_level": "summary",
            "base_selected": ["000001", "000002"],
            "shadow_selected": ["000002", "000003"],
            "diff_added": ["000003"],
            "diff_removed": ["000001"],
            "registry_snapshot": [{"signal_type": "sos"}],
            "health_snapshot": [{"signal_type": "sos"}],
            "selection_summary": {"base_count": 2, "shadow_count": 2, "diff_added_count": 1},
            "policy_summary": {"signal_weight_count": 2},
            "registry_summary": {"count": 12},
            "health_summary": {"count": 30},
        }
    ]

    stats = stats_mod.shadow_stats(shadow_rows, [], [5])

    assert stats["latest"] == {
        "trade_date": "2026-06-30",
        "regime": "RISK_ON",
        "schema_version": "shadow_policy_v2",
        "snapshot_level": "summary",
        "selection_summary": {"base_count": 2, "shadow_count": 2, "diff_added_count": 1},
        "diff_added_sample": ["000003"],
        "diff_removed_sample": ["000001"],
        "policy_summary": {"signal_weight_count": 2},
        "registry_summary": {"count": 12},
        "health_summary": {"count": 30},
    }


def test_attribution_shadow_latest_uses_newest_trade_date():
    import workflows.strategy_attribution_stats as stats_mod

    shadow_rows = [
        {"trade_date": "2026-06-01", "regime": "NEUTRAL", "diff_added": ["000001"], "diff_removed": []},
        {"trade_date": "2026-06-30", "regime": "RISK_ON", "diff_added": [], "diff_removed": ["000002"]},
    ]

    stats = stats_mod.shadow_stats(shadow_rows, [], [5])

    assert stats["latest"]["trade_date"] == "2026-06-30"
    assert stats["latest"]["regime"] == "RISK_ON"


def test_attribution_policy_governor_promotes_shadow_review_and_signal_actions():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {"id": 1, "trade_date": "2026-06-01", "code": "000001", "signal_type": "lps"},
        {"id": 2, "trade_date": "2026-06-01", "code": "000002", "signal_type": "sos"},
    ]
    outcomes = [
        {
            "observation_id": 1,
            "trade_date": "2026-06-01",
            "code": "000001",
            "horizon_days": 5,
            "return_pct": -3.0,
            "max_drawdown_pct": -11.0,
        }
        for _ in range(10)
    ]
    outcomes.extend(
        {
            "observation_id": 2,
            "trade_date": "2026-06-01",
            "code": "000002",
            "horizon_days": 5,
            "return_pct": 3.0,
            "max_drawdown_pct": -3.0,
        }
        for _ in range(10)
    )
    shadow_runs = [
        {
            "trade_date": "2026-06-01",
            "diff_added": ["000002"],
            "diff_removed": ["000001"],
        }
        for _ in range(10)
    ]

    payload = stats_mod.build_strategy_attribution_payload(
        report_date=stats_mod.date(2026, 7, 4),
        market="cn",
        window_start=stats_mod.date(2026, 5, 5),
        window_end=stats_mod.date(2026, 7, 4),
        horizons=[5],
        observations=observations,
        outcomes=outcomes,
        shadow_runs=shadow_runs,
    )

    governor = payload["shadow_diff_stats_json"]["policy_governor"]
    rows = payload["recommendations_json"]
    assert governor["status"] == "candidate"
    assert governor["mode_recommendation"] == "review_promote_dynamic_policy"
    assert governor["next_action"] == "run_backtest_confirmation"
    assert governor["next_action_summary"] == "shadow 新增组已跑赢移除组；先补齐最新回测确认，再进入人工晋级评审。"
    assert governor["promotion_status"] == "manual_review_required"
    assert governor["auto_apply"] is False
    assert governor["formal_dynamic_allowed"] is False
    assert governor["formal_dynamic_approval"] == "backtest_confirmation_required"
    assert governor["formal_dynamic_block_reason"] == "backtest_confirmation_required"
    assert {row["key"]: row["status"] for row in governor["promotion_checklist"]} == {
        "shadow_sample": "pass",
        "shadow_performance": "pass",
        "signal_actions": "review",
        "selection_actions": "pass",
        "backtest_confirmation": "review",
    }
    assert {row["type"] for row in rows} >= {"policy_governor", "downweight", "upweight"}


def test_attribution_policy_governor_accepts_structured_backtest_confirmation():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {"id": 1, "trade_date": "2026-06-01", "code": "000001", "signal_type": "lps"},
        {"id": 2, "trade_date": "2026-06-01", "code": "000002", "signal_type": "sos"},
    ]
    outcomes = [
        {
            "observation_id": 1,
            "trade_date": "2026-06-01",
            "code": "000001",
            "horizon_days": 5,
            "return_pct": -3.0,
            "max_drawdown_pct": -11.0,
        }
        for _ in range(10)
    ]
    outcomes.extend(
        {
            "observation_id": 2,
            "trade_date": "2026-06-01",
            "code": "000002",
            "horizon_days": 5,
            "return_pct": 3.0,
            "max_drawdown_pct": -3.0,
        }
        for _ in range(10)
    )
    shadow_runs = [
        {"trade_date": "2026-06-01", "diff_added": ["000002"], "diff_removed": ["000001"]} for _ in range(10)
    ]

    payload = stats_mod.build_strategy_attribution_payload(
        report_date=stats_mod.date(2026, 7, 4),
        market="cn",
        window_start=stats_mod.date(2026, 5, 5),
        window_end=stats_mod.date(2026, 7, 4),
        horizons=[5],
        observations=observations,
        outcomes=outcomes,
        shadow_runs=shadow_runs,
        backtest_confirmation_json={
            "status": "pass",
            "strategy_policy_ready": True,
            "strategy_policy": "lps×0.50↓；trend_pullback×0.60↓",
            "summary": "三周期回测确认正收益，允许进入人工晋级评审。",
        },
    )

    governor = payload["shadow_diff_stats_json"]["policy_governor"]
    checklist = {row["key"]: row for row in governor["promotion_checklist"]}

    assert governor["formal_dynamic_allowed"] is False
    assert governor["next_action"] == "review_policy_actions"
    assert governor["formal_dynamic_approval"] == "signal_actions_review_required"
    assert governor["formal_dynamic_block_reason"] == "signal_actions_review_required"
    assert checklist["backtest_confirmation"]["status"] == "pass"
    assert checklist["backtest_confirmation"]["summary"] == "三周期回测确认正收益，允许进入人工晋级评审。"


def test_attribution_policy_governor_rejects_stale_backtest_confirmation():
    from core.strategy_policy_governor import build_strategy_policy_governor

    governor = build_strategy_policy_governor(
        signal_stats_json={},
        score_bucket_stats_json={},
        shadow_diff_stats_json={
            "count": 10,
            "outcome_stats": {
                "5": {
                    "added": {
                        "matched_outcomes": 3,
                        "avg_return_pct": 3.0,
                        "win_rate_pct": 70.0,
                        "avg_drawdown_pct": -5.0,
                    },
                    "removed": {
                        "matched_outcomes": 3,
                        "avg_return_pct": -1.0,
                        "win_rate_pct": 40.0,
                        "avg_drawdown_pct": -8.0,
                    },
                }
            },
        },
        backtest_confirmation_json={"status": "pass", "summary": "旧 artifact 回测通过。"},
        horizons=[5],
    )

    checklist = {row["key"]: row for row in governor["promotion_checklist"]}
    assert governor["formal_dynamic_approval"] == "backtest_policy_evidence_required"
    assert governor["formal_dynamic_block_reason"] == "backtest_policy_evidence_required"
    assert governor["next_action"] == "run_backtest_confirmation"
    assert checklist["backtest_confirmation"]["status"] == "review"
    assert checklist["backtest_confirmation"]["summary"] == "回测结果仍需人工复核：缺少策略治理口径证据"


def test_attribution_policy_governor_allows_formal_dynamic_with_manual_approval():
    from core.strategy_policy_governor import build_strategy_policy_governor

    base_kwargs = {
        "signal_stats_json": {},
        "score_bucket_stats_json": {},
        "shadow_diff_stats_json": {
            "count": 10,
            "outcome_stats": {
                "5": {
                    "added": {
                        "matched_outcomes": 3,
                        "avg_return_pct": 3.0,
                        "win_rate_pct": 70.0,
                        "avg_drawdown_pct": -5.0,
                    },
                    "removed": {
                        "matched_outcomes": 3,
                        "avg_return_pct": -1.0,
                        "win_rate_pct": 40.0,
                        "avg_drawdown_pct": -8.0,
                    },
                }
            },
        },
        "backtest_confirmation_json": {
            "status": "pass",
            "strategy_policy_ready": True,
            "summary": "回测确认通过。",
        },
        "horizons": [5],
    }

    pending = build_strategy_policy_governor(**base_kwargs)
    approved = build_strategy_policy_governor(
        **base_kwargs,
        formal_dynamic_approval_json={
            "approved": True,
            "approved_by": "youngcan",
            "reason": "shadow 与回测均通过，人工批准进入正式 dynamic。",
        },
    )

    assert pending["formal_dynamic_allowed"] is False
    assert pending["formal_dynamic_approval"] == "manual_review_required"
    assert pending["next_action"] == "manual_review_dynamic_on"
    assert approved["formal_dynamic_allowed"] is True
    assert approved["formal_dynamic_approval"] == "manual_approved"
    assert approved["formal_dynamic_block_reason"] == ""
    assert approved["promotion_status"] == "manual_approved"
    assert approved["next_action"] == "formal_dynamic_approved"
    assert approved["formal_dynamic_manual_approval"]["approved_by"] == "youngcan"


def test_attribution_policy_governor_emits_scoped_context_weight():
    import workflows.strategy_attribution_stats as stats_mod
    from core.strategy_policy_governor import signal_weight_multipliers_from_rows

    observations = [
        {
            "id": idx,
            "trade_date": "2026-06-01",
            "code": f"00000{idx}",
            "signal_type": "lps",
            "regime": "RISK_ON",
            "candidate_lane": "trend_pullback",
            "entry_type": "wyckoff_structure",
        }
        for idx in range(1, 6)
    ]
    outcomes = [
        {
            "observation_id": idx,
            "trade_date": "2026-06-01",
            "code": f"00000{idx}",
            "horizon_days": 5,
            "return_pct": -2.0,
            "max_drawdown_pct": -11.0,
        }
        for idx in range(1, 6)
    ]

    payload = stats_mod.build_strategy_attribution_payload(
        report_date=stats_mod.date(2026, 7, 4),
        market="cn",
        window_start=stats_mod.date(2026, 5, 5),
        window_end=stats_mod.date(2026, 7, 4),
        horizons=[5],
        observations=observations,
        outcomes=outcomes,
        shadow_runs=[],
    )

    context_rows = payload["signal_context_stats_json"]["5"]
    weights = signal_weight_multipliers_from_rows(payload["recommendations_json"], horizon=5)

    assert len(context_rows) == 1
    expected_context = {
        "signal_type": "lps",
        "regime": "RISK_ON",
        "candidate_lane": "trend_pullback",
        "entry_type": "wyckoff_structure",
        "count": 5,
        "avg_return_pct": -2.0,
        "win_rate_pct": 0.0,
        "big_loss_rate_pct": 0.0,
        "avg_drawdown_pct": -11.0,
    }
    for key, value in expected_context.items():
        assert context_rows[0][key] == value
    assert weights == {"lps|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure": 0.75}


def test_attribution_policy_governor_emits_selection_source_actions():
    import workflows.strategy_attribution_stats as stats_mod
    from core.strategy_policy_governor import signal_weight_multipliers_from_rows
    from workflows.strategy_attribution_execution import attribution_execution_state, attribution_operations_brief

    observations = [
        {
            "id": idx,
            "trade_date": "2026-06-01",
            "code": f"00000{idx}",
            "signal_type": "shadow_added",
            "selection_mode": "candidate_lane_shadow",
            "strategy_version": "candidate_lane_v1",
            "candidate_lane": "trend_pullback",
            "entry_type": "wyckoff_structure",
        }
        for idx in range(1, 6)
    ]
    outcomes = [
        {
            "observation_id": idx,
            "trade_date": "2026-06-01",
            "code": f"00000{idx}",
            "horizon_days": 5,
            "return_pct": -2.0,
            "max_drawdown_pct": -11.0,
        }
        for idx in range(1, 6)
    ]

    payload = stats_mod.build_strategy_attribution_payload(
        report_date=stats_mod.date(2026, 7, 4),
        market="cn",
        window_start=stats_mod.date(2026, 5, 5),
        window_end=stats_mod.date(2026, 7, 4),
        horizons=[5],
        observations=observations,
        outcomes=outcomes,
        shadow_runs=[],
    )

    governor = payload["shadow_diff_stats_json"]["policy_governor"]
    rows = payload["recommendations_json"]

    assert any(row["target"] == "candidate_lane=trend_pullback" for row in governor["selection_actions"])
    assert any(row["type"] == "selection_downweight" for row in rows)
    assert "候选源降级复核" in governor["summary"]
    assert "candidate_lane=trend_pullback" in governor["summary"]
    assert signal_weight_multipliers_from_rows(rows, horizon=5) == {}
    execution = attribution_execution_state(governor, rows)
    operations = attribution_operations_brief(payload["shadow_diff_stats_json"], execution)
    assert execution["signal_action_count"] == 0
    assert execution["selection_action_count"] == 4
    assert "candidate_lane=trend_pullback" in operations["selection_action_summary"]
    assert "候选源治理 4 项" in operations["operator_summary"]


def test_attribution_policy_governor_blocks_formal_dynamic_on_selection_actions():
    from core.strategy_policy_governor import build_strategy_policy_governor

    governor = build_strategy_policy_governor(
        signal_stats_json={},
        score_bucket_stats_json={
            "_candidate_lane": {
                "5": {
                    "trend_pullback": {
                        "count": 5,
                        "avg_return_pct": -2.0,
                        "win_rate_pct": 20.0,
                        "big_loss_rate_pct": 40.0,
                        "avg_drawdown_pct": -11.0,
                    }
                }
            }
        },
        shadow_diff_stats_json={
            "count": 10,
            "outcome_stats": {
                "5": {
                    "added": {
                        "matched_outcomes": 3,
                        "avg_return_pct": 3.0,
                        "win_rate_pct": 70.0,
                        "avg_drawdown_pct": -5.0,
                    },
                    "removed": {
                        "matched_outcomes": 3,
                        "avg_return_pct": -1.0,
                        "win_rate_pct": 40.0,
                        "avg_drawdown_pct": -8.0,
                    },
                }
            },
        },
        backtest_confirmation_json={
            "status": "pass",
            "strategy_policy_ready": True,
            "strategy_policy": "candidate_lane=trend_pullback×0.50↓",
            "summary": "回测确认通过。",
        },
        horizons=[5],
    )

    checklist = {row["key"]: row for row in governor["promotion_checklist"]}
    assert governor["status"] == "candidate"
    assert governor["formal_dynamic_allowed"] is False
    assert governor["formal_dynamic_approval"] == "selection_actions_review_required"
    assert governor["formal_dynamic_block_reason"] == "selection_actions_review_required"
    assert governor["next_action"] == "review_policy_actions"
    assert "复核信号调权/候选源治理" in governor["next_action_summary"]
    assert checklist["selection_actions"]["status"] == "review"
    assert checklist["backtest_confirmation"]["status"] == "pass"


def test_attribution_console_summary_surfaces_policy_governor(monkeypatch):
    import workflows.strategy_attribution_report as report_mod

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    recommendations = [
        {"type": "policy_governor", "target": "dynamic_policy", "horizon": "5", "reason": "{}"},
        {"type": "downweight", "target": "lps", "horizon": "5", "reason": '{"weight_multiplier":0.5}'},
    ]
    report = {
        "market": "cn",
        "report_date": "2026-07-04",
        "shadow_diff_stats_json": {
            "count": 24,
            "policy_governor": {
                "status": "candidate",
                "mode_recommendation": "review_promote_dynamic_policy",
                "next_action": "manual_review_dynamic_on",
                "next_action_summary": "shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。",
                "promotion_status": "manual_review_required",
                "formal_dynamic_allowed": False,
                "formal_dynamic_block_reason": "manual_review_required",
                "promotion_checklist": [
                    {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                    {"key": "backtest_confirmation", "status": "review", "summary": "need backtest"},
                ],
                "auto_apply": False,
                "summary": "shadow 新增组显著优于移除组",
            },
        },
        "recommendations_json": recommendations,
    }
    report_mod.attach_policy_execution_state(report)

    got = report_mod.build_console_summary(report, written=False)

    assert got == {
        "market": "cn",
        "report_date": "2026-07-04",
        "written": False,
        "policy_status": "candidate",
        "mode_recommendation": "review_promote_dynamic_policy",
        "next_action": "manual_review_dynamic_on",
        "next_action_summary": "shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。",
        "promotion_status": "manual_review_required",
        "auto_apply": False,
        "policy_summary": "shadow 新增组显著优于移除组",
        "policy_display": {
            "status": "可进入人工晋级评审",
            "mode_recommendation": "评审是否切 on",
            "next_action": "进入人工晋级评审（非正式生效）",
            "promotion_status": "需人工复核",
            "auto_apply": "否",
        },
        "shadow_runs": 24,
        "execution_policy": "shadow",
        "execution_horizon": "5",
        "execution_scope": "tail_buy_and_funnel_shadow",
        "execution_summary": {
            "active_scope": "尾盘+漏斗shadow",
            "promotion_status": "需人工复核",
            "next_action": "进入人工晋级评审（非正式生效）",
            "formal_dynamic": "未进正式漏斗(人工复核未完成)",
            "summary": (
                "h=5 信号级调权会影响尾盘策略，并用于漏斗动态策略 shadow 对照。 "
                "策略治理器不会自动把 FUNNEL_DYNAMIC_POLICY 晋级到 on；"
                "下一步是进入人工晋级评审（非正式生效）（追证据字段: manual_review_dynamic_on）。"
            ),
        },
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "manual_review_required",
        "active_scope": "尾盘+漏斗shadow",
        "tail_buy_weights_active": True,
        "funnel_shadow_weights_active": True,
        "funnel_formal_weights_active": False,
        "signal_action_count": 1,
        "selection_action_count": 0,
        "selection_action_summary": "候选源治理=无",
        "promotion_checklist_summary": "样本=通过；回测=待复核",
        "promotion_blockers": [{"key": "backtest_confirmation", "status": "review", "summary": "need backtest"}],
        "backtest_confirmation": {"key": "backtest_confirmation", "status": "review", "summary": "need backtest"},
        "operator_summary": (
            "下一步=shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。；"
            "作用范围=尾盘+漏斗shadow；正式dynamic=未进正式漏斗(人工复核未完成)；"
            "回测确认=待复核(need backtest)；"
            "Shadow=暂无最新对照；本期 1 个 scoped 调权：lps×0.50；候选源治理=无"
        ),
    }
    assert "manual_review_required" not in got["operator_summary"]


def test_attribution_markdown_surfaces_execution_state(monkeypatch):
    import workflows.strategy_attribution_report as report_mod

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    report = {
        "market": "cn",
        "report_date": "2026-07-04",
        "window_start": "2026-05-05",
        "window_end": "2026-07-04",
        "shadow_diff_stats_json": {
            "count": 24,
            "avg_added": 0.42,
            "avg_removed": 12.83,
            "outcome_stats": {"5": {"added": {"avg_return_pct": 2.5}, "removed": {"avg_return_pct": -3.45}}},
            "policy_governor": {
                "status": "candidate",
                "mode_recommendation": "review_promote_dynamic_policy",
                "next_action": "manual_review_dynamic_on",
                "next_action_summary": "shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。",
                "promotion_status": "manual_review_required",
                "promotion_checklist": [
                    {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                    {"key": "backtest_confirmation", "status": "review", "summary": "need backtest"},
                ],
                "auto_apply": False,
                "summary": "shadow 新增组显著优于移除组",
            },
        },
        "recommendations_json": [
            {
                "type": "downweight",
                "target": "lps",
                "horizon": "5",
                "reason": (
                    '{"weight_multiplier":0.5,'
                    '"scope":{"regime":"RISK_ON","lane":"trend_pullback","entry_type":"wyckoff_structure"}}'
                ),
            }
        ],
    }
    report_mod.attach_policy_execution_state(report)

    markdown = report_mod.build_report_markdown(report)

    assert "## 调权执行状态" in markdown
    assert "- 下一步动作: 进入人工晋级评审（非正式生效） (`manual_review_dynamic_on`)" in markdown
    assert "- 下一步说明: shadow 新增组已跑赢移除组" in markdown
    assert "- 晋级状态: 需人工复核 (`manual_review_required`)" in markdown
    assert "### 晋级检查" in markdown
    assert "样本 (`shadow_sample`): 通过 (`pass`)" in markdown
    assert "- 漏斗动态策略: 正式调权(on)" in markdown
    assert "- 执行周期: `h=5`" in markdown
    assert "- 当前生效范围: `尾盘+漏斗shadow`" in markdown
    assert "- 底层范围: 尾盘+漏斗shadow (`tail_buy_and_funnel_shadow`)" in markdown
    assert "- 可执行调权: `1`" in markdown
    assert "下一步是进入人工晋级评审（非正式生效）（追证据字段: manual_review_dynamic_on）" in markdown
    assert "## 运营复盘" in markdown
    assert "- 操作摘要: 下一步=shadow 新增组已跑赢移除组" in markdown
    assert "作用范围=尾盘+漏斗shadow" in markdown
    assert "- 本期可执行调权:" in markdown
    assert "`lps[regime=RISK_ON, lane=trend_pullback, entry=wyckoff_structure]`" in markdown


def test_attribution_execution_state_counts_focus_horizon_only(monkeypatch):
    import workflows.strategy_attribution_report as report_mod

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    report = {
        "market": "cn",
        "report_date": "2026-07-04",
        "shadow_diff_stats_json": {
            "policy_governor": {
                "status": "candidate",
                "mode_recommendation": "review_promote_dynamic_policy",
                "auto_apply": False,
                "summary": "shadow 新增组显著优于移除组",
                "horizon": "5",
            },
        },
        "recommendations_json": [
            {"type": "downweight", "target": "evr", "horizon": "1", "reason": '{"weight_multiplier":0.5}'},
            {"type": "downweight", "target": "lps", "horizon": "5", "reason": '{"weight_multiplier":0.5}'},
            {"type": "upweight", "target": "launchpad", "horizon": "5", "reason": '{"weight_multiplier":1.2}'},
            {"type": "downweight", "target": "sos", "horizon": "10", "reason": '{"weight_multiplier":0.5}'},
        ],
    }
    report_mod.attach_policy_execution_state(report)

    state = report["shadow_diff_stats_json"]["policy_execution_state"]
    operations = report["shadow_diff_stats_json"]["policy_operations_brief"]

    assert state["horizon"] == "5"
    assert state["signal_action_count"] == 2
    assert state["promotion_status"] == "unknown"
    assert state["scope"] == "tail_buy_and_funnel_shadow"
    assert state["action_details"][0]["label"] == "lps"
    assert state["action_details"][1]["label"] == "launchpad"
    assert operations["action_count"] == 2
    assert operations["operator_summary"].startswith("下一步=-；作用范围=尾盘+漏斗shadow")
    assert operations["backtest_confirmation_text"] == "缺失(缺少检查项)"
    assert operations["promotion_checklist_summary"] == "晋级清单=无"


def test_attribution_execution_state_blocks_formal_on_without_governor_approval(monkeypatch):
    from workflows.strategy_attribution_execution import attribution_execution_state

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    governor = {
        "horizon": "5",
        "next_action": "keep_static_policy",
        "promotion_status": "do_not_promote",
        "auto_apply": False,
    }

    state = attribution_execution_state(
        governor,
        [{"type": "downweight", "target": "lps", "horizon": "5", "reason": '{"weight_multiplier":0.5}'}],
    )

    assert state["funnel_dynamic_policy"] == "on"
    assert state["scope"] == "tail_buy_and_funnel_shadow"
    assert state["formal_dynamic_allowed"] is False
    assert state["formal_dynamic_block_reason"] == "next_action=keep_static_policy"
    assert state["active_scope"] == "尾盘+漏斗shadow"
    assert state["tail_buy_weights_active"] is True
    assert state["funnel_shadow_weights_active"] is True
    assert state["funnel_formal_weights_active"] is False
    assert "未批准进入漏斗正式 dynamic" in state["summary"]


def test_attribution_execution_state_blocks_explicit_formal_on_without_checklist(monkeypatch):
    from workflows.strategy_attribution_execution import attribution_execution_state

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    governor = {
        "horizon": "5",
        "next_action": "manual_review_dynamic_on",
        "promotion_status": "manual_review_required",
        "formal_dynamic_allowed": True,
        "auto_apply": False,
    }

    state = attribution_execution_state(
        governor,
        [{"type": "upweight", "target": "sos", "horizon": "5", "reason": '{"weight_multiplier":1.15}'}],
    )

    assert state["scope"] == "tail_buy_and_funnel_shadow"
    assert state["formal_dynamic_allowed"] is False
    assert state["formal_dynamic_block_reason"] == "promotion_checklist=missing"
    assert state["active_scope"] == "尾盘+漏斗shadow"
    assert state["funnel_formal_weights_active"] is False


def test_attribution_execution_state_allows_formal_on_with_explicit_approval_and_passed_checklist(monkeypatch):
    from workflows.strategy_attribution_execution import attribution_execution_state

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    governor = {
        "horizon": "5",
        "next_action": "manual_review_dynamic_on",
        "promotion_status": "manual_review_required",
        "formal_dynamic_allowed": True,
        "auto_apply": False,
        "promotion_checklist": [
            {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
            {"key": "shadow_performance", "status": "pass", "summary": "performance ok"},
            {"key": "signal_actions", "status": "pass", "summary": "signals ok"},
            {"key": "backtest_confirmation", "status": "pass", "summary": "backtest ok"},
        ],
    }

    state = attribution_execution_state(
        governor,
        [{"type": "upweight", "target": "sos", "horizon": "5", "reason": '{"weight_multiplier":1.15}'}],
    )

    assert state["scope"] == "tail_buy_and_funnel"
    assert state["formal_dynamic_allowed"] is True
    assert state["formal_dynamic_block_reason"] == ""
    assert state["active_scope"] == "尾盘+正式漏斗"
    assert state["funnel_formal_weights_active"] is True


def test_attribution_policy_governor_keeps_shadow_reject_when_signal_actions_exist():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {"id": 1, "trade_date": "2026-06-01", "code": "000001", "signal_type": "lps"},
        {"id": 2, "trade_date": "2026-06-01", "code": "000002", "signal_type": "sos"},
    ]
    outcomes = [
        {
            "observation_id": 1,
            "trade_date": "2026-06-01",
            "code": "000001",
            "horizon_days": 5,
            "return_pct": 4.0,
            "max_drawdown_pct": -2.0,
        }
        for _ in range(10)
    ]
    outcomes.extend(
        {
            "observation_id": 2,
            "trade_date": "2026-06-01",
            "code": "000002",
            "horizon_days": 5,
            "return_pct": -4.0,
            "max_drawdown_pct": -12.0,
        }
        for _ in range(10)
    )
    shadow_runs = [
        {
            "trade_date": "2026-06-01",
            "diff_added": ["000002"],
            "diff_removed": ["000001"],
        }
        for _ in range(10)
    ]

    payload = stats_mod.build_strategy_attribution_payload(
        report_date=stats_mod.date(2026, 7, 4),
        market="cn",
        window_start=stats_mod.date(2026, 5, 5),
        window_end=stats_mod.date(2026, 7, 4),
        horizons=[5],
        observations=observations,
        outcomes=outcomes,
        shadow_runs=shadow_runs,
    )

    governor = payload["shadow_diff_stats_json"]["policy_governor"]
    rows = payload["recommendations_json"]
    assert governor["status"] == "reject"
    assert governor["mode_recommendation"] == "keep_static_policy"
    assert governor["next_action"] == "keep_static_policy"
    assert governor["promotion_status"] == "do_not_promote"
    assert {row["key"]: row["status"] for row in governor["promotion_checklist"]}["shadow_performance"] == "fail"
    assert {row["type"] for row in rows} >= {"policy_governor", "downweight", "upweight"}


def test_signal_weight_multipliers_ignore_governor_and_wrong_horizon():
    from core.strategy_policy_governor import signal_weight_multipliers_from_rows

    rows = [
        {"type": "policy_governor", "target": "dynamic_policy", "horizon": "5", "reason": "{}"},
        {
            "type": "downweight",
            "horizon": "5",
            "target": "lps",
            "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
        },
        {
            "type": "upweight",
            "horizon": "5",
            "target": "sos",
            "reason": '{"action":"upweight","horizon":"5","target":"sos","weight_multiplier":1.15}',
        },
        {
            "type": "downweight",
            "horizon": "10",
            "target": "evr",
            "reason": '{"action":"downweight","horizon":"10","target":"evr","weight_multiplier":0.75}',
        },
        {
            "type": "downweight",
            "horizon": "5",
            "target": "unknown",
            "reason": '{"action":"downweight","horizon":"5","target":"unknown","weight_multiplier":0.5}',
        },
        {
            "type": "downweight",
            "horizon": "5",
            "target": "evr",
            "reason": (
                '{"action":"downweight","horizon":"5","target":"evr","weight_multiplier":0.75,'
                '"scope":{"regime":"RISK_ON","lane":"trend_pullback","entry_type":"wyckoff_structure"}}'
            ),
        },
        {
            "type": "selection_downweight",
            "horizon": "5",
            "target": "candidate_lane=trend_pullback",
            "reason": (
                '{"action":"selection_downweight","horizon":"5","target":"candidate_lane=trend_pullback",'
                '"weight_multiplier":0.5}'
            ),
        },
    ]

    assert signal_weight_multipliers_from_rows(rows, horizon=5) == {
        "evr|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure": 0.75,
        "lps": 0.5,
        "sos": 1.15,
    }


def test_attribution_observation_coverage_marks_current_and_legacy():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {
            "id": 1,
            "trade_date": "2026-06-29",
            "signal_type": "launchpad",
            "selection_mode": "candidate_lane_shadow",
            "strategy_version": "candidate_lane_v1",
            "candidate_lane": "launchpad",
            "features_json": {"candidate_shadow_score": {"version": "candidate_shadow_score_v1"}},
        },
        {
            "id": 2,
            "trade_date": "2026-06-01",
            "signal_type": "sos",
            "selection_mode": "shadow",
            "strategy_version": "legacy_layered",
        },
    ]
    outcomes = [{"observation_id": 1, "horizon_days": 1, "return_pct": 1.2}]

    coverage = stats_mod.observation_coverage_stats(observations, outcomes, [1, 3])

    launchpad = coverage["signal_type"]["launchpad"]
    legacy = coverage["selection_mode"]["shadow"]
    assert launchpad["observations"] == 1
    assert launchpad["h1_coverage_pct"] == 100.0
    assert launchpad["h3_coverage_pct"] == 0.0
    assert launchpad["features_coverage_pct"] == 100.0
    assert launchpad["current_like_pct"] == 100.0
    assert legacy["legacy_like_pct"] == 100.0


def test_attribution_stats_ignore_nonfinite_scores_and_returns():
    import workflows.strategy_attribution_stats as stats_mod

    observations = [
        {"id": 1, "priority_score": float("inf"), "features_json": {"candidate_shadow_score": {"score": float("inf")}}},
        {"id": 2, "priority_score": 10.0},
        {"id": 3, "priority_score": 20.0},
        {"id": 4, "priority_score": 30.0},
        {"id": 5, "priority_score": 40.0},
        {"id": 6, "priority_score": 30.0},
    ]
    outcomes = [
        {"observation_id": 1, "horizon_days": 5, "code": "BAD_SCORE", "return_pct": 99.0},
        {"observation_id": 2, "horizon_days": 5, "code": "LOW1", "return_pct": -1.0},
        {"observation_id": 3, "horizon_days": 5, "code": "LOW2", "return_pct": 1.0},
        {"observation_id": 4, "horizon_days": 5, "code": "MID", "return_pct": 3.0},
        {"observation_id": 5, "horizon_days": 5, "code": "HIGH", "return_pct": 4.0},
        {"observation_id": 6, "horizon_days": 5, "code": "BAD_RETURN", "return_pct": float("inf")},
    ]

    joined = stats_mod.join_outcomes(outcomes, observations)
    score_stats = stats_mod.score_stats_json(joined, [5])
    ranked = stats_mod.ranked_outcomes(joined, 5, reverse=True)

    assert joined[0]["candidate_shadow_score"] is None
    assert score_stats["5"]["low"]["count"] == 2
    assert score_stats["5"]["mid"]["count"] == 1
    assert score_stats["5"]["high"]["count"] == 1
    assert score_stats["5"]["high"]["avg_return_pct"] == 4.0
    assert "BAD_RETURN" not in {row["code"] for row in ranked}
