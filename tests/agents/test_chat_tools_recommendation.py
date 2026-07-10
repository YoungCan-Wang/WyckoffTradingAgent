from __future__ import annotations

from agents.history_tools import query_history


def test_query_tail_buy_exposes_execution_semantics(monkeypatch):
    from integrations import local_db

    monkeypatch.setattr(
        local_db,
        "load_tail_buy_history",
        lambda run_date="", decision="", limit=20: [
            {
                "code": "600378",
                "name": "昊华科技",
                "run_date": "2026-07-04",
                "signal_type": "rec_momentum_continuation",
                "final_decision": "BUY",
                "rule_score": 96.0,
                "priority_score": 100.0,
                "llm_decision": "BUY",
                "llm_reason": "尾盘动能延续",
                "features_json": "",
            },
            {
                "code": "603713",
                "name": "密尔克卫",
                "run_date": "2026-07-04",
                "signal_type": "sos",
                "final_decision": "BUY",
                "rule_score": 92.0,
                "priority_score": 100.0,
                "llm_decision": "BUY",
                "llm_reason": "尾盘确认",
                "features_json": '{"execution_label":"可执行买入","orderable":true}',
            },
        ],
    )

    result = query_history(source="tail_buy", limit=2)

    assert result["records"][0]["decision_display"] == "BUY（观察买入）"
    assert result["records"][0]["orderable"] is False
    assert result["records"][0]["execution_next_step"] == "高位动能默认不买；只保留人工复核。"
    assert result["records"][1]["decision_display"] == "BUY（可执行买入）"
    assert result["records"][1]["orderable"] is True


def test_query_recommendation_exposes_non_ai_review_role(monkeypatch):
    from integrations import local_db

    monkeypatch.setattr(
        local_db,
        "load_recommendations",
        lambda limit: [
            {
                "code": "603039",
                "name": "泛微网络",
                "recommend_date": "20260611",
                "is_ai_recommended": False,
            }
        ],
    )

    result = query_history(source="recommendation", limit=1)

    assert result["records"][0]["is_ai_recommended"] is False
    assert result["records"][0]["entry_role"] == "观察/信号复盘"


def test_query_recommendation_exposes_ai_recommendation_role(monkeypatch):
    from integrations import local_db

    monkeypatch.setattr(
        local_db,
        "load_recommendations",
        lambda limit: [
            {
                "code": "300557",
                "name": "理工光科",
                "recommend_date": "20260615",
                "is_ai_recommended": "true",
            }
        ],
    )

    result = query_history(source="recommendation", limit=1)

    assert result["records"][0]["is_ai_recommended"] is True
    assert result["records"][0]["entry_role"] == "AI推荐"


def test_query_history_attribution_surfaces_policy_governor(monkeypatch):
    from agents import history_tools

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    monkeypatch.setattr(
        history_tools,
        "_load_attribution_rows",
        lambda limit, tool_context: [
            {
                "report_date": "2026-07-04",
                "window_start": "2026-05-05",
                "window_end": "2026-07-04",
                "shadow_diff_stats_json": {
                    "count": 12,
                    "avg_added": 1.4,
                    "avg_removed": 1.1,
                    "latest": {
                        "trade_date": "2026-07-03",
                        "regime": "RISK_ON",
                        "selection_summary": {
                            "base_count": 8,
                            "shadow_count": 9,
                            "diff_added_count": 2,
                            "diff_removed_count": 1,
                            "jaccard": 0.7,
                        },
                        "diff_added_sample": ["300502", "688008"],
                        "diff_removed_sample": ["002079"],
                    },
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
                        "horizon": "5",
                    },
                },
                "recommendations_json": [
                    {"type": "policy_governor", "target": "dynamic_policy", "horizon": "5", "reason": "{}"},
                    {
                        "type": "downweight",
                        "target": "lps",
                        "horizon": "5",
                        "reason": (
                            '{"action":"downweight","weight_multiplier":0.5,'
                            '"scope":{"regime":"RISK_ON","lane":"trend_pullback"},'
                            '"evidence":{"avg_return_pct":-3.0}}'
                        ),
                    },
                ],
            }
        ],
    )

    result = query_history(source="attribution", limit=1)

    assert result["latest_policy"]["status"] == "candidate"
    assert result["latest_source"] == "remote"
    assert result["latest_policy"]["next_action"] == "manual_review_dynamic_on"
    assert result["latest_policy"]["promotion_status"] == "manual_review_required"
    assert result["latest_policy_display"]["next_action"] == "进入人工晋级评审（非正式生效）"
    assert result["latest_policy_display"]["promotion_status"] == "需人工复核"
    assert result["latest_policy"]["promotion_checklist"][0]["key"] == "shadow_sample"
    assert result["latest_execution_state"]["scope"] == "tail_buy_and_funnel_shadow"
    assert result["latest_execution_state"]["active_scope"] == "尾盘+漏斗shadow"
    assert result["latest_execution_state"]["tail_buy_weights_active"] is True
    assert result["latest_execution_state"]["funnel_shadow_weights_active"] is True
    assert result["latest_execution_state"]["funnel_formal_weights_active"] is False
    assert result["latest_execution_state"]["promotion_status"] == "manual_review_required"
    assert result["latest_execution_summary"]["formal_dynamic"] == "未进正式漏斗(人工复核未完成)"
    assert result["latest_execution_summary"]["next_action"] == "进入人工晋级评审（非正式生效）"
    assert result["latest_operations"]["latest_shadow"]["trade_date"] == "2026-07-03"
    assert result["latest_operations"]["latest_shadow"]["diff_added_sample"] == ["300502", "688008"]
    assert result["latest_operations"]["backtest_confirmation_text"] == "待复核(need backtest)"
    assert result["latest_operations"]["promotion_checklist_summary"] == "样本=通过；回测=待复核"
    assert result["latest_operations"]["promotion_blockers"] == [
        {"key": "backtest_confirmation", "status": "review", "summary": "need backtest"}
    ]
    assert result["latest_operations"]["action_summary"].startswith("本期 1 个 scoped 调权")
    assert result["latest_operator_summary"] == result["latest_operations"]["operator_summary"]
    assert "Shadow=2026-07-03 RISK_ON 新增2 移除1" in result["latest_operator_summary"]
    assert "作用范围=尾盘+漏斗shadow" in result["latest_operator_summary"]
    assert "回测确认=待复核(need backtest)" in result["latest_operator_summary"]
    assert result["records"][0]["shadow"]["runs"] == 12
    assert result["records"][0]["policy_display"]["status"] == "可进入人工晋级评审"
    assert result["records"][0]["execution_summary"]["promotion_status"] == "需人工复核"
    assert result["records"][0]["execution_state"]["signal_action_count"] == 1
    assert result["records"][0]["execution_state"]["action_details"][0]["weight_multiplier"] == 0.5
    assert result["records"][0]["execution_state"]["action_details"][0]["evidence"] == {"avg_return_pct": -3.0}
    assert (
        result["records"][0]["execution_state"]["action_details"][0]["label"]
        == "lps[regime=RISK_ON, lane=trend_pullback]"
    )
    assert "漏斗动态策略 shadow 对照" in result["records"][0]["execution_state"]["summary"]
    assert result["records"][0]["signal_actions"] == [
        {
            "action": "downweight",
            "horizon": "5",
            "target": "lps",
            "label": "lps[regime=RISK_ON, lane=trend_pullback]",
            "weight_multiplier": 0.5,
            "scope": {"regime": "RISK_ON", "lane": "trend_pullback"},
            "evidence": {"avg_return_pct": -3.0},
        }
    ]


def test_query_history_attribution_uses_workflow_default_when_env_missing(monkeypatch, tmp_path):
    from agents import history_tools
    from workflows import strategy_attribution_execution

    monkeypatch.delenv("FUNNEL_DYNAMIC_POLICY", raising=False)
    workflow_path = tmp_path / "wyckoff_funnel.yml"
    workflow_path.write_text(
        "env:\n"
        "  FUNNEL_DYNAMIC_POLICY: "
        "${{ vars.FUNNEL_DYNAMIC_POLICY || secrets.FUNNEL_DYNAMIC_POLICY || 'shadow' }}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(strategy_attribution_execution, "DEFAULT_FUNNEL_WORKFLOW_PATH", workflow_path)
    monkeypatch.setattr(
        history_tools,
        "_load_attribution_rows",
        lambda limit, tool_context: [
            {
                "report_date": "2026-07-04",
                "window_start": "2026-05-05",
                "window_end": "2026-07-04",
                "shadow_diff_stats_json": {
                    "policy_governor": {
                        "status": "candidate",
                        "mode_recommendation": "review_promote_dynamic_policy",
                        "promotion_status": "manual_review_required",
                        "promotion_checklist": [
                            {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                        ],
                        "auto_apply": False,
                        "summary": "shadow 新增组显著优于移除组",
                        "horizon": "5",
                    },
                },
                "recommendations_json": [
                    {
                        "type": "downweight",
                        "target": "lps",
                        "horizon": "5",
                        "reason": '{"action":"downweight","weight_multiplier":0.5}',
                    },
                ],
            }
        ],
    )

    result = query_history(source="attribution", limit=1)

    state = result["latest_execution_state"]
    assert state["funnel_dynamic_policy"] == "shadow"
    assert state["scope"] == "tail_buy_and_funnel_shadow"


def test_dynamic_policy_defaults_to_shadow_without_repository_workflow(monkeypatch, tmp_path):
    from workflows.strategy_attribution_execution import funnel_dynamic_policy_mode

    monkeypatch.delenv("FUNNEL_DYNAMIC_POLICY", raising=False)

    assert funnel_dynamic_policy_mode(workflow_path=tmp_path / "missing.yml") == "shadow"


def test_query_history_attribution_falls_back_to_local_report(monkeypatch, tmp_path):
    import json

    from agents import history_tools

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")

    def fail_remote(_limit, _tool_context):
        raise RuntimeError("RLS denied")

    monkeypatch.setattr(history_tools, "_load_remote_attribution_rows", fail_remote)
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-07-04",
                "window_start": "2026-05-05",
                "window_end": "2026-07-04",
                "shadow_diff_stats_json": {
                    "count": 24,
                    "policy_governor": {
                        "status": "candidate",
                        "mode_recommendation": "review_promote_dynamic_policy",
                        "promotion_status": "manual_review_required",
                        "promotion_checklist": [
                            {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                        ],
                        "auto_apply": False,
                        "summary": "shadow 新增组显著优于移除组",
                        "horizon": "5",
                    },
                },
                "recommendations_json": [
                    {
                        "type": "downweight",
                        "target": "lps",
                        "horizon": "5",
                        "reason": '{"action":"downweight","weight_multiplier":0.5}',
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATEGY_ATTRIBUTION_REPORT_JSON", str(report_path))

    result = query_history(source="attribution", limit=1)

    assert result["latest_policy"]["promotion_status"] == "manual_review_required"
    assert result["latest_source"] == "local"
    assert "RLS denied" in result["remote_error"]
    assert result["latest_execution_state"]["scope"] == "tail_buy_and_funnel_shadow"
    assert result["latest_execution_state"]["promotion_checklist"][0]["key"] == "shadow_sample"
    assert result["records"][0]["shadow"]["runs"] == 24


def test_query_history_attribution_uses_newer_local_report(monkeypatch, tmp_path):
    import json

    from agents import history_tools

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    monkeypatch.setattr(
        history_tools,
        "_load_remote_attribution_rows",
        lambda limit, tool_context: [
            {
                "report_date": "2026-07-03",
                "window_start": "2026-05-04",
                "window_end": "2026-07-03",
                "shadow_diff_stats_json": {
                    "count": 12,
                    "policy_governor": {"status": "watch", "promotion_status": "keep_shadow"},
                },
                "recommendations_json": [],
            }
        ],
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-07-04",
                "window_start": "2026-05-05",
                "window_end": "2026-07-04",
                "shadow_diff_stats_json": {
                    "count": 24,
                    "policy_governor": {
                        "status": "candidate",
                        "promotion_status": "manual_review_required",
                        "promotion_checklist": [{"key": "shadow_sample", "status": "pass"}],
                    },
                },
                "recommendations_json": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATEGY_ATTRIBUTION_REPORT_JSON", str(report_path))

    result = query_history(source="attribution", limit=2)

    assert result["latest_source"] == "local"
    assert result["latest_policy"]["promotion_status"] == "manual_review_required"
    assert result["records"][0]["source"] == "local"
    assert result["records"][0]["report_date"] == "2026-07-04"
    assert result["records"][1]["source"] == "remote"
    assert result["records"][1]["report_date"] == "2026-07-03"


def test_query_attribution_exposes_policy_governor(monkeypatch):
    from integrations import supabase_base

    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return type("Result", (), {"data": self.rows})()

    class FakeClient:
        def table(self, _name):
            return FakeQuery(
                [
                    {
                        "report_date": "2026-07-04",
                        "window_start": "2026-05-05",
                        "window_end": "2026-07-04",
                        "shadow_diff_stats_json": {
                            "count": 24,
                            "avg_added": 0.42,
                            "avg_removed": 12.83,
                            "policy_governor": {
                                "status": "candidate",
                                "mode_recommendation": "review_promote_dynamic_policy",
                                "next_action": "manual_review_dynamic_on",
                                "next_action_summary": "shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。",
                                "promotion_status": "manual_review_required",
                                "promotion_checklist": [
                                    {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                                ],
                                "auto_apply": False,
                                "summary": "shadow 新增组显著优于移除组",
                                "horizon": "5",
                            },
                        },
                        "recommendations_json": [
                            {
                                "type": "downweight",
                                "horizon": "5",
                                "target": "lps",
                                "reason": '{"weight_multiplier": 0.5, "evidence": {"avg_return_pct": -3.2}}',
                            }
                        ],
                    }
                ]
            )

    monkeypatch.setattr(supabase_base, "create_read_client", lambda: FakeClient())

    result = query_history(source="attribution", limit=1)

    assert result["latest_policy"]["status"] == "candidate"
    assert result["latest_source"] == "remote"
    assert result["latest_execution_state"]["scope"] == "tail_buy_and_funnel_shadow"
    assert result["latest_execution_state"]["active_scope"] == "尾盘+漏斗shadow"
    assert result["latest_execution_state"]["tail_buy_weights_active"] is True
    assert result["latest_execution_state"]["funnel_shadow_weights_active"] is True
    assert result["latest_execution_state"]["funnel_formal_weights_active"] is False
    assert result["records"][0]["policy_governor"]["mode_recommendation"] == "review_promote_dynamic_policy"
    assert result["records"][0]["policy_governor"]["next_action"] == "manual_review_dynamic_on"
    assert result["records"][0]["policy_governor"]["promotion_status"] == "manual_review_required"
    assert result["records"][0]["execution_state"]["promotion_checklist"][0]["status"] == "pass"
    assert result["records"][0]["signal_actions"][0]["target"] == "lps"
    assert result["records"][0]["execution_state"]["action_details"][0]["weight_multiplier"] == 0.5
    assert result["records"][0]["operations"]["action_count"] == 1
    assert "未批准进入漏斗正式 dynamic" in result["records"][0]["execution_state"]["summary"]
    assert result["records"][0]["shadow"]["runs"] == 24


def test_query_history_schema_allows_attribution_source():
    from cli.tools import TOOL_SCHEMAS

    query_schema = next(item for item in TOOL_SCHEMAS if item["name"] == "query_history")
    source = query_schema["parameters"]["properties"]["source"]

    assert "attribution" in source["enum"]
    assert "latest_source" in source["description"]
    assert "remote_error" in source["description"]
    assert "latest_operator_summary" in source["description"]
    assert "latest_policy_display" in source["description"]
    assert "latest_execution_summary" in source["description"]
    assert "promotion_checklist" in source["description"]
    assert "latest_operations" in source["description"]
