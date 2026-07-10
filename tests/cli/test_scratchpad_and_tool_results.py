from __future__ import annotations

import json

from cli.scratchpad import AgentScratchpad
from cli.tool_results import INLINE_TOOL_RESULT_MAX_CHARS, format_tool_result_for_context, serialize_tool_result
from utils.tool_result_preview import tool_result_brief_lines, tool_result_preview


class _Scalar:
    def __init__(self, value: float):
        self.value = value

    def item(self) -> float:
        return self.value


def test_scratchpad_records_jsonl_and_redacts_secrets(tmp_path):
    scratchpad = AgentScratchpad("看看 000001", session_id="session_x", scratchpad_dir=tmp_path)
    scratchpad.record_tool_result(
        "web_fetch",
        {"url": "https://example.com", "api_key": "secret-value"},
        {"ok": True, "token": "secret-token"},
        duration_ms=12,
    )
    scratchpad.record_compaction(
        before_messages=12,
        after_messages=5,
        metadata={"archive_ref": "archive://session_x/ctx_1", "messages_path": "/tmp/ctx.jsonl"},
    )
    scratchpad.record_final("完成", input_tokens=10, output_tokens=5, elapsed_s=0.5)

    lines = [json.loads(line) for line in scratchpad.path.read_text(encoding="utf-8").splitlines()]

    assert [line["type"] for line in lines] == ["init", "tool_result", "compaction", "final"]
    tool_entry = lines[1]
    assert tool_entry["args"]["api_key"] == "***REDACTED***"
    assert tool_entry["result"]["token"] == "***REDACTED***"
    assert tool_entry["durationMs"] == 12
    assert lines[2]["contextArchive"]["archive_ref"] == "archive://session_x/ctx_1"


def test_tool_result_serialization_replaces_nonfinite_numbers() -> None:
    content = serialize_tool_result(
        {
            "nan_score": float("nan"),
            "inf_score": float("inf"),
            "nested": [float("-inf"), _Scalar(float("nan")), 12.5],
        }
    )

    assert "NaN" not in content
    assert "Infinity" not in content
    assert json.loads(content) == {
        "nan_score": None,
        "inf_score": None,
        "nested": [None, None, 12.5],
    }


def test_analyze_stock_preview_surfaces_actionable_diagnosis_brief() -> None:
    result = {
        "code": "002326",
        "name": "永太科技",
        "latest_date": "2026-07-03",
        "latest_close": 25.62,
        "data_status": "ok",
        "health": "🟢健康",
        "ma_pattern": "多头排列",
        "l2_channel": "主升通道",
        "track": "Trend",
        "candidate_lane": "wyckoff_structure",
        "candidate_entry_type": "SOS",
        "candidate_score": 83.04,
        "formatted_text": "long diagnostic text should stay out of the compact preview",
        "diagnosis_brief": {
            "status": "priority_watch",
            "label": "重点观察",
            "headline": "重点观察: 002326 永太科技",
            "strengths": ["多头排列", "L2通道: 主升通道", "候选车道: SOS(83.0)"],
            "risks": ["大盘风险闸门关闭"],
            "direct_buy_allowed": False,
            "next_step": "加入重点观察，等待市场闸门打开和回踩/触发确认",
        },
    }

    preview = json.loads(tool_result_preview("analyze_stock", result))
    lines = tool_result_brief_lines("analyze_stock", result)

    assert preview["diagnosis_brief"]["status"] == "priority_watch"
    assert preview["diagnosis_brief"]["direct_buy_allowed"] is False
    assert "formatted_text" not in preview
    assert lines == [
        "重点观察: 002326 永太科技",
        "现价25.62 · 日期2026-07-03 · 健康🟢健康 · 均线多头排列 · 通道主升通道 · 得分83.04",
        "亮点: 多头排列；L2通道: 主升通道；候选车道: SOS(83.0) · 风险: 大盘风险闸门关闭 · 下一步: 加入重点观察，等待市场闸门打开和回踩/触发确认",
    ]


def test_analyze_stock_brief_surfaces_ai_report_handoff() -> None:
    result = {
        "code": "002326",
        "name": "永太科技",
        "latest_date": "2026-07-03",
        "latest_close": 25.62,
        "health": "🟢健康",
        "ma_pattern": "多头排列",
        "l2_channel": "主升通道",
        "candidate_score": 83.04,
        "diagnosis_brief": {
            "status": "priority_watch",
            "headline": "重点观察: 002326 永太科技",
            "strengths": ["多头排列"],
            "risks": [],
            "next_step": "加入重点观察，等待市场闸门打开和回踩/触发确认",
        },
        "next_tool": {
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["002326"]},
            "reason": "个股诊断进入重点/触发观察，可生成 AI 研报复核；不直接触发买入",
        },
    }

    preview = json.loads(tool_result_preview("analyze_stock", result))
    lines = tool_result_brief_lines("analyze_stock", result, max_lines=4)

    assert preview["next_tool"]["tool"] == "generate_ai_report"
    assert lines[-1] == (
        "下一工具: generate_ai_report(stock_codes=002326) · "
        "个股诊断进入重点/触发观察，可生成 AI 研报复核；不直接触发买入"
    )


def test_analyze_stock_price_brief_uses_market_label() -> None:
    result = {
        "code": "002293",
        "latest_date": "2026-07-03",
        "latest_close": 11.07,
        "data_status": "ok",
        "days": 5,
        "data": [
            {"date": "2026-06-29", "close": 9.98, "pct_chg": 2.89},
            {"date": "2026-06-30", "close": 9.89, "pct_chg": -0.9},
            {"date": "2026-07-01", "close": 10.66, "pct_chg": 7.79},
            {"date": "2026-07-02", "close": 10.85, "pct_chg": 1.78},
            {"date": "2026-07-03", "close": 11.07, "pct_chg": 2.03},
        ],
    }

    preview = json.loads(tool_result_preview("analyze_stock", result))
    lines = tool_result_brief_lines("analyze_stock", result)

    assert preview["data"][0]["date"] == "2026-07-01"
    assert preview["data"][-1]["date"] == "2026-07-03"
    assert lines == [
        "个股行情: 002293",
        "现价11.07 · 日期2026-07-03",
        "行情样本: 5条 · 最新涨跌+2.03%",
    ]


def test_portfolio_view_brief_lines_guide_empty_holdings_in_chat() -> None:
    result = {
        "message": "未找到持仓记录，可通过 update_portfolio 添加",
        "positions": [],
        "free_cash": 0,
    }

    preview = json.loads(tool_result_preview("portfolio", result))
    lines = tool_result_brief_lines("portfolio", result)

    assert preview["message"] == "未找到持仓记录，可通过 update_portfolio 添加"
    assert lines == [
        "持仓: 暂无头寸 · 现金0.00",
        "未找到持仓记录，可通过 update_portfolio 添加",
        "下一步: 直接在聊天里发持仓代码 / 成本 / 仓位，我会继续做诊断",
    ]


def test_portfolio_diagnosis_brief_lines_prioritize_risky_positions() -> None:
    result = {
        "portfolio_id": "USER_LIVE:test",
        "free_cash": 11600,
        "position_count": 2,
        "successful_count": 2,
        "failed_count": 0,
        "diagnostics": [
            {
                "code": "000001",
                "name": "平安银行",
                "health": "🟢健康",
                "latest_close": 10.2,
                "pnl_pct": 2.0,
                "l2_channel": "主升通道",
                "health_reasons": ["多头排列"],
            },
            {
                "code": "002081",
                "name": "金螳螂",
                "health": "🔴危险",
                "latest_close": 3.15,
                "pnl_pct": -12.5,
                "l2_channel": "未入选",
                "health_reasons": ["结构止损（从高点回撤>10%）"],
                "diagnosis_brief": {
                    "status": "avoid",
                    "headline": "回避: 002081 金螳螂",
                    "risks": ["结构止损（从高点回撤>10%）"],
                    "next_step": "回避新增，等待结构止损解除或重新站回强势结构",
                    "direct_buy_allowed": False,
                },
            },
        ],
    }

    preview = json.loads(tool_result_preview("portfolio", result))
    lines = tool_result_brief_lines("portfolio", result)

    assert preview["diagnostics"][1]["diagnosis_brief"]["status"] == "avoid"
    assert lines == [
        "持仓诊断: 2只 · 成功2，失败0 · 现金11,600.00",
        "002081 金螳螂 · 🔴危险 · 现价3.15 · 盈亏-12.50% · 通道未入选 · 风险: 结构止损（从高点回撤>10%） · 下一步: 回避新增，等待结构止损解除或重新站回强势结构",
        "000001 平安银行 · 🟢健康 · 现价10.2 · 盈亏+2.00% · 通道主升通道",
    ]


def test_large_tool_result_is_persisted_with_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {"rows": ["x" * 1000 for _ in range(60)]}

    content = format_tool_result_for_context("screen_stocks", "call_1", result, max_chars=1000)

    assert "工具结果已卸载为可追溯节点" in content
    assert "node_id:" in content
    assert "result_ref:" in content
    assert "预览:" in content
    stored = list((tmp_path / "tool-results").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text(encoding="utf-8"))["rows"][0] == "x" * 1000
    index_lines = (tmp_path / "tool-results" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(index_lines[0])["tool_call_id"] == "call_1"


def test_default_tool_result_budget_offloads_medium_json(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {"rows": ["x" * 1000 for _ in range((INLINE_TOOL_RESULT_MAX_CHARS // 1000) + 2)]}

    content = format_tool_result_for_context("screen_stocks", "call_2", result)

    assert "result_ref:" in content
    assert len(list((tmp_path / "tool-results").glob("*.json"))) == 1


def test_dynamic_workflow_brief_lines_surface_candidate_result():
    result = {
        "workflow_run_id": "wf_screen",
        "workflow": "dynamic_task",
        "elapsed": 12.34,
        "final_text": "候选结论: 首选 300750 宁德时代\n风险边界: 跌破 20 日线转观察",
        "events": [
            {
                "type": "workflow_step_done",
                "step": {
                    "title": "扫描候选",
                    "status": "completed",
                    "summary": "候选扫描完成",
                    "evidence": [
                        "候选结论: 首选 300750 宁德时代 · 可进入AI复核",
                        "候选护栏: 1只禁止直接买入",
                    ],
                },
            }
        ],
    }

    lines = tool_result_brief_lines("dynamic_workflow", result, max_lines=4)

    assert lines == [
        "动态 workflow: 完成 · dynamic_task · wf_screen · 12.3s",
        "候选结论: 首选 300750 宁德时代",
        "候选护栏: 1只禁止直接买入",
        "最近步骤: 扫描候选 · completed · 候选扫描完成",
    ]


def test_dynamic_workflow_brief_lines_surface_failed_run():
    result = {
        "workflow_run_id": "wf_failed",
        "workflow": "dynamic_task",
        "error": "planner timeout",
        "events": [
            {
                "type": "workflow_step_start",
                "step": {"title": "生成研报", "status": "running", "summary": "等待模型返回"},
            }
        ],
    }

    lines = tool_result_brief_lines("dynamic_workflow", result, max_lines=3)

    assert lines == [
        "动态 workflow: 失败 · dynamic_task · wf_failed",
        "错误: planner timeout",
        "最近步骤: 生成研报 · running · 等待模型返回",
    ]


def test_screen_stocks_large_result_preview_prioritizes_top_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {
        "ok": True,
        "board": "chinext",
        "scan_scope": {"scope": "bounded", "board": "chinext", "limit": 200, "total_scanned": 200},
        "summary": {"total_scanned": 2000},
        "data_quality": {
            "status": "partial",
            "coverage_pct": 94.5,
            "warnings": ["11只股票拉取失败", "数据覆盖率 94.5%"],
            "action": "候选可参考，但需要优先复核缺失数据影响",
        },
        "trade_mode": {"regime": "RISK_OFF", "action": "不新增买入"},
        "decision_brief": {
            "market_gate": "风险规避 / 不新增买入",
            "report_focus": [
                {
                    "summary": "300750 宁德时代: LPS+SOS；只观察",
                    "risk_factors": ["大盘风险闸门关闭"],
                    "action_status": "blocked_by_market_gate",
                }
            ],
        },
        "selection_brief": {
            "status": "ready_for_ai_review",
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "best_codes": ["300750"],
            "best_candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "quality_factors": ["高优先级研报候选"],
                    "risk_factors": ["大盘风险闸门关闭"],
                    "action_status": "blocked_by_market_gate",
                }
            ],
            "tool_handoff": {"tool": "generate_ai_report", "args": {"stock_codes": ["300750"]}},
        },
        "action_plan": {
            "candidate_action": "只观察，不新增买入",
            "new_buy_allowed": False,
            "review_targets": {
                "codes": ["300750"],
                "status": "ready",
                "tool": "generate_ai_report",
                "args": {"stock_codes": ["300750"]},
            },
            "long_debug_payload": "x" * 1200,
        },
        "trigger_groups": {"huge": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(100)]},
        "top_candidates": [
            {
                "code": "300750",
                "name": "宁德时代",
                "score": 96.5,
                "triggers": ["lps", "sos"],
                "quality_factors": ["高优先级研报候选"],
                "risk_factors": ["大盘风险闸门关闭"],
                "action_status": "blocked_by_market_gate",
            },
        ],
        "symbols_for_report": ["300750"],
    }

    content = format_tool_result_for_context("screen_stocks", "call_screen", result, max_chars=1000)

    assert "result_ref:" in content
    assert '"top_candidates": [{"code": "300750"' in content
    assert "宁德时代" in content
    assert '"decision_brief": {"market_gate": "风险规避 / 不新增买入"' in content
    assert '"selection_brief": {"status": "ready_for_ai_review"' in content
    assert '"candidate_conclusion": {"line": "候选结论: 阻断候选 300750 宁德时代' in content
    assert "证据: 触发分96.5" in content
    assert '"scan_scope": {"scope": "bounded", "board": "chinext", "limit": 200, "total_scanned": 200}' in content
    assert '"data_quality": {"status": "partial"' in content
    assert "候选可参考，但需要优先复核缺失数据影响" in content
    assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in content
    assert "300750 宁德时代: LPS+SOS；只观察" in content
    assert '"risk_factors": ["大盘风险闸门关闭"]' in content
    assert '"action_status": "blocked_by_market_gate"' in content
    assert '"trade_mode": {"regime": "RISK_OFF", "action": "不新增买入"}' in content
    assert '"tool": "generate_ai_report"' in content
    assert '"args": {"stock_codes": ["300750"]}' in content
    assert "完整 trigger_groups 已保留在完整结果中" in content
    assert '"trigger_groups"' not in content
    assert "long_debug_payload" not in content
    stored = list((tmp_path / "tool-results").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text(encoding="utf-8"))["trigger_groups"]["huge"][0]["blob"] == "x" * 200


def test_screen_stocks_brief_surfaces_data_date_and_coverage():
    ok_result = {
        "scan_scope": {"scope": "bounded", "board": "main_chinext_star", "limit": 300, "total_scanned": 300},
        "data_quality": {"status": "ok", "coverage_pct": 100.0, "end_trade_date": "2026-07-03"},
        "style_preference": {"raw": "trend,pullback", "styles": ["trend", "pullback"]},
        "preference_match": {"style": "partial"},
    }
    degraded_result = {
        "scan_scope": {"scope": "full", "board": "all", "limit": 0, "total_scanned": 1000},
        "data_quality": {
            "status": "degraded",
            "coverage_pct": 87.0,
            "end_trade_date": "2026-07-03",
            "warnings": ["13只股票拉取失败", "2只股票交易日不匹配", "数据覆盖率 87.0%"],
        },
    }

    assert tool_result_brief_lines("screen_stocks", ok_result, max_lines=1) == [
        "快扫: main_chinext_star 前300只，实际扫描300只；数据: 2026-07-03 覆盖100.0%(可靠)；筛选偏好: 风格=趋势,低吸(部分命中)"
    ]
    assert tool_result_brief_lines("screen_stocks", degraded_result, max_lines=1) == [
        "全量: all 扫描1000只；数据: 2026-07-03 覆盖87.0%(降级)，13只股票拉取失败；2只股票交易日不匹配"
    ]


def test_screen_stocks_preview_surfaces_strategy_policy_governance():
    result = {
        "scan_scope": {"scope": "bounded", "board": "all", "limit": 300, "total_scanned": 300},
        "strategy_policy": {
            "dynamic_mode": "shadow",
            "execution_policy": "shadow",
            "policy_weight_active_scope": "尾盘+漏斗shadow",
            "selection_action_count": 1,
            "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
            "formal_dynamic_allowed": False,
            "next_action": "manual_review_dynamic_on",
            "signal_weights": {"lps": 0.5},
            "attribution_signal_weights": {"lps": 0.5},
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=4)

    assert preview["strategy_policy"]["selection_action_count"] == 1
    assert preview["strategy_policy"]["active_scope"] == "尾盘+漏斗shadow"
    assert preview["strategy_policy"]["dynamic_mode"] == "shadow 对照(shadow)"
    assert preview["strategy_policy"]["dynamic_mode_raw"] == "shadow"
    assert preview["strategy_policy"]["execution_policy"] == "shadow 对照(shadow)"
    assert preview["strategy_policy"]["execution_policy_raw"] == "shadow"
    assert preview["strategy_policy"]["next_action"] == "进入人工晋级评审（非正式生效）"
    assert preview["strategy_policy"]["next_action_raw"] == "manual_review_dynamic_on"
    assert "candidate_lane=trend_pullback" in preview["strategy_policy"]["selection_action_summary"]
    assert "策略治理: 候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75" in lines


def test_screen_stocks_brief_lines_surface_candidate_risk_status():
    result = {
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "primary_pick": {
                "code": "300750",
                "name": "宁德时代",
                "priority_score": 12.5,
                "shadow_score": 4.2,
                "quality_factors": ["高优先级研报候选", "趋势线"],
                "risk_factors": ["大盘风险闸门关闭"],
                "action_status": "blocked_by_market_gate",
                "next_step": "只观察，等待风险闸门重新打开",
            },
        },
        "top_candidates": [
            {
                "code": "000001",
                "name": "平安银行",
                "why": "触发:SOS；缩量回踩",
                "score": 8.5,
                "risk_factors": ["未进入本轮研报候选"],
                "action_status": "watch_only",
            }
        ],
    }

    lines = tool_result_brief_lines("screen_stocks", result)

    assert lines == [
        "本轮首选可进入 AI 研报复核: 300750 宁德时代",
        "候选结论: 阻断候选 300750 宁德时代 · 风险闸门关闭 · 证据: 优先分12.5；动态分4.2 · 亮点: 高优先级研报候选；趋势线 · 风险: 大盘风险闸门关闭 · 下一步: 只观察，等待风险闸门重新打开",
        "000001 平安银行 · 观察池 · 证据: 触发分8.5 · 亮点: 触发:SOS；缩量回踩 · 风险: 未进入本轮研报候选",
    ]


def test_screen_stocks_brief_lines_surface_next_tool_handoff():
    result = {
        "scan_scope": {"scope": "bounded", "board": "main_chinext_star", "limit": 300, "total_scanned": 300},
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代",
            "primary_pick": {
                "code": "300750",
                "name": "宁德时代",
                "candidate_shadow_score": 92.0,
                "candidate_shadow_grade": "S",
                "action_status": "ready_for_ai_review",
            },
        },
        "next_tool": {
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["300750", "000001"]},
            "reason": "首选候选已通过市场闸门，可进入 AI 研报复核",
        },
    }

    lines = tool_result_brief_lines("screen_stocks", result, max_lines=4)

    assert "" not in lines
    assert lines == [
        "快扫: main_chinext_star 前300只，实际扫描300只",
        "本轮首选可进入 AI 研报复核: 300750 宁德时代",
        "下一工具: generate_ai_report(stock_codes=300750,000001) · 首选候选已通过市场闸门，可进入 AI 研报复核",
        "候选结论: 首选 300750 宁德时代 · 可进入AI复核 · 证据: 候选影子S/92",
    ]


def test_screen_stocks_brief_lines_surface_diagnosis_review_chain():
    result = {
        "scan_scope": {"scope": "bounded", "board": "all", "limit": 1200, "total_scanned": 1200},
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 002436 兴森科技",
            "primary_pick": {
                "code": "002436",
                "name": "兴森科技",
                "priority_score": 111.08,
                "action_status": "confirmation_required",
            },
        },
        "diagnosis_targets": [
            {
                "tool": "analyze_stock",
                "args": {"code": "002436", "mode": "diagnose"},
                "reason": "研报候选先做个股结构复核",
            }
        ],
        "next_tool": {
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["002436", "002245"]},
            "reason": "首选候选已通过市场闸门，可进入 AI 研报复核",
        },
    }

    lines = tool_result_brief_lines("screen_stocks", result, max_lines=4)

    assert lines == [
        "快扫: all 前1200只，实际扫描1200只",
        "本轮首选可进入 AI 研报复核: 002436 兴森科技",
        "复核链路: analyze_stock(code=002436, mode=diagnose) → generate_ai_report(stock_codes=002436,002245) · 先结构诊断，再研报复核",
        "候选结论: 待确认候选 002436 兴森科技 · 等待确认 · 证据: 优先分111.08",
    ]


def test_screen_stocks_brief_lines_surface_bounded_scan_scope():
    result = {
        "style_preference": {"raw": "pullback", "styles": ["pullback"]},
        "theme_preference": {"raw": "机器人", "theme": "机器人"},
        "scan_scope": {
            "scope": "bounded",
            "board": "all",
            "limit": 1200,
            "total_scanned": 1200,
            "financial_metrics": "skipped_quick_scan",
            "financial_metrics_count": 0,
        },
        "selection_brief": {"headline": "本轮只有观察候选: 002326 永太科技"},
    }

    lines = tool_result_brief_lines("screen_stocks", result)

    assert lines[:2] == [
        "快扫: all 前1200只，实际扫描1200只，财务过滤: 快扫跳过；筛选偏好: 风格=低吸(未命中)；主题=机器人(未命中)",
        "本轮只有观察候选: 002326 永太科技",
    ]


def test_screen_stocks_brief_lines_surface_full_scan_scope():
    result = {
        "scan_scope": {
            "scope": "full",
            "board": "main_chinext_star",
            "limit": 0,
            "total_scanned": 5002,
            "financial_metrics": "available",
            "financial_metrics_count": 4960,
        },
        "selection_brief": {"headline": "本轮只有观察候选: 002326 永太科技"},
    }

    lines = tool_result_brief_lines("screen_stocks", result)

    assert lines[:2] == [
        "全量: main_chinext_star 扫描5002只，财务过滤: 4960只",
        "本轮只有观察候选: 002326 永太科技",
    ]


def test_screen_stocks_preview_and_brief_lines_surface_etf_candidates():
    result = {
        "scan_scope": {
            "scope": "bounded",
            "board": "all",
            "limit": 1200,
            "total_scanned": 1200,
            "financial_metrics": "skipped_quick_scan",
            "financial_metrics_count": 0,
        },
        "selection_brief": {"headline": "本轮股票候选偏观察"},
        "etf_enhancement": {"pool": 2, "fetched": 2, "l2_passed": 1, "strong_candidates": 1},
        "etf_candidates": [
            {
                "code": "512480",
                "name": "半导体ETF",
                "sector": "半导体",
                "score": 12.3,
                "ret3": 2.1,
                "ret20": 10.5,
                "vol_ratio": 1.8,
                "channel": "主升通道",
            }
        ],
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=3)

    assert preview["etf_enhancement"]["l2_passed"] == 1
    assert preview["etf_candidates"][0]["code"] == "512480"
    assert lines == [
        "快扫: all 前1200只，实际扫描1200只，财务过滤: 快扫跳过",
        "本轮股票候选偏观察",
        "ETF强势池: 池2 → 拉取2 → L2强势1；候选: 512480 半导体ETF",
    ]


def test_screen_stocks_preview_merges_entry_risk_flags_into_visible_risks():
    result = {
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 000012 高分带风险",
            "primary_pick": {
                "code": "000012",
                "name": "高分带风险",
                "candidate_shadow_score": 91.0,
                "candidate_shadow_grade": "S",
                "entry_quality_risk_flags": ["短线涨幅偏快", "量能未确认"],
                "action_status": "ready_for_ai_review",
                "next_step": "生成 AI 研报",
            },
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["candidate_conclusion"]["risk_factors"] == ["短线涨幅偏快", "量能未确认"]
    assert "风险: 短线涨幅偏快；量能未确认" in lines[1]


def test_screen_stocks_preview_surfaces_daily_trap_reason_as_risk():
    result = {
        "selection_brief": {
            "primary_pick": {
                "code": "002217",
                "name": "合力泰",
                "candidate_shadow_score": 84.0,
                "candidate_shadow_grade": "A",
                "daily_trap_reason": "日线放量上影(2.6x)",
                "action_status": "watch_only",
                "next_step": "等待回踩确认",
            },
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["selection_brief"]["primary_pick"]["daily_trap_reason"] == "日线放量上影(2.6x)"
    assert preview["candidate_conclusion"]["risk_factors"] == ["日线放量上影(2.6x)"]
    assert "风险: 日线放量上影(2.6x)" in lines[0]


def test_screen_stocks_preview_prioritizes_ready_candidate_over_first_watch():
    result = {
        "report_candidates": [
            {
                "code": "000013",
                "name": "观察候选",
                "action_status": "watch_only",
                "candidate_shadow_score": 96.0,
            },
            {
                "code": "000014",
                "name": "高质量候选",
                "action_status": "ready_for_ai_review",
                "candidate_shadow_score": 92.0,
                "candidate_shadow_grade": "S",
                "next_step": "生成 AI 研报",
            },
        ]
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["candidate_conclusion"]["code"] == "000014"
    assert lines[0].startswith("候选结论: 首选 000014 高质量候选")
    assert "候选影子S/92" in lines[0]


def test_screen_stocks_preview_surfaces_theme_context_and_event_candidate():
    result = {
        "theme_context": {
            "event_mainlines": "机器人 0.82/爆发",
            "today_activity": "机器人 0.76/活跃",
            "theme_radar": "人形机器人 0.78/confirmed",
            "theme_radar_source": "current",
            "hot_concepts": ["机器人", "灵巧手"],
        },
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 000012 事件机器人",
            "primary_pick": {
                "code": "000012",
                "name": "事件机器人",
                "priority_score": 13.2,
                "strategic_theme": "机器人",
                "theme_source": "ths_hot_event",
                "theme_event_id": "evt-robot",
                "theme_event_reason": "灵巧手",
                "quality_factors": ["事件主线:机器人", "高优先级研报候选"],
                "action_status": "ready_for_ai_review",
                "next_step": "生成 AI 研报",
            },
        },
    }

    preview = tool_result_preview("screen_stocks", result)
    lines = tool_result_brief_lines("screen_stocks", result)

    assert '"theme_context": {"event_mainlines": "机器人 0.82/爆发"' in preview
    assert '"strategic_theme": "机器人"' in preview
    assert '"theme_event_id": "evt-robot"' in preview
    assert "事件主线: 机器人(灵巧手)" in preview
    assert lines == [
        "本轮首选可进入 AI 研报复核: 000012 事件机器人",
        "主题上下文: 事件主线: 机器人 0.82/爆发；异动主题: 机器人 0.76/活跃",
        "候选结论: 首选 000012 事件机器人 · 可进入AI复核 · 证据: 优先分13.2 · 事件主线: 机器人(灵巧手) · 亮点: 事件主线:机器人；高优先级研报候选 · 下一步: 生成 AI 研报",
    ]


def test_screen_stocks_preview_surfaces_theme_preference_and_match():
    result = {
        "theme_preference": {"raw": "机器人", "theme": "机器人"},
        "top_candidates": [
            {
                "code": "000002",
                "name": "机器人股",
                "theme_match": True,
                "theme_match_score": 1,
                "theme_match_reasons": ["主题偏好: 机器人"],
                "quality_factors": ["主题偏好: 机器人"],
            }
        ],
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
    assert preview["preference_match"] == {"theme": "hit"}
    assert preview["top_candidates"][0]["theme_match"] is True
    assert preview["top_candidates"][0]["theme_match_reasons"] == ["主题偏好: 机器人"]
    assert lines == [
        "筛选偏好: 主题=机器人",
        "候选结论: 候选 000002 机器人股 · 亮点: 主题偏好: 机器人",
    ]


def test_screen_stocks_brief_prioritizes_preference_reasons():
    result = {
        "style_preference": {"raw": "trend", "styles": ["trend"]},
        "theme_preference": {"raw": "机器人", "theme": "机器人"},
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 000012 机器人候选",
            "primary_pick": {
                "code": "000012",
                "name": "机器人候选",
                "action_status": "ready_for_ai_review",
                "style_match_reasons": ["趋势偏好: 趋势线"],
                "theme_match_reasons": ["主题偏好: 机器人"],
                "quality_factors": ["高质量研报候选", "候选影子评级 S", "入场质量评级 A", "主升阶段"],
            },
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["style_preference"] == {"raw": "trend", "styles": ["trend"]}
    assert preview["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
    assert preview["preference_match"] == {"style": "hit", "theme": "hit"}
    assert preview["candidate_conclusion"]["quality_factors"][:4] == [
        "趋势偏好: 趋势线",
        "主题偏好: 机器人",
        "高质量研报候选",
        "候选影子评级 S",
    ]
    assert lines == [
        "本轮首选可进入 AI 研报复核: 000012 机器人候选",
        "筛选偏好: 风格=趋势；主题=机器人",
        "候选结论: 首选 000012 机器人候选 · 可进入AI复核 · 亮点: 趋势偏好: 趋势线；主题偏好: 机器人",
    ]


def test_screen_stocks_brief_surfaces_preference_miss():
    result = {
        "style_preference": {"raw": "trend", "styles": ["trend"]},
        "theme_preference": {"raw": "机器人", "theme": "机器人"},
        "selection_brief": {
            "headline": "本轮首选可进入 AI 研报复核: 000012 非主题候选",
            "primary_pick": {
                "code": "000012",
                "name": "非主题候选",
                "action_status": "ready_for_ai_review",
                "quality_factors": ["高质量研报候选"],
            },
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result)

    assert preview["preference_match"] == {"style": "miss", "theme": "miss"}
    assert preview["candidate_conclusion"]["risk_factors"] == [
        "风格偏好未命中: 趋势",
        "主题偏好未命中: 机器人",
    ]
    assert lines == [
        "本轮首选可进入 AI 研报复核: 000012 非主题候选",
        "筛选偏好: 风格=趋势(未命中)；主题=机器人(未命中)",
        "候选结论: 首选 000012 非主题候选 · 可进入AI复核 · 亮点: 高质量研报候选 · 风险: 风格偏好未命中: 趋势；主题偏好未命中: 机器人",
    ]


def test_screen_stocks_brief_marks_primary_miss_even_when_pool_has_preference_hit():
    result = {
        "theme_preference": {"raw": "机器人", "theme": "机器人"},
        "preference_match": {"theme": "hit"},
        "selection_brief": {
            "primary_pick": {
                "code": "000012",
                "name": "非主题候选",
                "action_status": "ready_for_ai_review",
                "quality_factors": ["高质量研报候选"],
            },
            "preference_alternatives": [
                {
                    "code": "000099",
                    "name": "机器人观察",
                    "action_status": "watch_only",
                    "theme_match": True,
                    "theme_match_reasons": ["主题偏好: 机器人"],
                }
            ],
        },
        "top_candidates": [
            {
                "code": "000099",
                "name": "机器人观察",
                "action_status": "watch_only",
                "theme_match": True,
                "theme_match_reasons": ["主题偏好: 机器人"],
            }
        ],
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=2)

    assert preview["preference_match"] == {"theme": "hit"}
    assert preview["selection_brief"]["preference_alternatives"][0]["code"] == "000099"
    assert preview["candidate_conclusion"]["risk_factors"] == ["主题偏好未命中: 机器人"]
    assert lines == [
        "筛选偏好: 主题=机器人；偏好命中观察: 000099 机器人观察",
        "候选结论: 首选 000012 非主题候选 · 可进入AI复核 · 亮点: 高质量研报候选 · 风险: 主题偏好未命中: 机器人",
    ]


def test_screen_stocks_brief_surfaces_missing_style_in_combined_preference():
    result = {
        "style_preference": {"raw": "trend,pullback", "styles": ["trend", "pullback"]},
        "selection_brief": {
            "primary_pick": {
                "code": "000012",
                "name": "纯趋势候选",
                "action_status": "ready_for_ai_review",
                "style_match": True,
                "style_match_styles": ["trend"],
                "style_match_reasons": ["趋势偏好: 趋势线", "趋势偏好: 主升阶段"],
                "quality_factors": ["高质量研报候选"],
            },
        },
    }

    preview = json.loads(tool_result_preview("screen_stocks", result))
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=2)

    assert preview["preference_match"] == {"style": "partial"}
    assert preview["candidate_conclusion"]["risk_factors"] == ["风格偏好未命中: 低吸"]
    assert lines == [
        "筛选偏好: 风格=趋势,低吸(部分命中)",
        (
            "候选结论: 首选 000012 纯趋势候选 · 可进入AI复核 · "
            "亮点: 趋势偏好: 趋势线；趋势偏好: 主升阶段 · 风险: 风格偏好未命中: 低吸"
        ),
    ]


def test_screen_stocks_preview_surfaces_data_quality_gate():
    result = {
        "selection_brief": {
            "headline": "本轮有候选，但数据质量未过关: 300750 宁德时代",
            "primary_pick": {
                "code": "300750",
                "name": "宁德时代",
                "priority_score": 12.5,
                "quality_factors": ["高优先级研报候选"],
                "risk_factors": ["不要直接据此选股，先重跑或缩小扫描范围"],
                "action_status": "blocked_by_data_quality",
                "next_step": "数据质量不足，先重跑或缩小扫描范围",
            },
        },
        "action_plan": {
            "new_buy_allowed": False,
            "ai_review_allowed": False,
            "data_quality_gate": {
                "status": "degraded",
                "reason": "不要直接据此选股，先重跑或缩小扫描范围",
            },
            "review_targets": {
                "codes": ["300750"],
                "status": "blocked_by_data_quality",
                "reason": "不要直接据此选股，先重跑或缩小扫描范围",
            },
        },
    }

    preview = tool_result_preview("screen_stocks", result)
    lines = tool_result_brief_lines("screen_stocks", result)

    assert '"ai_review_allowed": false' in preview
    assert '"data_quality_gate": {"status": "degraded"' in preview
    assert '"status": "blocked_by_data_quality"' in preview
    assert "候选结论: 阻断候选 300750 宁德时代" in preview
    assert "护栏: 不要直接据此选股，先重跑或缩小扫描范围" in preview
    assert lines == [
        "本轮有候选，但数据质量未过关: 300750 宁德时代",
        "候选结论: 阻断候选 300750 宁德时代 · 数据质量未过关 · 证据: 优先分12.5 · 亮点: 高优先级研报候选 · 风险: 不要直接据此选股，先重跑或缩小扫描范围 · 护栏: 不要直接据此选股，先重跑或缩小扫描范围 · 下一步: 数据质量不足，先重跑或缩小扫描范围",
    ]


def test_screen_stocks_preview_surfaces_decision_state():
    summary = (
        "筛股决策: 好股观察 · 首选: 002326 永太科技 · 市场新增: 关 · 候选直买: 关 · "
        "AI复核: 不可 · 原因: 市场闸门关闭 · 下一步: 只观察"
    )
    result = {
        "selection_brief": {"headline": "本轮有强候选，但市场闸门未打开: 002326 永太科技"},
        "decision_state": {
            "status": "blocked_by_market_gate",
            "label": "好股观察",
            "trade_readiness": "observe_only",
            "new_buy_allowed": False,
            "ai_review_allowed": False,
            "primary": "002326 永太科技",
            "reason": "市场闸门关闭",
            "next_step": "只观察",
            "summary": summary,
        },
    }

    preview = tool_result_preview("screen_stocks", result)
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=2)

    assert '"decision_state": {"status": "blocked_by_market_gate"' in preview
    assert '"trade_readiness": "observe_only"' in preview
    assert lines == ["本轮有强候选，但市场闸门未打开: 002326 永太科技", summary]


def test_screen_stocks_preview_surfaces_quality_gate():
    reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    result = {
        "selection_brief": {
            "headline": "本轮只有观察候选: 000013 低质量候选",
            "primary_pick": {
                "code": "000013",
                "name": "低质量候选",
                "risk_adjusted_quality_score": 65.0,
                "risk_factors": [reason],
                "action_status": "watch_only",
                "next_step": "观察池跟踪，暂不进入本轮AI复核",
            },
        },
        "action_plan": {
            "new_buy_allowed": False,
            "ai_review_allowed": False,
            "quality_gate": {
                "status": "blocked_by_quality_gate",
                "reason": reason,
                "blocked_count": 1,
            },
            "review_targets": {
                "codes": [],
                "status": "blocked_by_quality_gate",
                "reason": reason,
            },
            "diagnosis_targets": [
                {
                    "tool": "analyze_stock",
                    "args": {"code": "000013", "mode": "diagnose"},
                    "code": "000013",
                    "name": "低质量候选",
                    "reason": "观察候选先做个股结构诊断",
                }
            ],
        },
        "diagnosis_targets": [
            {
                "tool": "analyze_stock",
                "args": {"code": "000013", "mode": "diagnose"},
                "code": "000013",
                "name": "低质量候选",
                "reason": "观察候选先做个股结构诊断",
            }
        ],
        "top_candidates": [
            {
                "code": "000013",
                "name": "低质量候选",
                "selected_for_report": False,
                "raw_selected_for_report": True,
                "risk_adjusted_quality_score": 65.0,
                "action_status": "watch_only",
                "next_step": "观察池跟踪，暂不进入本轮AI复核",
            }
        ],
    }

    preview = tool_result_preview("screen_stocks", result)
    lines = tool_result_brief_lines("screen_stocks", result)

    assert '"quality_gate": {"status": "blocked_by_quality_gate"' in preview
    assert '"ai_review_allowed": false' in preview
    assert '"selected_for_report": false' in preview
    assert '"raw_selected_for_report": true' in preview
    assert '"diagnosis_targets": [{"tool": "analyze_stock"' in preview
    assert '"args": {"code": "000013", "mode": "diagnose"}' in preview
    assert "候选结论: 观察候选 000013 低质量候选" in preview
    assert "护栏: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00" in preview
    assert lines == [
        "本轮只有观察候选: 000013 低质量候选",
        "候选结论: 观察候选 000013 低质量候选 · 观察池 · 证据: 风险调整分65 · 风险: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00 · 护栏: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00 · 下一步: 观察池跟踪，暂不进入本轮AI复核",
    ]


def test_web_background_funnel_screen_preview_surfaces_top_level_quality_gate():
    reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    result = {
        "job_kind": "funnel_screen",
        "symbols_for_report": [],
        "watch_candidates": [
            {
                "code": "000013",
                "name": "低质量候选",
                "risk_adjusted_quality_score": 65.0,
                "risk_factors": [reason],
                "action_status": "watch_only",
                "next_step": "观察池跟踪，暂不进入本轮AI复核",
            }
        ],
        "quality_gate": {"status": "blocked_by_quality_gate", "reason": reason, "blocked_count": 1},
    }

    preview = tool_result_preview("web_background_job", result)
    lines = tool_result_brief_lines("web_background_job", result)

    assert '"job_kind": "funnel_screen"' in preview
    assert '"quality_gate": {"status": "blocked_by_quality_gate"' in preview
    assert "候选结论: 观察候选 000013 低质量候选" in preview
    assert "护栏: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00" in preview
    assert lines == [
        "候选结论: 观察候选 000013 低质量候选 · 观察池 · 证据: 风险调整分65 · 风险: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00 · 护栏: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00 · 下一步: 观察池跟踪，暂不进入本轮AI复核"
    ]


def test_screen_stocks_brief_lines_use_symbols_for_report_handoff():
    result = {
        "symbols_for_report": [
            {
                "code": "300750",
                "name": "宁德时代",
                "candidate_shadow_score": 92.0,
                "candidate_shadow_grade": "S",
                "action_status": "ready_for_ai_review",
                "next_step": "生成 AI 研报",
            }
        ]
    }

    lines = tool_result_brief_lines("screen_stocks", result)

    assert lines == [
        "候选结论: 首选 300750 宁德时代 · 可进入AI复核 · 证据: 候选影子S/92 · 下一步: 生成 AI 研报",
    ]


def test_screen_stocks_preview_prioritizes_report_candidates_over_watch():
    result = {
        "selection_brief": {
            "primary_pick": {"code": "000013", "name": "观察候选", "action_status": "watch_only"},
            "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
        },
        "symbols_for_report": [],
        "report_candidates": [
            {
                "code": "000014",
                "name": "高质量候选",
                "candidate_shadow_score": 92.0,
                "candidate_shadow_grade": "S",
                "action_status": "ready_for_ai_review",
                "next_step": "生成 AI 研报",
            }
        ],
        "watch_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
    }

    preview = tool_result_preview("screen_stocks", result)
    lines = tool_result_brief_lines("screen_stocks", result)

    assert '"report_candidates": [{"code": "000014"' in preview
    assert '"watch_candidates": [{"code": "000013"' in preview
    assert "候选结论: 首选 000014 高质量候选" in preview
    assert lines[0] == "候选结论: 首选 000014 高质量候选 · 可进入AI复核 · 证据: 候选影子S/92 · 下一步: 生成 AI 研报"


def test_recommendation_event_eval_large_result_preview_preserves_policy_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {
        "ok": True,
        "job_kind": "recommendation_event_eval",
        "result_summary": (
            "推荐事件评估: ready=12/20, hit=60%, ranking_decision=candidate\n"
            "排序接入候选: candidate_shadow_then_score top1 已通过样本/lift/风险门槛\n"
            "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代"
        ),
        "summary": {
            "all": {"rows_ready": 12, "rows_total": 20, "hit_rate_pct": 60.0},
            "ranking_decision": {
                "status": "candidate",
                "recommended_strategy": "candidate_shadow_then_score",
                "recommended_top_k": 1,
                "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
            },
        },
        "policy_selection": {
            "status": "candidate",
            "selection_strategy": "candidate_shadow_then_score",
            "top_k": 1,
            "recommend_date": 20260601,
            "uses_promoted_ranking": True,
            "action_plan": {
                "primary_action": "generate_ai_report",
                "candidate_action": "generate_ai_report",
                "new_buy_allowed": False,
                "ai_review_allowed": True,
                "trade_readiness": "research_only",
                "review_status": "ready_for_ai_review",
                "reason": "只读推荐事件评估已通过排序接入门槛，可进入 AI 研报；不直接触发买入",
                "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                "next_tool": {"tool": "generate_ai_report", "args": {"stock_codes": ["300750"]}},
            },
            "picks": [
                {
                    "rank": 1,
                    "code": "300750",
                    "name": "宁德时代",
                    "funnel_score": 89.5,
                    "candidate_shadow_score": 92.0,
                    "candidate_shadow_grade": "S",
                    "entry_quality_score": 84.0,
                    "entry_quality_grade": "A",
                    "candidate_quality_score": 92.0,
                    "risk_adjusted_quality_score": 87.0,
                    "entry_risk_penalty": 5.0,
                    "action_status": "ready_for_ai_review",
                    "quality_factors": ["候选影子评级 S"],
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                    "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                    "label_ready": False,
                    "label_status": "partial_window",
                }
            ],
        },
        "candidate_guard_summary": {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "reason": "候选标签未成熟，禁止直接买入",
                    "action_status": "ready_for_ai_review",
                    "label_ready": False,
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                }
            ],
        },
        "events": [{"code": f"{idx:06d}", "blob": "x" * 200} for idx in range(20)],
    }

    content = format_tool_result_for_context("web_background_job", "call_eval", result, max_chars=1000)
    preview = tool_result_preview("web_background_job", result)
    lines = tool_result_brief_lines("web_background_job", result)
    handoff_lines = tool_result_brief_lines("web_background_job", result, max_lines=4)

    assert "result_ref:" in content
    assert "ranking_decision=candidate" in content
    assert "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代" in content
    assert '"candidate_shadow_grade": "S"' in preview
    assert '"trade_readiness": "research_only"' in preview
    assert '"new_buy_allowed": false' in preview
    assert '"action_status": "ready_for_ai_review"' in preview
    assert '"candidate_conclusion"' in preview
    assert "候选结论: 受限复核候选 300750 宁德时代" in preview
    assert '"risk_adjusted_quality_score": 87.0' in preview
    assert '"quality_factors": ["候选影子评级 S"]' in preview
    assert '"risk_factors": ["最新候选的未来窗口标签尚未成熟"]' in preview
    assert "证据: 漏斗分89.5；候选影子S/92；入场A/84；风险调整分87" in preview
    assert '"candidate_guard_summary"' in preview
    assert "候选标签未成熟，禁止直接买入" in preview
    assert "护栏: 候选标签未成熟" in preview
    assert "最新候选的未来窗口标签尚未成熟" in preview
    assert '"events"' not in content
    assert lines == [
        "推荐事件评估: ready=12/20, hit=60%, ranking_decision=candidate",
        "候选结论: 受限复核候选 300750 宁德时代 · 可进入AI复核 · 证据: 漏斗分89.5；候选影子S/92；入场A/84；风险调整分87 · 亮点: 候选影子评级 S · 风险: 最新候选的未来窗口标签尚未成熟 · 护栏: 候选标签未成熟，禁止直接买入 · 下一步: 生成 AI 研报并结合持仓形成攻防决策",
        "候选护栏: 1只禁止直接买入 · 300750 宁德时代(候选标签未成熟，禁止直接买入)",
    ]
    assert handoff_lines == [
        *lines,
        "下一工具: generate_ai_report(stock_codes=300750) · 只读推荐事件评估已通过排序接入门槛，可进入 AI 研报；不直接触发买入",
    ]


def test_generate_ai_report_large_result_preview_preserves_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    report_text = "# 研报\n" + "量价结构良好。" * 500
    result = {
        "ok": True,
        "reason": "ok",
        "report_text": report_text,
        "model": "gpt-test",
        "stock_count": 2,
        "reviewed_codes": ["000001", "300750"],
        "reviewed_symbols": [
            {"code": "000001", "name": "平安银行", "tag": "chat_request"},
            {
                "code": "300750",
                "name": "宁德时代",
                "tag": "chat_request",
                "risk_factors": ["大盘风险闸门关闭"],
                "action_status": "blocked_by_market_gate",
            },
        ],
        "candidate_guard_summary": {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "reason": "候选状态 blocked_by_market_gate 不允许直接买入",
                    "action_status": "blocked_by_market_gate",
                    "risk_factors": ["大盘风险闸门关闭"],
                }
            ],
        },
        "next_action": "研报已完成；候选存在禁止直接买入边界，下一步只进入组合攻防复核",
        "next_tool": {
            "tool": "generate_strategy_decision",
            "args": {},
            "reason": "研报已完成，可继续生成组合攻防复核；候选护栏禁止把观察/未成熟候选直接写成买入",
        },
    }

    content = format_tool_result_for_context("generate_ai_report", "call_report", result, max_chars=1000)
    lines = tool_result_brief_lines("generate_ai_report", result, max_lines=3)
    handoff_lines = tool_result_brief_lines("generate_ai_report", result, max_lines=4)

    assert "result_ref:" in content
    assert '"reviewed_codes": ["000001", "300750"]' in content
    assert '"risk_factors": ["大盘风险闸门关闭"]' in content
    assert '"action_status": "blocked_by_market_gate"' in content
    assert '"candidate_guard_summary"' in content
    assert '"candidate_conclusion": {"line": "候选结论: 阻断候选 300750 宁德时代' in content
    assert "护栏: 候选状态 blocked_by_market_gate 不允许直接买入" in content
    assert '"direct_buy_blocked_count": 1' in content
    assert '"tool": "generate_strategy_decision"' in content
    assert '"report_excerpt": "# 研报' in content
    assert report_text not in content
    assert lines == [
        "AI研报: reviewed=2, model=gpt-test, next=研报已完成；候选存在禁止直接买入边界，下一步只进入组合攻防复核",
        "候选结论: 阻断候选 300750 宁德时代 · 风险闸门关闭 · 风险: 大盘风险闸门关闭 · 护栏: 候选状态 blocked_by_market_gate 不允许直接买入 · 下一步: 研报已完成；候选存在禁止直接买入边界，下一步只进入组合攻防复核",
        "候选护栏: 1只禁止直接买入 · 300750 宁德时代(候选状态 blocked_by_market_gate 不允许直接买入)",
    ]
    assert handoff_lines == [
        *lines,
        "下一工具: generate_strategy_decision() · 研报已完成，可继续生成组合攻防复核；候选护栏禁止把观察/未成熟候选直接写成买入",
    ]
    stored = list((tmp_path / "tool-results").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text(encoding="utf-8"))["report_text"] == report_text


def test_ai_report_preview_prioritizes_ready_reviewed_candidate_over_guarded_watch():
    result = {
        "reviewed_symbols": [
            {
                "code": "000014",
                "name": "高质量候选",
                "action_status": "ready_for_ai_review",
                "candidate_shadow_score": 92.0,
                "candidate_shadow_grade": "S",
            }
        ],
        "candidate_guard_summary": {
            "direct_buy_blocked_count": 1,
            "candidates": [
                {
                    "code": "000013",
                    "name": "观察候选",
                    "reason": "候选状态 watch_only 不允许直接买入",
                    "action_status": "watch_only",
                    "candidate_shadow_score": 96.0,
                }
            ],
        },
    }

    preview = json.loads(tool_result_preview("generate_ai_report", result))
    lines = tool_result_brief_lines("generate_ai_report", result, max_lines=3)

    assert preview["candidate_conclusion"]["code"] == "000014"
    assert lines[1].startswith("候选结论: 首选 000014 高质量候选")
    assert lines[2].startswith("候选护栏: 1只禁止直接买入")


def test_ai_report_preview_surfaces_strategy_policy():
    result = {
        "ok": True,
        "model": "gpt-test",
        "reviewed_codes": ["000004"],
        "reviewed_symbols": [{"code": "000004", "name": "主线候选", "action_status": "ready_for_ai_review"}],
        "strategy_policy": {
            "dynamic_mode": "shadow",
            "policy_weight_active_scope": "尾盘+漏斗shadow",
            "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级",
            "attribution_signal_weights": {"lps": 0.5, "trend_pullback": 0.75},
        },
        "next_action": "研报已完成，可结合持仓和候选进入组合攻防决策",
    }

    preview = json.loads(tool_result_preview("generate_ai_report", result))
    lines = tool_result_brief_lines("generate_ai_report", result, max_lines=3)

    assert preview["strategy_policy"]["active_scope"] == "尾盘+漏斗shadow"
    assert "candidate_lane=trend_pullback" in preview["strategy_policy"]["selection_action_summary"]
    assert lines[0] == (
        "AI研报: reviewed=1, model=gpt-test, "
        "策略治理=候选源治理 1 项：candidate_lane=trend_pullback 降级, "
        "next=研报已完成，可结合持仓和候选进入组合攻防决策"
    )
    assert lines[1].startswith("候选结论: 首选 000004 主线候选")


def test_generate_strategy_decision_large_result_preview_preserves_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {
        "ok": True,
        "status": "skipped_notify_unconfigured",
        "reason": "skipped_notify_unconfigured",
        "report_source": "last_ai_report",
        "candidate_count": 1,
        "reviewed_codes": ["300750"],
        "reviewed_symbols": [
            {
                "code": "300750",
                "name": "宁德时代",
                "track": "Trend",
                "risk_factors": ["大盘风险闸门关闭"],
                "action_status": "blocked_by_market_gate",
            }
        ],
        "candidate_guard_summary": {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "reason": "候选状态 blocked_by_market_gate 不允许直接买入",
                    "action_status": "blocked_by_market_gate",
                    "risk_factors": ["大盘风险闸门关闭"],
                }
            ],
        },
        "screen_summary": {"report_candidates": 1},
        "decision_brief": {
            "next_action": "允许候选进入AI复核",
            "report_focus": [{"code": "300750", "risk_factors": ["大盘风险闸门关闭"]}],
        },
        "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
        "missing_credentials": ["TG_BOT_TOKEN", "TG_CHAT_ID"],
        "message": "已完成候选和研报交接，但未配置 Telegram。",
        "report_preview": "研报摘要" * 500,
    }

    content = format_tool_result_for_context("generate_strategy_decision", "call_strategy", result, max_chars=1000)
    lines = tool_result_brief_lines("generate_strategy_decision", result, max_lines=3)

    assert "result_ref:" in content
    assert '"report_source": "last_ai_report"' in content
    assert '"reviewed_codes": ["300750"]' in content
    assert '"risk_factors": ["大盘风险闸门关闭"]' in content
    assert '"action_status": "blocked_by_market_gate"' in content
    assert '"candidate_guard_summary"' in content
    assert '"candidate_conclusion": {"line": "候选结论: 阻断候选 300750 宁德时代' in content
    assert "护栏: 候选状态 blocked_by_market_gate 不允许直接买入" in content
    assert '"direct_buy_blocked_count": 1' in content
    assert '"missing_credentials": ["TG_BOT_TOKEN", "TG_CHAT_ID"]' in content
    assert "候选状态 blocked_by_market_gate 不允许直接买入" in content
    assert "补充 Telegram 配置后可生成并发送 OMS 工单" in content
    assert result["report_preview"] not in content
    assert lines == [
        "攻防决策: 未发送工单 · 来源: 上一轮AI研报 · 已复核: 1只 · 缺配置: TG_BOT_TOKEN,TG_CHAT_ID · 下一步: 补充 Telegram 配置后可生成并发送 OMS 工单",
        "候选结论: 阻断候选 300750 宁德时代 · 风险闸门关闭 · 风险: 大盘风险闸门关闭 · 护栏: 候选状态 blocked_by_market_gate 不允许直接买入 · 下一步: 补充 Telegram 配置后可生成并发送 OMS 工单",
        "候选护栏: 1只禁止直接买入 · 300750 宁德时代(候选状态 blocked_by_market_gate 不允许直接买入)",
    ]


def test_generate_strategy_decision_brief_surfaces_report_boundaries():
    result = {
        "ok": True,
        "status": "skipped_notify_unconfigured",
        "report_source": "last_ai_report",
        "candidate_count": 1,
        "reviewed_codes": ["002293"],
        "reviewed_symbols": [
            {
                "code": "002293",
                "name": "罗莱生活",
                "action_status": "ready_for_ai_review",
            }
        ],
        "report_preview": "# 攻防计划\n002293 罗莱生活：触发位 11.20；失效位 11.00；只做确认后的右侧。",
    }

    lines = tool_result_brief_lines("generate_strategy_decision", result, max_lines=3)

    assert lines == [
        "攻防决策: 未发送工单 · 来源: 上一轮AI研报 · 已复核: 1只",
        "候选结论: 首选 002293 罗莱生活 · 可进入AI复核",
        "研报边界: 002293 罗莱生活：触发位 11.20；失效位 11.00；只做确认后的右侧。",
    ]


def test_generate_strategy_decision_preview_surfaces_strategy_policy():
    result = {
        "ok": True,
        "status": "skipped_notify_unconfigured",
        "report_source": "last_ai_report",
        "candidate_count": 1,
        "reviewed_codes": ["000004"],
        "reviewed_symbols": [{"code": "000004", "name": "主线候选", "action_status": "ready_for_ai_review"}],
        "strategy_policy": {
            "dynamic_mode": "shadow",
            "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级",
            "signal_weights": {"trend_pullback": 0.75},
        },
        "next_action": "补充 Telegram 配置后可生成并发送 OMS 工单",
    }

    preview = json.loads(tool_result_preview("generate_strategy_decision", result))
    lines = tool_result_brief_lines("generate_strategy_decision", result, max_lines=3)

    assert preview["strategy_policy"]["signal_weights"] == {"trend_pullback": 0.75}
    assert "candidate_lane=trend_pullback" in preview["strategy_policy"]["selection_action_summary"]
    assert lines[0] == (
        "攻防决策: 未发送工单 · 来源: 上一轮AI研报 · 已复核: 1只 · "
        "策略治理=候选源治理 1 项：candidate_lane=trend_pullback 降级 · "
        "下一步: 补充 Telegram 配置后可生成并发送 OMS 工单"
    )
    assert lines[1].startswith("候选结论: 首选 000004 主线候选")


def test_generate_strategy_decision_brief_labels_quality_gate_blocker():
    reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    result = {
        "ok": False,
        "status": "blocked_by_quality_gate",
        "reason": reason,
        "report_source": "blocked_by_screen_quality_gate",
        "candidate_count": 0,
        "reviewed_codes": [],
        "next_action": "先保留观察候选，等待风险调整质量分达标后再生成策略决策",
    }

    lines = tool_result_brief_lines("generate_strategy_decision", result, max_lines=2)

    assert lines == [
        (
            "攻防决策: 候选质量门槛未过 · 来源: 筛选质量门槛阻断 · 已复核: 0只 · "
            "原因: 000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00 · "
            "下一步: 先保留观察候选，等待风险调整质量分达标后再生成策略决策"
        )
    ]
