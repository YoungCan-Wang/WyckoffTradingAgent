from __future__ import annotations

import json
from datetime import date

from workflows import strategy_attribution_policy as attribution_policy
from workflows import tail_buy_policy as policy


def test_tail_buy_policy_adjustments_use_local_no_write_report(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-07-04",
                "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
                "recommendations_json": [
                    {
                        "type": "downweight",
                        "horizon": "5",
                        "target": "lps",
                        "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON", str(report_path))
    monkeypatch.setenv("STRATEGY_ATTRIBUTION_MAX_AGE_DAYS", "0")
    monkeypatch.setattr(attribution_policy, "load_latest_attribution_report", lambda _market: None)
    monkeypatch.setattr(policy, "log_line", lambda *_args, **_kwargs: None)

    assert policy.load_tail_buy_policy_adjustments(market="cn") == {"lps": 0.5}


def test_tail_buy_policy_adjustments_prefer_remote_report(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"market": "cn", "report_date": "2026-07-04"}), encoding="utf-8")
    monkeypatch.setenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON", str(report_path))
    monkeypatch.setattr(
        attribution_policy,
        "load_latest_attribution_report",
        lambda _market: {
            "market": "cn",
            "report_date": "2026-07-04",
            "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
            "recommendations_json": [
                {
                    "type": "upweight",
                    "horizon": "5",
                    "target": "sos",
                    "reason": '{"action":"upweight","horizon":"5","target":"sos","weight_multiplier":1.15}',
                }
            ],
        },
    )
    monkeypatch.setattr(policy, "log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("STRATEGY_ATTRIBUTION_MAX_AGE_DAYS", "0")

    assert policy.load_tail_buy_policy_adjustments(market="cn") == {"sos": 1.15}


def test_attribution_weights_fall_back_to_fresh_local_when_remote_is_stale(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-07-04",
                "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
                "recommendations_json": [
                    {
                        "type": "upweight",
                        "horizon": "5",
                        "target": "sos",
                        "reason": '{"action":"upweight","horizon":"5","target":"sos","weight_multiplier":1.15}',
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON", str(report_path))
    monkeypatch.setattr(
        attribution_policy,
        "load_latest_attribution_report",
        lambda _market: {
            "market": "cn",
            "report_date": "2026-06-20",
            "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
            "recommendations_json": [
                {
                    "type": "downweight",
                    "horizon": "5",
                    "target": "lps",
                    "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
                }
            ],
        },
    )

    weights = attribution_policy.load_attribution_signal_weights(market="cn", as_of=date(2026, 7, 4))

    assert weights == {"sos": 1.15}


def test_attribution_weights_use_newer_explicit_local_report(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-07-04",
                "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
                "recommendations_json": [
                    {
                        "type": "upweight",
                        "horizon": "5",
                        "target": "launchpad",
                        "reason": '{"action":"upweight","horizon":"5","target":"launchpad","weight_multiplier":1.2}',
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATEGY_ATTRIBUTION_REPORT_JSON", str(report_path))
    monkeypatch.setattr(
        attribution_policy,
        "load_latest_attribution_report",
        lambda _market: {
            "market": "cn",
            "report_date": "2026-07-03",
            "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
            "recommendations_json": [
                {
                    "type": "downweight",
                    "horizon": "5",
                    "target": "lps",
                    "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
                },
            ],
        },
    )

    snapshot = attribution_policy.load_attribution_policy_snapshot(market="cn", as_of=date(2026, 7, 4))

    assert snapshot.source == "本地"
    assert snapshot.report_date == "2026-07-04"
    assert snapshot.weights == {"launchpad": 1.2}


def test_attribution_policy_snapshot_exposes_source_age_and_execution(monkeypatch):
    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "shadow")
    monkeypatch.setattr(
        attribution_policy,
        "load_latest_attribution_report",
        lambda _market: {
            "market": "cn",
            "report_date": "2026-07-02",
            "shadow_diff_stats_json": {
                "policy_governor": {
                    "horizon": "5",
                    "status": "candidate",
                    "mode_recommendation": "review_promote_dynamic_policy",
                    "next_action": "manual_review_dynamic_on",
                    "next_action_summary": "shadow 新增组已跑赢移除组",
                    "formal_dynamic_allowed": False,
                    "formal_dynamic_block_reason": "manual_review_required",
                    "promotion_checklist": [
                        {"key": "shadow_sample", "status": "pass", "summary": "sample ok"},
                        {"key": "backtest_confirmation", "status": "review", "summary": "need backtest"},
                    ],
                    "auto_apply": False,
                }
            },
            "recommendations_json": [
                {
                    "type": "downweight",
                    "horizon": "5",
                    "target": "lps",
                    "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
                },
                {
                    "type": "selection_downweight",
                    "horizon": "5",
                    "target": "candidate_lane=trend_pullback",
                    "reason": (
                        '{"action":"selection_downweight","horizon":"5",'
                        '"target":"candidate_lane=trend_pullback","weight_multiplier":0.75,'
                        '"recommendation":"降级到 shadow/人工复核"}'
                    ),
                },
            ],
        },
    )

    snapshot = attribution_policy.load_attribution_policy_snapshot(market="cn", as_of=date(2026, 7, 4))

    assert snapshot.weights == {"lps": 0.5}
    assert snapshot.source == "远端"
    assert snapshot.report_date == "2026-07-02"
    assert snapshot.age_days == 2
    assert snapshot.governor_status == "candidate"
    assert snapshot.mode_recommendation == "review_promote_dynamic_policy"
    assert snapshot.next_action == "manual_review_dynamic_on"
    assert snapshot.next_action_summary == "shadow 新增组已跑赢移除组"
    assert snapshot.formal_dynamic_allowed is False
    assert snapshot.formal_dynamic_block_reason == "manual_review_required"
    assert snapshot.promotion_checklist_summary == "样本=通过；回测=待复核"
    assert snapshot.backtest_confirmation_text == "待复核(need backtest)"
    assert snapshot.execution_policy == "shadow"
    assert snapshot.execution_scope == "tail_buy_and_funnel_shadow"
    assert snapshot.signal_action_count == 1
    assert snapshot.selection_action_count == 1
    assert snapshot.selection_action_summary == (
        "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75"
    )
    assert snapshot.as_dict()["weight_count"] == 1
    assert snapshot.as_dict()["next_action"] == "manual_review_dynamic_on"
    assert snapshot.as_dict()["formal_dynamic_allowed"] is False
    assert snapshot.as_dict()["promotion_checklist_summary"] == "样本=通过；回测=待复核"
    assert snapshot.as_dict()["backtest_confirmation_text"] == "待复核(need backtest)"
    assert snapshot.as_dict()["selection_action_count"] == 1
    assert snapshot.as_dict()["selection_action_summary"] == (
        "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75"
    )
    assert snapshot.as_dict()["execution_scope"] == "tail_buy_and_funnel_shadow"
    assert snapshot.as_dict()["active_scope"] == "尾盘+漏斗shadow"
    assert snapshot.as_dict()["tail_buy_weights_active"] is True
    assert snapshot.as_dict()["funnel_shadow_weights_active"] is True
    assert snapshot.as_dict()["funnel_formal_weights_active"] is False


def test_attribution_weights_for_funnel_respects_governor_gate():
    blocked = attribution_policy.AttributionPolicySnapshot(
        weights={"lps": 0.5},
        execution_scope="tail_buy_and_funnel_shadow",
        formal_dynamic_allowed=False,
        formal_dynamic_block_reason="next_action=keep_static_policy",
    )
    allowed = attribution_policy.AttributionPolicySnapshot(
        weights={"sos": 1.15},
        execution_scope="tail_buy_and_funnel",
        formal_dynamic_allowed=True,
    )

    assert attribution_policy.attribution_weights_for_funnel(blocked, mode="shadow") == {"lps": 0.5}
    assert attribution_policy.attribution_weights_for_funnel(blocked, mode="on") == {}
    assert attribution_policy.attribution_weights_for_funnel(allowed, mode="on") == {"sos": 1.15}
    assert attribution_policy.attribution_weights_for_funnel(allowed, mode="off") == {}


def test_attribution_policy_snapshot_requires_checklist_for_formal_dynamic(monkeypatch):
    monkeypatch.setenv("FUNNEL_DYNAMIC_POLICY", "on")
    monkeypatch.setattr(
        attribution_policy,
        "load_latest_attribution_report",
        lambda _market: {
            "market": "cn",
            "report_date": "2026-07-04",
            "shadow_diff_stats_json": {
                "policy_governor": {
                    "horizon": "5",
                    "next_action": "manual_review_dynamic_on",
                    "promotion_status": "manual_review_required",
                    "formal_dynamic_allowed": True,
                }
            },
            "recommendations_json": [
                {
                    "type": "upweight",
                    "horizon": "5",
                    "target": "sos",
                    "reason": '{"action":"upweight","horizon":"5","target":"sos","weight_multiplier":1.15}',
                }
            ],
        },
    )

    snapshot = attribution_policy.load_attribution_policy_snapshot(market="cn", as_of=date(2026, 7, 4))

    assert snapshot.weights == {"sos": 1.15}
    assert snapshot.formal_dynamic_allowed is False
    assert snapshot.formal_dynamic_block_reason == "promotion_checklist=missing"
    assert snapshot.execution_scope == "tail_buy_and_funnel_shadow"
    assert attribution_policy.attribution_weights_for_funnel(snapshot, mode="on") == {}
    assert attribution_policy.attribution_weights_for_funnel(snapshot, mode="shadow") == {"sos": 1.15}


def test_attribution_weights_skip_stale_reports(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "market": "cn",
                "report_date": "2026-06-20",
                "shadow_diff_stats_json": {"policy_governor": {"horizon": "5"}},
                "recommendations_json": [
                    {
                        "type": "downweight",
                        "horizon": "5",
                        "target": "lps",
                        "reason": '{"action":"downweight","horizon":"5","target":"lps","weight_multiplier":0.5}',
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON", str(report_path))
    monkeypatch.setattr(attribution_policy, "load_latest_attribution_report", lambda _market: None)

    weights = attribution_policy.load_attribution_signal_weights(market="cn", as_of=date(2026, 7, 4))

    assert weights == {}
