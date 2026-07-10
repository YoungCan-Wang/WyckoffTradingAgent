from __future__ import annotations

from core.strategy_policy_display import (
    format_policy_meta_text,
    format_policy_weight_text,
    parse_policy_weight_key,
    policy_execution_display,
    policy_formal_dynamic_label,
    policy_governor_display,
    policy_weight_rows,
)


def test_parse_policy_weight_key_preserves_scoped_context() -> None:
    parsed = parse_policy_weight_key("lps|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure")

    assert parsed == {
        "signal_type": "lps",
        "scope": {
            "regime": "RISK_ON",
            "lane": "trend_pullback",
            "entry_type": "wyckoff_structure",
        },
    }


def test_policy_weight_rows_include_label_and_direction() -> None:
    rows = policy_weight_rows({"lps|regime=RISK_ON|lane=trend_pullback": 0.5, "sos": 1.15})

    assert rows[0]["label"] == "lps[regime=RISK_ON, lane=trend_pullback]"
    assert rows[0]["direction"] == "down"
    assert rows[1]["label"] == "sos"
    assert rows[1]["direction"] == "up"


def test_format_policy_weight_text_sanitizes_invalid_values() -> None:
    assert format_policy_weight_text({"bad": "bad", "nan": float("nan")}, delimiter="；") == "bad×1.00；nan×1.00"


def test_format_policy_meta_text_surfaces_active_scope() -> None:
    assert (
        format_policy_meta_text(
            {
                "source": "远端",
                "report_date": "2026-07-04",
                "horizon": "5",
                "execution_policy": "on",
                "execution_scope": "tail_buy_and_funnel",
                "tail_buy_weights_active": True,
                "funnel_shadow_weights_active": True,
                "funnel_formal_weights_active": True,
            }
        )
        == "（远端, 报告=2026-07-04, 周期=h5, 策略=正式调权(on), 范围=尾盘+正式漏斗）"
    )


def test_format_policy_meta_text_derives_active_scope_from_legacy_scope() -> None:
    assert (
        format_policy_meta_text(
            {
                "source": "远端",
                "execution_policy": "shadow",
                "execution_scope": "tail_buy_and_funnel_shadow",
            }
        )
        == "（远端, 策略=shadow 对照(shadow), 范围=尾盘+漏斗shadow）"
    )


def test_format_policy_meta_text_surfaces_promotion_evidence() -> None:
    assert (
        format_policy_meta_text(
            {
                "source": "远端",
                "report_date": "2026-07-04",
                "formal_dynamic_allowed": False,
                "formal_dynamic_block_reason": "backtest_confirmation_required",
                "backtest_confirmation_text": "待复核(need backtest)",
                "promotion_checklist_summary": "样本=通过；回测=待复核",
            }
        )
        == "（远端, 报告=2026-07-04, 正式dynamic=未进正式漏斗(缺少回测确认), 回测=待复核(need backtest), 晋级=样本=通过；回测=待复核）"
    )


def test_format_policy_meta_text_distinguishes_stale_backtest_evidence() -> None:
    assert (
        format_policy_meta_text(
            {
                "source": "远端",
                "formal_dynamic_allowed": False,
                "formal_dynamic_block_reason": "backtest_policy_evidence_required",
                "backtest_confirmation_text": "待复核(缺少策略治理口径证据)",
            }
        )
        == "（远端, 正式dynamic=未进正式漏斗(回测缺少策略治理证据), 回测=待复核(缺少策略治理口径证据)）"
    )


def test_policy_display_helpers_translate_governor_codes() -> None:
    governor = {
        "status": "candidate",
        "mode_recommendation": "review_promote_dynamic_policy",
        "next_action": "manual_review_dynamic_on",
        "promotion_status": "manual_review_required",
        "auto_apply": False,
    }

    assert policy_governor_display(governor) == {
        "status": "可进入人工晋级评审",
        "mode_recommendation": "评审是否切 on",
        "next_action": "进入人工晋级评审（非正式生效）",
        "promotion_status": "需人工复核",
        "auto_apply": "否",
    }


def test_policy_display_helpers_translate_backtest_gate_actions() -> None:
    assert policy_governor_display({"next_action": "run_backtest_confirmation"})["next_action"] == "先跑回测确认"
    assert policy_governor_display({"next_action": "review_policy_actions"})["next_action"] == "先复核调权治理项"
    assert (
        policy_governor_display({"next_action": "formal_dynamic_approved"})["next_action"] == "正式 dynamic 已人工批准"
    )
    assert (
        policy_governor_display({"next_action": "keep_shadow_backtest_failed"})["next_action"]
        == "回测未通过，保持 shadow"
    )


def test_policy_display_helpers_translate_manual_approval() -> None:
    display = policy_governor_display({"promotion_status": "manual_approved"})

    assert display["promotion_status"] == "已人工批准"


def test_policy_execution_display_distinguishes_formal_gate() -> None:
    execution = {
        "active_scope": "尾盘+漏斗shadow",
        "promotion_status": "manual_review_required",
        "next_action": "manual_review_dynamic_on",
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "auto_apply=false",
        "summary": "只进入 shadow",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(未启用自动晋级)"
    assert policy_execution_display(execution) == {
        "active_scope": "尾盘+漏斗shadow",
        "promotion_status": "需人工复核",
        "next_action": "进入人工晋级评审（非正式生效）",
        "formal_dynamic": "未进正式漏斗(未启用自动晋级)",
        "summary": "只进入 shadow",
    }


def test_policy_execution_display_labels_signal_action_review_gate() -> None:
    execution = {
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "signal_actions_review_required",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(信号调权待复核)"


def test_policy_execution_display_labels_policy_action_review_next_step() -> None:
    assert policy_formal_dynamic_label({"next_action": "review_policy_actions"}) == "未进正式漏斗(调权治理项待复核)"


def test_policy_execution_display_labels_missing_promotion_checklist() -> None:
    execution = {
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "promotion_checklist=missing",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(晋级清单缺失)"


def test_policy_execution_display_labels_promotion_status_reason() -> None:
    execution = {
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "promotion_status=do_not_promote",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(晋级状态=禁止晋级)"


def test_policy_execution_display_labels_blocked_promotion_checklist() -> None:
    execution = {
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "promotion_checklist=shadow_sample:review",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(晋级清单未通过(样本:待复核))"


def test_policy_execution_display_labels_missing_execution_state() -> None:
    execution = {
        "formal_dynamic_allowed": False,
        "formal_dynamic_block_reason": "execution_state=missing",
    }

    assert policy_formal_dynamic_label(execution) == "未进正式漏斗(缺少后端执行态)"
