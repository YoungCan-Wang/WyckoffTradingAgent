from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from workflows.funnel_ai_selection import FunnelAiSelection


def test_execution_decision_line_makes_observe_only_action_explicit() -> None:
    from workflows.funnel_render import _execution_decision_line

    line = _execution_decision_line("CRASH", 0)

    assert "禁止新仓" in line
    assert "不从本报告选择买入标的" in line


def test_execution_decision_line_blocks_formal_action_when_data_quality_is_degraded() -> None:
    from workflows.funnel_render import _execution_decision_line

    line = _execution_decision_line(
        "RISK_ON",
        3,
        {"status": "degraded", "trade_readiness": "observe_only", "reasons": ["ohlcv_coverage<95%"]},
    )

    assert "数据质量降级" in line
    assert "禁止正式推荐" in line


def test_data_quality_report_lines_show_coverages_sources_rps_and_rejections() -> None:
    from workflows.funnel_render import _data_quality_report_lines

    lines = _data_quality_report_lines(
        {
            "data_quality": {
                "status": "degraded",
                "trade_readiness": "observe_only",
                "reasons": ["financial_coverage<90%"],
                "financial_requested": True,
                "coverage": {"ohlcv": 0.98, "market_cap": 0.96, "financial": 0.75},
                "ohlcv_source_counts": {"tickflow": 70, "tushare": 28},
            },
            "rps_universe_count": 98,
            "layer_rejections": {
                "layer1": {"input": 100, "passed": 80, "rejected": 20, "reason": "基础准入"},
                "layer2": {"input": 80, "passed": 30, "rejected": 50, "reason": "强度条件"},
            },
        }
    )

    assert "OHLCV 98.0%" in lines[0]
    assert "财务 75.0%" in lines[0]
    assert "observe_only" in lines[0]
    assert "tickflow=70" in lines[1]
    assert "RPS universe=98" in lines[1]
    assert "L1:100→80" in lines[2]


def test_data_quality_report_marks_financials_not_applicable_for_price_volume_run() -> None:
    from workflows.funnel_render import _data_quality_report_lines

    lines = _data_quality_report_lines(
        {
            "data_quality": {
                "status": "normal",
                "trade_readiness": "ready",
                "reasons": [],
                "financial_requested": False,
                "coverage": {"ohlcv": 1.0, "market_cap": 1.0, "financial": 0.0},
            }
        }
    )

    assert "财务 未纳入量价漏斗" in lines[0]
    assert "财务 0.0%" not in lines[0]


def test_execution_decision_line_separates_review_from_execution() -> None:
    from workflows.funnel_render import _execution_decision_line

    line = _execution_decision_line("NEUTRAL", 3)

    assert "市场闸门开放" in line
    assert "Step3 待审候选 3 只" in line
    assert "跨日确认" in line
    assert "OMS 核准" in line


def test_confirmation_label_names_abc_as_springboard_structure(monkeypatch) -> None:
    from workflows import funnel_render

    monkeypatch.setattr(
        funnel_render,
        "score_springboard_abc",
        lambda _df, _signal_type: {"grade": "A+B+C", "met_count": 3},
    )
    ctx = SimpleNamespace(
        all_df_map={"000001": pd.DataFrame({"close": [10.0]})},
        code_to_trigger_keys={"000001": ["spring"]},
        candidate_entry_map={},
        mainline_candidate_set=set(),
    )

    assert funnel_render._confirmation_label(ctx, "000001") == "起跳板结构:A+B+C(3/3)"


def test_tracking_shape_section_lists_only_abc_rows_written_for_tracking(monkeypatch) -> None:
    from workflows import funnel_render

    def fake_score(df: pd.DataFrame, _signal_type: str) -> dict:
        return {"grade": df.attrs["grade"], "met_count": df.attrs["met_count"]}

    monkeypatch.setattr(funnel_render, "score_springboard_abc", fake_score)
    frames = {code: pd.DataFrame({"close": [close]}) for code, close in [("000001", 10.0), ("000002", 20.0)]}
    frames["000001"].attrs.update(grade="A+C", met_count=2)
    frames["000002"].attrs.update(grade="C", met_count=1)
    ctx = SimpleNamespace(
        formal_triggers={"sos": [("000001", 92.0)], "lps": [("000002", 61.0)]},
        all_df_map=frames,
        name_map={"000001": "形态通过", "000002": "形态不足"},
        latest_close_map={"000001": 10.0, "000002": 20.0},
        code_to_total_score={"000001": 92.0, "000002": 61.0},
    )

    lines = funnel_render._tracking_shape_section_lines(ctx, _make_selection([]))
    report = "\n".join(lines)

    assert lines[0] == "**【🧾 今日形态入表观察】1 只**"
    assert "000001 形态通过  A+C  分92.00  现价10.00" in report
    assert "000002" not in report
    assert "不是买入清单" in report


def test_tracking_shape_section_deduplicates_and_lists_every_row(monkeypatch) -> None:
    from workflows import funnel_render

    def fake_score(_df: pd.DataFrame, signal_type: str) -> dict:
        return {"grade": "A+B+C", "met_count": 3} if signal_type == "sos" else {"grade": "A+C", "met_count": 2}

    monkeypatch.setattr(funnel_render, "score_springboard_abc", fake_score)
    ctx = SimpleNamespace(
        formal_triggers={
            "sos": [("000001", 90.0)],
            "lps": [("000001", 90.0), ("000002", 80.0)],
        },
        all_df_map={
            "000001": pd.DataFrame({"close": [10.0]}),
            "000002": pd.DataFrame({"close": [20.0]}),
        },
        name_map={"000001": "三项通过", "000002": "两项通过"},
        latest_close_map={},
        code_to_total_score={"000001": 90.0, "000002": 80.0},
    )

    lines = funnel_render._tracking_shape_section_lines(ctx, _make_selection([]))
    report = "\n".join(lines)

    assert lines[0] == "**【🧾 今日形态入表观察】2 只**"
    assert "双/多 Wyckoff 形态共振（1）" in report
    assert "000001 三项通过  SOS(A+B+C)+LPS(A+C)" in report
    assert "000002 两项通过  A+C" in report
    assert "另" not in report
    assert "已入表" not in report


def test_tracking_shape_section_lists_all_compression_rows(monkeypatch) -> None:
    from workflows import funnel_render

    monkeypatch.setattr(
        funnel_render,
        "score_springboard_abc",
        lambda _df, _signal_type: {"grade": "A+C", "met_count": 2},
    )
    codes = [f"{idx:06d}" for idx in range(1, 33)]
    ctx = SimpleNamespace(
        formal_triggers={"compression": [(code, 70.0) for code in codes]},
        all_df_map={code: pd.DataFrame({"close": [10.0]}) for code in codes},
        name_map={code: f"压缩{code}" for code in codes},
        latest_close_map={},
        code_to_total_score={code: 70.0 for code in codes},
    )

    report = "\n".join(funnel_render._tracking_shape_section_lines(ctx, _make_selection([])))

    assert "今日形态入表观察】32 只" in report
    assert all(f"{code} 压缩{code}" in report for code in codes)
    assert "另" not in report
    assert "已入表" not in report
    assert "略" not in report


def test_bypass_and_mainline_card_sections_list_every_name() -> None:
    from workflows.funnel_render import _append_l2_bypass_card_section, _append_mainline_card_section

    bypass_codes = [f"{idx:06d}" for idx in range(1, 25)]
    bypass_ctx = SimpleNamespace(
        l2_bypass_pool=bypass_codes,
        l2_bypass_ranked=bypass_codes,
        name_map={code: f"旁路{code}" for code in bypass_codes},
        bypass_triggers={"sos": [(code, 60.0) for code in bypass_codes]},
        sector_map={},
        theme_badge_map={},
        all_df_map={},
        code_to_trigger_keys={code: ["sos"] for code in bypass_codes},
        candidate_entry_map={},
        mainline_candidate_set=set(),
    )
    bypass_lines: list[str] = []
    _append_l2_bypass_card_section(bypass_lines, bypass_ctx, 0)
    bypass_report = "\n".join(bypass_lines)
    assert all(f"{code} 旁路{code}" in bypass_report for code in bypass_codes)
    assert "略" not in bypass_report

    mainline_rows = [
        {
            "code": f"{idx:06d}",
            "name": f"主线{idx:06d}",
            "theme": "测试",
            "status": "主线买点候选",
            "entry_type": "等待确认",
            "mainline_score": 0.8,
            "risk_flags": [],
            "metrics": {},
        }
        for idx in range(1, 12)
    ]
    mainline_ctx = SimpleNamespace(
        mainline_candidates=mainline_rows,
        mainline_tradeable=mainline_rows,
        mainline_observe=[],
        mainline_overheated=[],
        name_map={},
    )
    mainline_lines: list[str] = []
    _append_mainline_card_section(mainline_lines, mainline_ctx, 0)
    mainline_report = "\n".join(mainline_lines)
    assert all(f"{idx:06d} 主线{idx:06d}" in mainline_report for idx in range(1, 12))
    assert "略" not in mainline_report


def test_execution_decision_line_blocks_risk_on_new_buys() -> None:
    from workflows.funnel_render import _execution_decision_line

    line = _execution_decision_line("RISK_ON", 3)

    assert "禁止新仓" in line


def test_top_summary_separates_rotation_detection_from_trade_permission() -> None:
    from workflows.funnel_render import _top_summary_lines

    ctx = SimpleNamespace(
        regime="PANIC_REPAIR",
        benchmark_context={},
        metrics={
            "theme_radar": {
                "themes": [{"theme": "创新药医药", "score": 0.64, "state": "observe"}],
                "rotation_watch": [
                    {
                        "theme": "创新药医药",
                        "rotation_score": 0.91,
                        "rotation_state": "surging",
                        "ret5": 8.6,
                        "advancing_ratio_5d": 0.72,
                    }
                ],
            }
        },
        theme_radar_source="current",
        mainline_tradeable=[],
        theme_candidate_map={},
        unique_hit_count=3,
    )

    lines = _top_summary_lines(ctx, 0, "资金修复")

    assert lines[0] == "**【🚦 一眼结论】**"
    assert "创新药医药 0.91/surging" in "\n".join(lines)
    assert "已发现短周期轮动，但市场闸门关闭" in "\n".join(lines)
    assert "不写正式推荐" in "\n".join(lines)


def test_policy_governance_line_surfaces_attribution_and_merged_weights() -> None:
    from workflows.funnel_render import _policy_governance_line

    line = _policy_governance_line(
        {
            "_attribution_signal_weights": {"lps": 0.5, "sos": 1.15},
            "_attribution_policy_meta": {
                "source": "远端",
                "report_date": "2026-07-04",
                "horizon": "5",
                "age_days": 0,
                "execution_policy": "shadow",
                "execution_scope": "funnel_shadow",
                "next_action": "manual_review_dynamic_on",
                "formal_dynamic_allowed": True,
                "backtest_confirmation_text": "待复核(need backtest)",
                "promotion_checklist_summary": "样本=通过；回测=待复核",
                "funnel_shadow_weights_active": True,
                "funnel_formal_weights_active": False,
                "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
            },
            "_signal_weights": {"evr": 0.75, "lps": 0.5, "sos": 1.15},
        }
    )

    assert line.startswith("**策略治理调权**")
    assert (
        "归因 lps×0.50↓，sos×1.15↑"
        "（远端, 报告=2026-07-04, 周期=h5, 距今=0天, 策略=shadow 对照(shadow), 下一步=进入人工晋级评审（非正式生效）, 范围=漏斗shadow, 回测=待复核(need backtest), 晋级=样本=通过；回测=待复核）"
    ) in line
    assert "最终 evr×0.75↓，lps×0.50↓，sos×1.15↑" in line
    assert "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75" in line


def test_policy_governance_line_surfaces_selection_governance_without_weights() -> None:
    from workflows.funnel_render import _policy_governance_line

    line = _policy_governance_line(
        {
            "_attribution_policy_meta": {
                "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
            },
        }
    )

    assert line == "**策略治理调权**: 候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75"


def test_policy_governance_line_formats_scoped_weights() -> None:
    from workflows.funnel_render import _policy_governance_line

    line = _policy_governance_line(
        {
            "_signal_weights": {
                "lps|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure": 0.5,
            },
        }
    )

    assert "lps[regime=RISK_ON, lane=trend_pullback, entry=wyckoff_structure]×0.50↓" in line


def test_policy_governance_line_sanitizes_invalid_weights() -> None:
    from workflows.funnel_render import _policy_governance_line

    line = _policy_governance_line({"_signal_weights": {"bad": "bad", "nan": float("nan"), "inf": float("inf")}})

    assert "bad×1.00" in line
    assert "nan×1.00" in line
    assert "inf×1.00" in line


def _make_selection(codes: list[str]) -> FunnelAiSelection:
    return FunnelAiSelection(
        selected_for_ai=codes,
        trend_selected=codes,
        accum_selected=[],
        score_map={code: 10.0 for code in codes},
        ai_policy={},
        theme_promoted_count=0,
    )


def test_top_candidate_list_lists_every_selected_code_with_buy_gate_note(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context
    from workflows.funnel_render import _top_candidate_list_lines

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {"000001": "平安银行"})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {"sos": [("000001", 10.0)]},
        {"benchmark_context": {"regime": "NEUTRAL"}},
    )
    selection = _make_selection(["000001"])

    lines = _top_candidate_list_lines(ctx, selection)

    assert lines[0] == "**【✅ 今日候选清单】1 只**"
    assert "起跳板" in lines[1] or "confirmed" in lines[1] or "可送审" in lines[1]
    assert any("000001 平安银行" in line for line in lines)


def test_top_candidate_list_marks_observe_only_when_recommendation_write_blocked(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context
    from workflows.funnel_render import _top_candidate_list_lines

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {"sos": [("000001", 10.0)]}, {"benchmark_context": {"regime": "PANIC_REPAIR"}}
    )
    selection = _make_selection(["000001"])

    lines = _top_candidate_list_lines(ctx, selection)

    assert "研究预选" in lines[1]


def test_top_candidate_list_shows_empty_placeholder_when_no_candidates(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context
    from workflows.funnel_render import _top_candidate_list_lines

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context({}, {"benchmark_context": {"regime": "RISK_ON"}})
    selection = _make_selection([])

    lines = _top_candidate_list_lines(ctx, selection)

    assert lines[0] == "**【✅ 今日候选清单】0 只**"
    assert "  无" in lines


def test_render_context_treats_event_reversal_mainline_as_tradeable(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {},
        {
            "mainline_candidates": [
                {"code": "000006", "status": "事件主题修复候选"},
                {"code": "000007", "status": "主线观察"},
            ]
        },
    )

    assert ctx.mainline_tradeable_codes == ["000006"]
    assert [row["code"] for row in ctx.mainline_tradeable] == ["000006"]


def test_render_context_scores_capital_migration_theme_candidates(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})
    monkeypatch.setattr(render_context, "FUNNEL_THEME_RADAR_BONUS_MAX", 0.0)
    monkeypatch.setattr(render_context, "FUNNEL_CAPITAL_MIGRATION_BONUS_MAX", 6.0)
    monkeypatch.setattr(render_context, "FUNNEL_CAPITAL_MIGRATION_PENALTY_MAX", 8.0)

    ctx = render_context.build_render_context(
        {"sos": [("000001", 10.0), ("000002", 10.0)]},
        {
            "theme_radar": {
                "strategic_candidates": [
                    {"code": "000001", "theme": "光模块", "theme_score": 0.8, "stock_score": 0.7},
                    {"code": "000002", "theme": "创新药医药", "theme_score": 0.7, "stock_score": 0.7},
                ]
            },
            "capital_migration": {
                "inflow": [{"theme": "CPO", "score": 0.75}],
                "outflow": [{"theme": "医药", "score": 0.50}],
            },
        },
    )

    assert ctx.capital_migration_bonus_map == {"000001": 4.5, "000002": -4.0}
    assert ctx.code_to_total_score["000001"] == 14.5
    assert ctx.code_to_total_score["000002"] == 6.0
    assert ctx.formal_sorted_codes == ["000001", "000002"]
    assert "资金迁入:光模块(+4.5)" in ctx.code_to_reasons["000001"]
    assert "资金撤出:创新药医药(-4.0)" in ctx.code_to_reasons["000002"]


def test_render_context_normalizes_candidate_entry_keys_and_keeps_best_entry(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {},
        {
            "candidate_entries": [
                {"code": "000001", "entry_type": "launchpad", "score": 80.0},
                {"code": "000001", "entry_type": "Early-Breakout", "score": 90.0},
            ]
        },
    )

    assert ctx.candidate_entry_map["000001"]["entry_type"] == "Early-Breakout"
    assert ctx.code_to_trigger_keys["000001"] == ["early_breakout"]
    assert ctx.code_to_total_score["000001"] == 90.0


def test_render_context_sanitizes_invalid_trigger_scores(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {"sos": [("BAD", "bad"), ("INF", float("inf")), ("NAN", float("nan")), ("GOOD", 4.25)]},
        {},
    )

    assert ctx.code_to_total_score == {"BAD": 0.0, "INF": 0.0, "NAN": 0.0, "GOOD": 4.25}
    assert ctx.formal_sorted_codes[0] == "GOOD"


def test_render_context_sanitizes_invalid_candidate_entry_scores(monkeypatch) -> None:
    import workflows.funnel_render_context as render_context

    monkeypatch.setattr(render_context, "load_stock_name_map", lambda: {})
    monkeypatch.setattr(render_context, "fetch_sector_map", lambda: {})

    ctx = render_context.build_render_context(
        {},
        {
            "candidate_entries": [
                {"code": "BAD", "entry_type": "launchpad", "score": "bad"},
                {"code": "INF", "entry_type": "launchpad", "score": float("inf")},
                {"code": "NAN", "entry_type": "launchpad", "score": float("nan")},
                {"code": "GOOD", "entry_type": "launchpad", "score": 8.0},
            ]
        },
    )

    assert ctx.code_to_total_score == {"BAD": 0.0, "INF": 0.0, "NAN": 0.0, "GOOD": 8.0}
