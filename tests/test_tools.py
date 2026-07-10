"""tools/ 层单元测试 — 测试 Phase 2 提取的纯逻辑 Tool 函数。"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from types import ModuleType, SimpleNamespace

import pandas as pd


def _money_flow_df(prev_close: float, latest_close: float, latest_amount: float) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=20, freq="D").strftime("%Y-%m-%d")
    close = [prev_close] * 19 + [latest_close]
    amount = [100_000_000.0] * 19 + [latest_amount]
    return pd.DataFrame({"date": dates, "close": close, "amount": amount})


def _benchmark_df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(closes), freq="D").strftime("%Y-%m-%d")
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "pct_chg": close.pct_change().fillna(0.0) * 100.0,
            "volume": [100_000_000.0] * len(closes),
        }
    )


def _benchmark_with_last_drop(drop_pct: float) -> pd.DataFrame:
    closes = [100.0 + i * 0.2 for i in range(220)]
    closes[-1] = closes[-2] * (1.0 + drop_pct / 100.0)
    return _benchmark_df(closes)


# ── utils.json_text ──


class TestJsonText:
    def test_parse_json_object_preserves_dict_identity(self):
        from utils.json_text import parse_json_object

        raw = {"key": {"nested": True}}

        assert parse_json_object(raw) is raw

    def test_parse_json_object_parses_object_string(self):
        from utils.json_text import parse_json_object

        assert parse_json_object('{"key": "value"}') == {"key": "value"}

    def test_parse_json_object_rejects_invalid_or_non_object_values(self):
        from utils.json_text import parse_json_object

        for raw in ('["value"]', "not-json", "", "   ", None, 42, ["value"]):
            assert parse_json_object(raw) == {}


# ── utils.env ──


class TestFunnelConfig:
    def test_parse_int_env_reads_env(self, monkeypatch):
        from utils.env import parse_int_env

        monkeypatch.setenv("_TEST_INT", "42")
        assert parse_int_env("_TEST_INT", 0) == 42

    def test_parse_int_env_fallback_on_missing(self, monkeypatch):
        from utils.env import parse_int_env

        monkeypatch.delenv("_TEST_INT", raising=False)
        assert parse_int_env("_TEST_INT", 7) == 7

    def test_parse_int_env_handles_float_string(self, monkeypatch):
        from utils.env import parse_int_env

        monkeypatch.setenv("_TEST_INT", "5.0")
        assert parse_int_env("_TEST_INT", 0) == 5

    def test_parse_bool_truthy(self):
        from utils.env import parse_bool

        for val in ("1", "true", "True", "yes", "on"):
            assert parse_bool(val) is True, f"Expected True for {val!r}"

    def test_parse_bool_falsy(self):
        from utils.env import parse_bool

        for val in ("0", "false", "no", "off", ""):
            assert parse_bool(val) is False, f"Expected False for {val!r}"


# ── tools/report_builder ──


class TestReportBuilder:
    def test_extract_ops_codes_from_markdown_happy_path(self):
        from tools.report_parser import extract_ops_codes_from_markdown

        report = (
            "# \u5904\u4e8e\u8d77\u8df3\u677f\n"
            "- 600056 \u4e2d\u56fd\u533b\u836f\n"
            "- 300632 \u5149\u83c6\u80a1\u4efd\n"
            "# \u903b\u8f91\u7834\u4ea7\n"
            "- 000001 \u5e73\u5b89\u94f6\u884c\n"
        )
        allowed = {"600056", "300632", "000001"}
        result = extract_ops_codes_from_markdown(report, allowed)
        assert result == ["600056", "300632"]
        assert "000001" not in result

    def test_extract_ops_codes_empty_report(self):
        from tools.report_parser import extract_ops_codes_from_markdown

        assert extract_ops_codes_from_markdown("", set()) == []

    def test_try_parse_structured_report_none_on_empty(self):
        from tools.report_parser import try_parse_structured_report

        assert try_parse_structured_report("", set(), {}) is None

    def test_extract_json_block_strips_fences(self):
        from utils.json_text import extract_json_block

        raw = '```json\n{"key": "value"}\n```'
        result = extract_json_block(raw)
        assert result == '{"key": "value"}'

    def test_extract_json_block_plain_json(self):
        from utils.json_text import extract_json_block

        raw = '{"a": 1}'
        assert extract_json_block(raw) == '{"a": 1}'

    def test_extract_operation_pool_codes_happy_path(self):
        from tools.report_parser import extract_operation_pool_codes

        report = "# \u5904\u4e8e\u8d77\u8df3\u677f\n- 600056 \u4e2d\u56fd\u533b\u836f\n"
        codes = extract_operation_pool_codes(report, ["600056", "300632"])
        assert "600056" in codes

    def test_extract_operation_pool_codes_deduplicates(self):
        from tools.report_parser import extract_operation_pool_codes

        report = "# \u5904\u4e8e\u8d77\u8df3\u677f\n- 600056 A\n- 600056 B\n"
        codes = extract_operation_pool_codes(report, ["600056"])
        assert codes == ["600056"]

    def test_extract_operation_pool_springboards_reads_gate_line(self):
        from tools.report_parser import extract_operation_pool_springboards

        report = (
            "# \u5904\u4e8e\u8d77\u8df3\u677f\n"
            "603373 \u5b89\u90a6\u62a4\u536b\n"
            "\u6ee1\u8db3\u7684\u786c\u95e8\u69db\uff1a A+C\n"
            "Plan A: \u6b21\u65e5\u7f29\u91cf\u56de\u8e29\u3002\n"
            "\n"
            "301348 \u84dd\u7bad\u7535\u5b50\n"
            "\u6ee1\u8db3\u7684\u786c\u95e8\u69db\uff1a C + \u677f\u5757\u5171\u632f\u66ff\u4ee3A\n"
            "# \u903b\u8f91\u7834\u4ea7\n"
            "000001 \u5e73\u5b89\u94f6\u884c\n"
            "\u6ee1\u8db3\u7684\u786c\u95e8\u69db\uff1a A+B+C\n"
        )

        result = extract_operation_pool_springboards(report, ["603373", "301348", "000001"])

        assert result["603373"]["springboard_combo"] == "A+C"
        assert result["603373"]["springboard_a"] is True
        assert result["603373"]["springboard_c"] is True
        assert result["301348"]["springboard_combo"] == "A+C"
        assert result["301348"]["springboard_evidence"]["llm_hard_gates"] == "C + \u677f\u5757\u5171\u632f\u66ff\u4ee3A"
        assert "000001" not in result


class TestAiReportTool:
    def test_generate_ai_report_returns_handoff_metadata(self, monkeypatch):
        from agents import report_tools

        captured = {}
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(
            report_tools,
            "code_to_name",
            lambda code: {"000001": "平安银行", "300750": "宁德时代"}.get(code, code),
        )

        def fake_run_ai_report(symbols_info, **kwargs):
            captured["symbols_info"] = symbols_info
            captured["kwargs"] = kwargs
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(["000001", " 300750 ", ""])

        assert result["ok"] is True
        assert result["reviewed_codes"] == ["000001", "300750"]
        assert result["reviewed_symbols"] == [
            {
                "code": "000001",
                "name": "平安银行",
                "tag": "chat_request",
                "selection_source": "explicit_report_input",
            },
            {
                "code": "300750",
                "name": "宁德时代",
                "tag": "chat_request",
                "selection_source": "explicit_report_input",
            },
        ]
        assert result["next_action"] == "研报已完成，可结合持仓和候选进入组合攻防决策"
        assert result["next_tool"]["tool"] == "generate_strategy_decision"
        assert captured["symbols_info"] == result["reviewed_symbols"]
        assert captured["kwargs"]["provider"] == "openai"
        assert captured["kwargs"]["model"] == "gpt-test"

    def test_generate_ai_report_reuses_screen_handoff_metadata(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "tag": "主线买点确认 | 威科夫候选",
                            "track": "Trend",
                            "stage": "Markup",
                            "candidate_lane": "launchpad",
                            "entry_type": "launchpad",
                            "priority_score": 12.5,
                            "candidate_quality_score": 92.0,
                            "risk_adjusted_quality_score": 87.0,
                            "entry_risk_penalty": 5.0,
                            "rank_reason": "研报候选#1；优先分 12.50",
                            "quality_factors": ["高优先级研报候选", "优先分 12.50"],
                            "risk_factors": ["大盘风险闸门关闭"],
                            "action_status": "blocked_by_market_gate",
                        }
                    ]
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(["300750"], tool_context=ctx)

        assert captured["symbols_info"][0]["track"] == "Trend"
        assert captured["symbols_info"][0]["candidate_lane"] == "launchpad"
        assert captured["symbols_info"][0]["entry_type"] == "launchpad"
        assert result["reviewed_symbols"][0]["priority_score"] == 12.5
        assert result["reviewed_symbols"][0]["candidate_quality_score"] == 92.0
        assert result["reviewed_symbols"][0]["risk_adjusted_quality_score"] == 87.0
        assert result["reviewed_symbols"][0]["entry_risk_penalty"] == 5.0
        assert result["reviewed_symbols"][0]["rank_reason"] == "研报候选#1；优先分 12.50"
        assert result["reviewed_symbols"][0]["quality_factors"] == ["高优先级研报候选", "优先分 12.50"]
        assert result["reviewed_symbols"][0]["risk_factors"] == ["大盘风险闸门关闭"]
        assert result["reviewed_symbols"][0]["action_status"] == "blocked_by_market_gate"
        assert result["candidate_guard_summary"] == {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "reason": "候选状态 blocked_by_market_gate 不允许直接买入",
                    "action_status": "blocked_by_market_gate",
                    "action_label": "风险闸门关闭",
                    "action_level": "blocked",
                    "direct_buy_allowed": False,
                    "risk_factors": ["大盘风险闸门关闭"],
                }
            ],
        }
        assert result["next_action"] == "研报已完成；候选存在禁止直接买入边界，下一步只进入组合攻防复核"
        assert "候选护栏禁止" in result["next_tool"]["reason"]
        assert ctx.state["last_ai_report"]["reviewed_codes"] == ["300750"]

    def test_generate_ai_report_carries_strategy_policy_from_screen_handoff(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": [{"code": "000004", "name": "主线候选", "candidate_lane": "trend_pullback"}],
                    "strategy_policy": {
                        "dynamic_mode": "shadow",
                        "execution_policy": "shadow",
                        "policy_weight_active_scope": "尾盘+漏斗shadow",
                        "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级",
                        "attribution_signal_weights": {"lps": 0.5, "trend_pullback": 0.75},
                        "next_action": "review_policy_actions",
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            assert symbols_info[0]["candidate_lane"] == "trend_pullback"
            return True, "ok", "# 主线候选研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["strategy_policy"]["policy_weight_active_scope"] == "尾盘+漏斗shadow"
        assert result["report_text"].startswith("## 策略治理上下文")
        assert "下一步: 先复核调权治理项" in result["report_text"]
        assert "执行模式: shadow 对照(shadow)" in result["report_text"]
        assert "candidate_lane=trend_pullback" in result["report_text"]
        assert "信号调权: lps=0.5, trend_pullback=0.75" in result["report_text"]
        assert "# 主线候选研报" in result["report_text"]
        assert ctx.state["last_ai_report"]["strategy_policy"]["execution_policy"] == "shadow"

    def test_generate_ai_report_accepts_candidate_object_inputs(self, monkeypatch):
        from agents import report_tools

        captured = {}
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(
            [
                {
                    "code": "000004",
                    "name": "主线候选",
                    "track": "Trend",
                    "candidate_lane": "mainline",
                    "priority_score": 11.0,
                    "score": 8.5,
                    "why": "趋势线 / 主线买点",
                }
            ]
        )

        assert result["reviewed_codes"] == ["000004"]
        assert result["reviewed_symbols"][0]["track"] == "Trend"
        assert result["reviewed_symbols"][0]["candidate_lane"] == "mainline"
        assert result["reviewed_symbols"][0]["priority_score"] == 11.0
        assert result["reviewed_symbols"][0]["score"] == 8.5
        assert captured["symbols_info"][0]["why"] == "趋势线 / 主线买点"

    def test_generate_ai_report_accepts_comma_separated_codes(self, monkeypatch):
        from agents import report_tools

        captured = {}
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(
            report_tools, "code_to_name", lambda code: {"000004": "主线候选", "000005": "二号候选"}[code]
        )

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report("000004, 000005")

        assert result["reviewed_codes"] == ["000004", "000005"]
        assert [row["name"] for row in captured["symbols_info"]] == ["主线候选", "二号候选"]
        assert [row["selection_source"] for row in captured["symbols_info"]] == [
            "explicit_report_input",
            "explicit_report_input",
        ]

    def test_generate_ai_report_corrects_llm_code_name_mismatch(self, monkeypatch):
        from agents import report_tools

        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(report_tools, "code_to_name", lambda code: {"002774": "快意电梯"}[code])

        def fake_run_ai_report(symbols_info, **_kwargs):
            assert symbols_info[0]["name"] == "快意电梯"
            return True, "ok", "### 近就绪\n- **002774（康尼机电）**\n  触发位 15.09。"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report("002774")

        assert "002774（快意电梯）" in result["report_text"]
        assert "康尼机电" not in result["report_text"]

    def test_generate_ai_report_normalizes_exchange_wrapped_codes_from_text(self, monkeypatch):
        from agents import report_tools

        captured = {}
        names = {
            "600519": "贵州茅台",
            "000001": "平安银行",
            "000390": "晨光",
            "833575": "北交样本",
        }
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(report_tools, "code_to_name", lambda code: names[code])

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report("候选 SH600519、sz000001、000390.SZ、833575.BJ，重复 sh600519")

        assert result["reviewed_codes"] == ["600519", "000001", "000390", "833575"]
        assert [row["name"] for row in captured["symbols_info"]] == ["贵州茅台", "平安银行", "晨光", "北交样本"]

    def test_generate_ai_report_enriches_codes_from_selection_brief(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": ["000004"],
                    "selection_brief": {
                        "best_candidates": [
                            {
                                "code": "000004",
                                "name": "主线候选",
                                "tier": "高优先级研报候选",
                                "why": "趋势线 / 主线买点",
                                "track": "Trend",
                                "candidate_lane": "mainline",
                                "strategic_theme": "机器人",
                                "theme_score": 0.72,
                                "theme_source": "ths_hot_event",
                                "theme_event_id": "evt-robot",
                                "theme_event_title": "机器人主线回归",
                                "theme_event_reason": "灵巧手订单催化",
                                "priority_score": 11.0,
                                "shadow_score": 4.2,
                            }
                        ],
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(["000004"], tool_context=ctx)

        assert result["reviewed_codes"] == ["000004"]
        assert result["reviewed_symbols"][0]["tier"] == "高优先级研报候选"
        assert result["reviewed_symbols"][0]["candidate_lane"] == "mainline"
        assert result["reviewed_symbols"][0]["strategic_theme"] == "机器人"
        assert result["reviewed_symbols"][0]["theme_source"] == "ths_hot_event"
        assert result["reviewed_symbols"][0]["theme_event_reason"] == "灵巧手订单催化"
        assert result["reviewed_symbols"][0]["shadow_score"] == 4.2
        assert captured["symbols_info"][0]["why"] == "趋势线 / 主线买点"
        assert captured["symbols_info"][0]["strategic_theme"] == "机器人"
        assert captured["symbols_info"][0]["theme_event_id"] == "evt-robot"
        assert captured["symbols_info"][0]["shadow_score"] == 4.2

    def test_generate_ai_report_uses_screen_handoff_when_codes_omitted(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": [
                        {
                            "code": "000004",
                            "name": "主线候选",
                            "tag": "主线买点确认 | 威科夫候选",
                            "track": "Trend",
                            "candidate_lane": "mainline",
                            "priority_score": 11.0,
                        }
                    ],
                    "selection_brief": {
                        "tool_handoff": {
                            "tool": "generate_ai_report",
                            "args": {"stock_codes": ["000004"]},
                        }
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 自动续接研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["000004"]
        assert result["report_text"] == "# 自动续接研报"
        assert captured["symbols_info"][0]["candidate_lane"] == "mainline"
        assert captured["symbols_info"][0]["priority_score"] == 11.0
        assert ctx.state["last_ai_report"]["reviewed_codes"] == ["000004"]

    def test_generate_ai_report_blocks_top_level_quality_gate_handoff(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "job_kind": "funnel_screen",
                    "symbols_for_report": [],
                    "watch_candidates": [{"code": "000013", "name": "低质量候选"}],
                    "quality_gate": {"status": "blocked_by_quality_gate", "reason": reason, "blocked_count": 1},
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            report_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["status"] == "blocked_by_quality_gate"
        assert result["reason"] == reason
        assert result["error"].startswith("上一轮候选质量门槛未过")

    def test_generate_ai_report_does_not_enrich_from_policy_guard_candidates(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "scan_scope": {"source": "recommendation_event_eval"},
                    "selection_brief": {"status": "watch_only"},
                    "action_plan": {"ai_review_allowed": False, "reason": "只读推荐事件评估未通过排序接入门槛"},
                    "top_candidates": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "candidate_shadow_score": 92.0,
                            "action_status": "watch_only",
                        }
                    ],
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(report_tools, "code_to_name", lambda code: {"300750": "宁德时代"}[code])

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 显式研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(["300750"], tool_context=ctx)

        assert result["reviewed_codes"] == ["300750"]
        assert captured["symbols_info"][0] == {
            "code": "300750",
            "name": "宁德时代",
            "tag": "chat_request",
            "selection_source": "explicit_report_input",
        }
        assert "candidate_shadow_score" not in result["reviewed_symbols"][0]
        assert "action_status" not in result["reviewed_symbols"][0]

    def test_generate_ai_report_allows_top_level_quality_gate_with_passing_candidate(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "job_kind": "funnel_screen",
                    "symbols_for_report": [{"code": "000014", "name": "高质量候选"}],
                    "watch_candidates": [{"code": "000013", "name": "低质量候选"}],
                    "quality_gate": {
                        "status": "blocked_by_quality_gate",
                        "reason": "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00",
                        "blocked_count": 1,
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 自动续接研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["000014"]
        assert captured["symbols_info"][0]["name"] == "高质量候选"

    def test_generate_ai_report_uses_top_level_report_candidates_when_symbols_missing(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1, "watch_candidates": 1},
                    "symbols_for_report": [],
                    "report_candidates": [
                        {
                            "code": "000014",
                            "name": "高质量候选",
                            "candidate_shadow_grade": "S",
                            "action_status": "ready_for_ai_review",
                        }
                    ],
                    "selection_brief": {
                        "status": "ready_for_ai_review",
                        "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                    "top_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 顶层候选研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["000014"]
        assert result["reviewed_symbols"][0]["candidate_shadow_grade"] == "S"
        assert result["reviewed_symbols"][0]["action_status"] == "ready_for_ai_review"
        assert captured["symbols_info"][0]["code"] == "000014"

    def test_generate_ai_report_preserves_report_candidate_metadata_for_string_handoff(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1, "watch_candidates": 1},
                    "symbols_for_report": ["000014"],
                    "report_candidates": [
                        {
                            "code": "000014",
                            "name": "高质量候选",
                            "candidate_shadow_grade": "S",
                            "candidate_shadow_score": 92.0,
                            "action_status": "ready_for_ai_review",
                        }
                    ],
                    "selection_brief": {
                        "status": "ready_for_ai_review",
                        "best_candidates": [{"code": "000014", "name": "观察候选", "action_status": "watch_only"}],
                    },
                    "top_candidates": [{"code": "000014", "name": "备用观察", "action_status": "watch_only"}],
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 字符串候选研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["000014"]
        assert result["reviewed_symbols"][0]["name"] == "高质量候选"
        assert result["reviewed_symbols"][0]["candidate_shadow_grade"] == "S"
        assert result["reviewed_symbols"][0]["candidate_shadow_score"] == 92.0
        assert result["reviewed_symbols"][0]["action_status"] == "ready_for_ai_review"
        assert captured["symbols_info"][0]["action_status"] == "ready_for_ai_review"

    def test_generate_ai_report_blocks_auto_handoff_on_watch_only_screen(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 0, "watch_candidates": 1},
                    "symbols_for_report": [],
                    "watch_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    "selection_brief": {
                        "status": "watch_only",
                        "headline": "本轮只有观察候选: 000013 观察候选",
                        "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                    "action_plan": {
                        "ai_review_allowed": False,
                        "watch_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            report_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["status"] == "blocked_by_watch_only"
        assert result["reason"] == "本轮只有观察候选: 000013 观察候选"
        assert result["error"].startswith("上一轮候选仍是只读观察")
        assert "last_ai_report" not in ctx.state

    def test_generate_ai_report_reuses_recommendation_eval_handoff(self, monkeypatch):
        from agents import recommendation_tools, report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext({})
        recommendation_tools.remember_recommendation_event_eval(
            ctx,
            {
                "ok": True,
                "job_kind": "recommendation_event_eval",
                "result_summary": "推荐事件评估: ready=12/20, hit=60%, ranking_decision=candidate",
                "metadata": {"market": "cn"},
                "summary": {
                    "all": {"rows_ready": 12, "rows_total": 20, "hit_rate_pct": 60.0},
                    "ranking_decision": {"status": "candidate"},
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
                            "funnel_score": 0.0,
                            "candidate_shadow_score": 92.0,
                            "candidate_shadow_grade": "S",
                            "entry_quality_score": 84.0,
                            "entry_quality_grade": "A",
                            "entry_quality_risk_flags": ["短线涨幅偏快"],
                            "label_ready": False,
                            "label_status": "pending",
                            "action_status": "ready_for_ai_review",
                        }
                    ],
                },
                "daily": [],
            },
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 推荐评估候选研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["300750"]
        assert result["reviewed_symbols"][0]["selection_source"] == "recommendation_event_eval"
        assert result["reviewed_symbols"][0]["funnel_score"] == 0.0
        assert result["reviewed_symbols"][0]["candidate_shadow_score"] == 92.0
        assert result["reviewed_symbols"][0]["candidate_shadow_grade"] == "S"
        assert result["reviewed_symbols"][0]["entry_quality_score"] == 84.0
        assert result["reviewed_symbols"][0]["candidate_quality_score"] == 92.0
        assert result["reviewed_symbols"][0]["risk_adjusted_quality_score"] == 87.0
        assert result["reviewed_symbols"][0]["entry_risk_penalty"] == 5.0
        assert result["reviewed_symbols"][0]["label_ready"] is False
        assert result["reviewed_symbols"][0]["label_status"] == "pending"
        assert result["reviewed_symbols"][0]["trade_readiness"] == "research_only"
        assert result["reviewed_symbols"][0]["new_buy_allowed"] is False
        assert "短线涨幅偏快" in result["reviewed_symbols"][0]["risk_factors"]
        assert captured["symbols_info"][0]["risk_adjusted_quality_score"] == 87.0
        assert captured["symbols_info"][0]["entry_quality_grade"] == "A"
        assert captured["symbols_info"][0]["label_ready"] is False
        assert captured["symbols_info"][0]["new_buy_allowed"] is False
        assert ctx.state["last_ai_report"]["report_text"] == "# 推荐评估候选研报"

    def test_generate_ai_report_blocks_auto_handoff_on_degraded_screen_data(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": [{"code": "000004", "name": "主线候选"}],
                    "selection_brief": {"status": "blocked_by_data_quality", "best_codes": ["000004"]},
                    "action_plan": {
                        "data_quality_gate": {
                            "status": "degraded",
                            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
                        },
                        "review_targets": {
                            "codes": ["000004"],
                            "status": "blocked_by_data_quality",
                            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
                        },
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            report_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result == {
            "error": "上一轮筛选数据质量不足，不能自动续接 AI 研报: 不要直接据此选股，先重跑或缩小扫描范围",
            "status": "blocked_by_data_quality",
            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
        }
        assert "last_ai_report" not in ctx.state

    def test_generate_ai_report_explicit_codes_ignore_degraded_screen_metadata(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "symbols_for_report": [
                        {
                            "code": "000004",
                            "name": "坏数据候选",
                            "priority_score": 12.5,
                            "why": "上一轮坏数据候选原因",
                        }
                    ],
                    "action_plan": {
                        "data_quality_gate": {
                            "status": "degraded",
                            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
                        }
                    },
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))
        monkeypatch.setattr(report_tools, "code_to_name", lambda code: {"000004": "主线候选"}[code])

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 显式代码研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report("000004", tool_context=ctx)

        assert result["reviewed_codes"] == ["000004"]
        assert result["reviewed_symbols"][0] == {
            "code": "000004",
            "name": "主线候选",
            "tag": "chat_request",
            "selection_source": "explicit_report_input",
        }
        assert captured["symbols_info"][0] == {
            "code": "000004",
            "name": "主线候选",
            "tag": "chat_request",
            "selection_source": "explicit_report_input",
        }

    def test_generate_ai_report_uses_best_candidates_when_handoff_missing(self, monkeypatch):
        from agents import report_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "selection_brief": {
                        "best_candidates": [
                            {
                                "code": "000007",
                                "name": "观察候选",
                                "tier": "强观察候选",
                                "why": "趋势线 / 启动平台",
                                "track": "Trend",
                            }
                        ]
                    }
                }
            }
        )
        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(report_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", ""))

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 观察候选研报"

        monkeypatch.setattr(report_tools, "run_ai_report", fake_run_ai_report)

        result = report_tools.generate_ai_report(tool_context=ctx)

        assert result["reviewed_codes"] == ["000007"]
        assert result["reviewed_symbols"][0]["tier"] == "强观察候选"
        assert captured["symbols_info"][0]["why"] == "趋势线 / 启动平台"


class TestStrategyDecisionTool:
    def test_generate_strategy_decision_reuses_last_report_without_rescreening(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_ai_report": {
                    "report_text": "# 上一跳研报",
                    "reviewed_codes": ["300750"],
                    "reviewed_symbols": [
                        {
                            "code": "300750",
                            "name": "宁德时代",
                            "track": "Trend",
                            "label_ready": False,
                            "label_status": "pending",
                            "action_status": "ready_for_ai_review",
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                            "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                        }
                    ],
                },
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "decision_brief": {"next_action": "允许候选进入AI复核"},
                    "symbols_for_report": [{"code": "300750", "name": "宁德时代", "track": "Trend"}],
                    "strategy_policy": {
                        "dynamic_mode": "shadow",
                        "execution_policy": "shadow",
                        "policy_weight_active_scope": "尾盘+漏斗shadow",
                        "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级",
                        "attribution_signal_weights": {"lps": 0.5},
                        "next_action": "manual_review_dynamic_on",
                    },
                },
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools, "run_ai_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["ok"] is True
        assert result["status"] == "skipped_notify_unconfigured"
        assert result["report_source"] == "last_ai_report"
        assert result["reviewed_codes"] == ["300750"]
        assert result["missing_credentials"] == ["TG_BOT_TOKEN", "TG_CHAT_ID"]
        assert result["screen_summary"] == {"report_candidates": 1}
        assert result["strategy_policy"]["policy_weight_active_scope"] == "尾盘+漏斗shadow"
        assert result["candidate_guard_summary"] == {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "reason": "候选标签未成熟，禁止直接买入",
                    "action_status": "ready_for_ai_review",
                    "action_label": "可进入AI复核",
                    "action_level": "ai_review",
                    "direct_buy_allowed": False,
                    "label_ready": False,
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                    "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                }
            ],
        }
        assert result["report_preview"].startswith("## 策略治理上下文")
        assert "candidate_lane=trend_pullback" in result["report_preview"]
        assert "# 上一跳研报" in result["report_preview"]
        assert ctx.state["last_strategy_decision"]["reviewed_codes"] == ["300750"]
        assert ctx.state["last_strategy_decision"]["strategy_policy"]["execution_policy"] == "shadow"

    def test_generate_strategy_decision_merges_stock_diagnosis_handoff(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_ai_report": {
                    "report_text": "# 上一跳研报",
                    "reviewed_symbols": [
                        {
                            "code": "000004",
                            "name": "主线候选",
                            "label_ready": False,
                            "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                        }
                    ],
                },
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "symbols_for_report": [
                        {
                            "code": "000004",
                            "name": "主线候选",
                            "track": "Trend",
                            "action_status": "ready_for_ai_review",
                        }
                    ],
                },
                "last_stock_diagnosis": {
                    "latest": {
                        "code": "000004",
                        "name": "主线候选",
                        "health": "健康",
                        "stage": "Accum_C",
                        "candidate_score": 83.04,
                        "risk_factors": ["短线涨幅偏快"],
                        "next_step": "等待放量突破触发位",
                    }
                },
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools, "run_ai_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        reviewed = result["reviewed_symbols"][0]
        assert result["report_source"] == "last_ai_report"
        assert reviewed["code"] == "000004"
        assert reviewed["track"] == "Trend"
        assert reviewed["health"] == "健康"
        assert reviewed["stage"] == "Accum_C"
        assert reviewed["candidate_score"] == 83.04
        assert reviewed["risk_factors"] == ["短线涨幅偏快", "最新候选的未来窗口标签尚未成熟"]
        assert reviewed["label_ready"] is False
        assert result["candidate_guard_summary"]["candidates"][0]["risk_factors"] == [
            "短线涨幅偏快",
            "最新候选的未来窗口标签尚未成熟",
        ]
        assert ctx.state["last_strategy_decision"]["reviewed_symbols"][0]["health"] == "健康"

    def test_generate_strategy_decision_preserves_explicit_reviewed_code_order(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_stock_diagnosis": {
                    "diagnosed_symbols": [
                        {"code": "001314", "name": "亿道信息", "candidate_score": 100.0},
                        {"code": "002293", "name": "罗莱生活", "candidate_score": 97.62},
                    ]
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools, "run_ai_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
        )

        result = strategy_tools.generate_strategy_decision(
            report_text="# 显式研报",
            reviewed_codes=["002293", "001314"],
            tool_context=ctx,
        )

        assert result["reviewed_codes"] == ["002293", "001314"]
        assert [row["candidate_score"] for row in result["reviewed_symbols"]] == [97.62, 100.0]

    def test_generate_strategy_decision_passes_provided_report_to_step4(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "strategy_policy": {
                        "dynamic_mode": "shadow",
                        "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级",
                        "signal_weights": {"trend_pullback": 0.75},
                    }
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(
            strategy_tools,
            "get_credential",
            lambda _tool_context, key, _env: "token" if key == "tg_bot_token" else "chat",
        )
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools, "run_ai_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
        )

        def fake_run_strategy_step4(tool_context, report_text, candidate_meta, *args):
            captured["tool_context"] = tool_context
            captured["report_text"] = report_text
            captured["candidate_meta"] = candidate_meta
            captured["args"] = args
            return True, "ok"

        monkeypatch.setattr(strategy_tools, "_run_strategy_step4", fake_run_strategy_step4)

        result = strategy_tools.generate_strategy_decision(
            report_text="# 显式研报",
            reviewed_symbols=[{"code": "000001", "name": "平安银行", "stage": "Accum_C"}],
            tool_context=ctx,
        )

        assert result["ok"] is True
        assert result["report_source"] == "provided"
        assert result["next_action"] == "攻防决策已完成，查看 Telegram 或订单记录确认工单"
        assert captured["report_text"].startswith("## 策略治理上下文")
        assert "candidate_lane=trend_pullback" in captured["report_text"]
        assert "# 显式研报" in captured["report_text"]
        assert captured["candidate_meta"] == [{"code": "000001", "name": "平安银行", "stage": "Accum_C"}]
        assert result["strategy_policy"]["signal_weights"] == {"trend_pullback": 0.75}
        assert ctx.state["last_strategy_decision"]["reviewed_codes"] == ["000001"]

    def test_generate_strategy_decision_uses_best_candidates_when_report_list_empty(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 0},
                    "selection_brief": {
                        "status": "watch_only",
                        "best_candidates": [
                            {
                                "code": "000007",
                                "name": "启动平台",
                                "tier": "强观察候选",
                                "why": "趋势线 / 主升阶段 / 启动平台",
                                "candidate_lane": "launchpad",
                                "entry_type": "launchpad",
                                "priority_score": 8.5,
                                "quality_factors": ["强观察候选", "启动平台"],
                                "risk_factors": ["未进入本轮研报候选", "观察池，不进入本轮AI复核"],
                                "action_status": "watch_only",
                            }
                        ],
                    },
                    "top_candidates": [{"code": "000008", "name": "备用观察"}],
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 观察候选研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["report_source"] == "generated_from_candidates"
        assert result["candidate_count"] == 1
        assert result["reviewed_codes"] == ["000007"]
        assert result["reviewed_symbols"][0]["candidate_lane"] == "launchpad"
        assert result["reviewed_symbols"][0]["entry_type"] == "launchpad"
        assert result["reviewed_symbols"][0]["risk_factors"] == ["未进入本轮研报候选", "观察池，不进入本轮AI复核"]
        assert result["reviewed_symbols"][0]["action_status"] == "watch_only"
        assert result["candidate_guard_summary"] == {
            "direct_buy_blocked_count": 1,
            "message": "以下候选仅可复核或观察，禁止直接买入",
            "candidates": [
                {
                    "code": "000007",
                    "name": "启动平台",
                    "reason": "候选状态 watch_only 不允许直接买入",
                    "action_status": "watch_only",
                    "action_label": "观察池",
                    "action_level": "watch",
                    "direct_buy_allowed": False,
                    "risk_factors": ["未进入本轮研报候选", "观察池，不进入本轮AI复核"],
                }
            ],
        }
        assert result["report_preview"] == "# 观察候选研报"
        assert captured["symbols_info"][0]["why"] == "趋势线 / 主升阶段 / 启动平台"
        assert captured["symbols_info"][0]["quality_factors"] == ["强观察候选", "启动平台"]

    def test_generate_strategy_decision_uses_top_level_report_candidates_when_symbols_missing(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1, "watch_candidates": 1},
                    "symbols_for_report": [],
                    "report_candidates": [
                        {
                            "code": "000014",
                            "name": "高质量候选",
                            "candidate_shadow_grade": "S",
                            "candidate_shadow_score": 92.0,
                            "action_status": "ready_for_ai_review",
                        }
                    ],
                    "selection_brief": {
                        "status": "ready_for_ai_review",
                        "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                    "top_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 顶层候选研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["report_source"] == "generated_from_candidates"
        assert result["reviewed_codes"] == ["000014"]
        assert result["reviewed_symbols"][0]["candidate_shadow_grade"] == "S"
        assert result["reviewed_symbols"][0]["candidate_shadow_score"] == 92.0
        assert result["reviewed_symbols"][0]["action_status"] == "ready_for_ai_review"
        assert captured["symbols_info"][0]["code"] == "000014"
        assert result["report_preview"] == "# 顶层候选研报"

    def test_generate_strategy_decision_enriches_string_symbols_from_report_candidates(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "symbols_for_report": ["000019"],
                    "report_candidates": [
                        {"code": "000014", "name": "一号候选"},
                        {"code": "000015", "name": "二号候选"},
                        {"code": "000016", "name": "三号候选"},
                        {"code": "000017", "name": "四号候选"},
                        {"code": "000018", "name": "五号候选"},
                        {
                            "code": "000019",
                            "name": "高质量候选",
                            "track": "Trend",
                            "candidate_shadow_grade": "S",
                            "candidate_shadow_score": 92.0,
                            "action_status": "ready_for_ai_review",
                            "strategic_theme": "机器人",
                            "theme_score": 0.72,
                            "theme_source": "ths_hot_event",
                            "theme_event_id": "evt-robot",
                            "theme_event_reason": "灵巧手订单催化",
                            "entry_zone": [12.3, 12.8],
                            "stop_loss": 11.9,
                            "max_entry_price": 13.0,
                            "position_size_pct": 0.25,
                            "tape_condition": "放量站回5日线",
                            "invalidate_condition": "跌破11.9取消交易",
                        },
                    ],
                    "selection_brief": {
                        "best_candidates": [{"code": "000019", "name": "观察候选", "action_status": "watch_only"}]
                    },
                    "top_candidates": [{"code": "000019", "name": "备用观察", "action_status": "watch_only"}],
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 字符串候选研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["reviewed_codes"] == ["000019"]
        assert result["reviewed_symbols"][0]["track"] == "Trend"
        assert result["reviewed_symbols"][0]["candidate_shadow_grade"] == "S"
        assert result["reviewed_symbols"][0]["candidate_shadow_score"] == 92.0
        assert result["reviewed_symbols"][0]["action_status"] == "ready_for_ai_review"
        assert result["reviewed_symbols"][0]["strategic_theme"] == "机器人"
        assert result["reviewed_symbols"][0]["theme_source"] == "ths_hot_event"
        assert result["reviewed_symbols"][0]["theme_event_reason"] == "灵巧手订单催化"
        assert result["reviewed_symbols"][0]["entry_zone"] == [12.3, 12.8]
        assert result["reviewed_symbols"][0]["stop_loss"] == 11.9
        assert result["reviewed_symbols"][0]["max_entry_price"] == 13.0
        assert result["reviewed_symbols"][0]["position_size_pct"] == 0.25
        assert result["reviewed_symbols"][0]["tape_condition"] == "放量站回5日线"
        assert result["reviewed_symbols"][0]["invalidate_condition"] == "跌破11.9取消交易"
        assert captured["symbols_info"][0]["track"] == "Trend"
        assert captured["symbols_info"][0]["strategic_theme"] == "机器人"
        assert captured["symbols_info"][0]["theme_event_id"] == "evt-robot"
        assert captured["symbols_info"][0]["entry_zone"] == [12.3, 12.8]

    def test_generate_strategy_decision_blocks_auto_report_on_degraded_screen_data(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "decision_brief": {"next_action": "不要直接据此选股，先重跑或缩小扫描范围"},
                    "symbols_for_report": [{"code": "000004", "name": "主线候选"}],
                    "selection_brief": {
                        "status": "blocked_by_data_quality",
                        "best_candidates": [{"code": "000004", "name": "主线候选"}],
                    },
                    "action_plan": {
                        "data_quality_gate": {
                            "status": "degraded",
                            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
                        }
                    },
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["ok"] is False
        assert result["status"] == "blocked_by_data_quality"
        assert result["report_source"] == "blocked_by_screen_data_quality"
        assert result["candidate_count"] == 0
        assert result["reviewed_codes"] == []
        assert result["screen_summary"] == {"report_candidates": 1}
        assert result["next_action"] == "先重跑或缩小扫描范围，确认行情数据质量后再生成策略决策"
        assert ctx.state["last_strategy_decision"]["status"] == "blocked_by_data_quality"

    def test_generate_strategy_decision_blocks_auto_report_on_quality_gate(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        reason = "000013 低质量候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 0},
                    "symbols_for_report": [],
                    "decision_brief": {"next_action": "观察候选"},
                    "selection_brief": {
                        "status": "watch_only",
                        "best_candidates": [{"code": "000013", "name": "低质量候选", "action_status": "watch_only"}],
                    },
                    "action_plan": {
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
                    },
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["ok"] is False
        assert result["status"] == "blocked_by_quality_gate"
        assert result["report_source"] == "blocked_by_screen_quality_gate"
        assert result["reason"] == reason
        assert result["candidate_count"] == 0
        assert result["next_action"] == "先保留观察候选，等待风险调整质量分达标后再生成策略决策"
        assert ctx.state["last_strategy_decision"]["status"] == "blocked_by_quality_gate"

    def test_generate_strategy_decision_labels_recommendation_policy_guard(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "scan_scope": {"source": "recommendation_event_eval"},
                    "summary": {"report_candidates": 1},
                    "selection_brief": {
                        "status": "watch_only",
                        "headline": "推荐事件评估仍是观察候选",
                        "best_candidates": [{"code": "300750", "name": "宁德时代", "action_status": "watch_only"}],
                    },
                    "action_plan": {
                        "ai_review_allowed": False,
                        "reason": "推荐事件评估仍是观察候选，需等待排序门槛通过",
                    },
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["ok"] is False
        assert result["status"] == "blocked_by_policy_guard"
        assert result["report_source"] == "blocked_by_screen_policy_guard"
        assert result["next_action"] == "先观察候选，等待排序或策略门槛通过后再生成策略决策"

    def test_generate_strategy_decision_blocks_auto_report_on_watch_only_screen(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 0, "watch_candidates": 1},
                    "selection_brief": {
                        "status": "watch_only",
                        "headline": "本轮只有观察候选: 000013 观察候选",
                        "best_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                    "action_plan": {
                        "ai_review_allowed": False,
                        "watch_candidates": [{"code": "000013", "name": "观察候选", "action_status": "watch_only"}],
                    },
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["ok"] is False
        assert result["status"] == "blocked_by_watch_only"
        assert result["report_source"] == "blocked_by_screen_watch_only"
        assert result["candidate_count"] == 0
        assert result["next_action"] == "先观察候选，等待形成研报候选后再生成策略决策"
        assert ctx.state["last_strategy_decision"]["status"] == "blocked_by_watch_only"

    def test_generate_strategy_decision_explicit_report_ignores_degraded_screen_candidates(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "symbols_for_report": [{"code": "000004", "name": "坏数据候选", "priority_score": 12.5}],
                    "selection_brief": {
                        "best_candidates": [
                            {
                                "code": "000004",
                                "name": "坏数据候选",
                                "why": "上一轮坏数据候选原因",
                            }
                        ]
                    },
                    "action_plan": {
                        "data_quality_gate": {
                            "status": "degraded",
                            "reason": "不要直接据此选股，先重跑或缩小扫描范围",
                        }
                    },
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run report")),
        )

        result = strategy_tools.generate_strategy_decision(report_text="# 外部研报", tool_context=ctx)

        assert result["ok"] is True
        assert result["report_source"] == "provided"
        assert result["candidate_count"] == 0
        assert result["reviewed_codes"] == []
        assert result["reviewed_symbols"] == []
        assert result["report_preview"] == "# 外部研报"

    def test_generate_strategy_decision_enriches_string_report_codes(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext(
            {
                "last_screen_result": {
                    "summary": {"report_candidates": 1},
                    "symbols_for_report": ["000004"],
                    "selection_brief": {
                        "best_candidates": [
                            {
                                "code": "000004",
                                "name": "主线候选",
                                "track": "Trend",
                                "candidate_lane": "mainline",
                                "priority_score": 11.0,
                                "why": "趋势线 / 主线买点",
                            }
                        ],
                    },
                    "top_candidates": [{"code": "000005", "name": "备用观察", "track": "Accum"}],
                }
            }
        )
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 主线候选研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(tool_context=ctx)

        assert result["reviewed_codes"] == ["000004"]
        assert result["candidate_count"] == 1
        assert result["reviewed_symbols"][0]["track"] == "Trend"
        assert result["reviewed_symbols"][0]["candidate_lane"] == "mainline"
        assert result["reviewed_symbols"][0]["priority_score"] == 11.0
        assert captured["symbols_info"][0]["why"] == "趋势线 / 主线买点"

    def test_generate_strategy_decision_accepts_comma_codes_without_rescreening(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        ctx = ToolContext()
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_symbols_info_from_codes(codes, tool_context):
            captured["codes_arg"] = codes
            return [{"code": "000004", "name": "主线候选"}, {"code": "000005", "name": "二号候选"}]

        monkeypatch.setattr(strategy_tools, "symbols_info_from_codes", fake_symbols_info_from_codes)

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 代码研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(reviewed_codes="000004, 000005", tool_context=ctx)

        assert result["report_source"] == "generated_from_candidates"
        assert result["reviewed_codes"] == ["000004", "000005"]
        assert captured["codes_arg"] == ["000004, 000005"]
        assert [row["code"] for row in captured["symbols_info"]] == ["000004", "000005"]
        assert result["report_preview"] == "# 代码研报"

    def test_generate_strategy_decision_normalizes_exchange_wrapped_candidate_codes(self, monkeypatch):
        from agents import report_tools, strategy_tools
        from agents.tool_context import ToolContext

        captured = {}
        names = {"600519": "贵州茅台", "833575": "北交样本"}
        ctx = ToolContext()
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(report_tools, "code_to_name", lambda code: names[code])

        def fake_run_ai_report(symbols_info, **_kwargs):
            captured["symbols_info"] = symbols_info
            return True, "ok", "# 归一研报"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(
            reviewed_symbols={"code": "000390.SZ", "name": "晨光", "track": "Trend"},
            reviewed_codes="SH600519、833575.BJ",
            tool_context=ctx,
        )

        assert result["reviewed_codes"] == ["000390", "600519", "833575"]
        assert result["reviewed_symbols"][0]["track"] == "Trend"
        assert [row["code"] for row in captured["symbols_info"]] == ["000390", "600519", "833575"]
        assert result["report_preview"] == "# 归一研报"

    def test_generate_strategy_decision_corrects_generated_report_names(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext()
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})

        def fake_run_ai_report(symbols_info, **_kwargs):
            assert symbols_info[0]["name"] == "快意电梯"
            return True, "ok", "- **002774（康尼机电）**\n  触发位 15.09。"

        monkeypatch.setattr(strategy_tools, "run_ai_report", fake_run_ai_report)

        result = strategy_tools.generate_strategy_decision(
            reviewed_symbols={"code": "002774", "name": "快意电梯", "track": "Trend"},
            tool_context=ctx,
        )

        assert "002774（快意电梯）" in result["report_preview"]
        assert "康尼机电" not in result["report_preview"]

    def test_generate_strategy_decision_accepts_single_reviewed_symbol_object(self, monkeypatch):
        from agents import strategy_tools
        from agents.tool_context import ToolContext

        ctx = ToolContext()
        monkeypatch.setattr(strategy_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setattr(
            strategy_tools, "resolve_llm_config", lambda tool_context: ("openai", "key", "gpt-test", "")
        )
        monkeypatch.setattr(strategy_tools, "get_credential", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(strategy_tools, "screen_stocks", lambda **_kwargs: {"error": "should not screen"})
        monkeypatch.setattr(
            strategy_tools, "run_ai_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)
        )

        result = strategy_tools.generate_strategy_decision(
            report_text="# 显式研报",
            reviewed_symbols={
                "code": "000004",
                "name": "主线候选",
                "track": "Trend",
                "candidate_lane": "mainline",
            },
            tool_context=ctx,
        )

        assert result["reviewed_codes"] == ["000004"]
        assert result["reviewed_symbols"][0]["track"] == "Trend"
        assert result["reviewed_symbols"][0]["candidate_lane"] == "mainline"


# ── core.candidate_ranker ──


class TestCandidateRanker:
    def test_calc_close_return_pct_normal(self):
        from core.candidate_ranker import calc_close_return_pct

        s = pd.Series([100.0, 105.0, 110.0])
        result = calc_close_return_pct(s, lookback=1)
        assert result is not None
        assert abs(result - 4.76) < 0.1  # (110-105)/105 * 100

    def test_calc_close_return_pct_short_series(self):
        from core.candidate_ranker import calc_close_return_pct

        s = pd.Series([100.0])
        assert calc_close_return_pct(s, lookback=5) is None

    def test_calc_close_return_pct_zero_start(self):
        from core.candidate_ranker import calc_close_return_pct

        s = pd.Series([0.0, 10.0, 20.0])
        # lookback=1 → start=10, end=20 → 100%
        result = calc_close_return_pct(s, lookback=1)
        assert result is not None
        assert abs(result - 100.0) < 0.1

    def test_trigger_labels_is_dict(self):
        from core.candidate_ranker import TRIGGER_LABELS

        assert isinstance(TRIGGER_LABELS, dict)
        assert "sos" in TRIGGER_LABELS
        assert "spring" in TRIGGER_LABELS
        assert len(TRIGGER_LABELS) == 10

    def test_rank_l3_candidates_rewards_trigger_and_hot_sector(self):
        from core.candidate_ranker import rank_l3_candidates

        dates = pd.date_range("2026-01-01", periods=30, freq="D").strftime("%Y-%m-%d")
        df_map = {
            "000001": pd.DataFrame(
                {"date": dates, "close": [10.0 + i * 0.10 for i in range(30)], "volume": [1000] * 30}
            ),
            "000002": pd.DataFrame(
                {"date": dates, "close": [10.0 + i * 0.08 for i in range(30)], "volume": [1000] * 30}
            ),
            "000003": pd.DataFrame(
                {"date": dates, "close": [10.0 + i * 0.30 for i in range(30)], "volume": [1000] * 30}
            ),
        }

        ranked, score_map = rank_l3_candidates(
            ["000001", "000002", "000003"],
            df_map,
            {"000001": "热点行业", "000002": "冷门行业", "000003": "冷门行业"},
            {"sos": [("000001", 8.0)]},
            ["热点行业"],
        )

        assert ranked[0] == "000001"
        assert score_map["000001"] > score_map["000002"]

    def test_rank_l3_candidates_breaks_watch_score_ties_with_quality_inputs(self):
        from core.candidate_ranker import rank_l3_candidates

        dates = pd.date_range("2026-01-01", periods=30, freq="D").strftime("%Y-%m-%d")
        flat = pd.DataFrame({"date": dates, "close": [10.0] * 30, "volume": [1000] * 30})

        ranked, score_map = rank_l3_candidates(
            ["000003", "000002", "000001"],
            {"000001": flat.copy(), "000002": flat.copy(), "000003": flat.copy()},
            {"000001": "行业A", "000002": "行业A", "000003": "行业A"},
            {"sos": [("000002", 8.0), ("000003", 5.0), ("000001", 8.0)]},
            [],
        )

        assert score_map["000001"] == score_map["000002"]
        assert ranked == ["000001", "000002", "000003"]

    def test_rank_l3_candidates_penalizes_overextended_momentum(self):
        from core.candidate_ranker import rank_l3_candidates

        dates = pd.date_range("2026-01-01", periods=30, freq="D").strftime("%Y-%m-%d")
        healthy = [10.0] * 9 + [10.0 + i * 0.10 for i in range(21)]
        overheated = [10.0] * 9 + [10.0 + i * 0.55 for i in range(21)]
        df_map = {
            "000001": pd.DataFrame({"date": dates, "close": healthy, "volume": [1000] * 30}),
            "000002": pd.DataFrame({"date": dates, "close": overheated, "volume": [1000] * 30}),
        }

        ranked, score_map = rank_l3_candidates(
            ["000002", "000001"],
            df_map,
            {"000001": "行业A", "000002": "行业A"},
            {"sos": [("000001", 8.0), ("000002", 8.0)]},
            [],
        )

        assert ranked[0] == "000001"
        assert score_map["000001"] > score_map["000002"]

    def test_rank_l3_candidates_treats_invalid_trigger_scores_as_zero(self):
        from core.candidate_ranker import rank_l3_candidates

        dates = pd.date_range("2026-01-01", periods=30, freq="D").strftime("%Y-%m-%d")
        flat = pd.DataFrame({"date": dates, "close": [10.0] * 30, "volume": [1000] * 30})

        ranked, score_map = rank_l3_candidates(
            ["GOOD", "BAD", "INF", "NAN"],
            {code: flat.copy() for code in ("GOOD", "BAD", "INF", "NAN")},
            {code: "行业A" for code in ("GOOD", "BAD", "INF", "NAN")},
            {"sos": [("GOOD", 8.0), ("BAD", "bad"), ("INF", float("inf")), ("NAN", float("nan"))]},
            [],
        )

        assert ranked[0] == "GOOD"
        assert score_map["GOOD"] > score_map["BAD"]
        assert score_map["BAD"] == score_map["INF"] == score_map["NAN"]

    def test_extension_penalty_series_handles_bad_return_values(self):
        from core.candidate_ranker import _extension_penalty_series

        penalty = _extension_penalty_series(pd.DataFrame({"ret20": [None, "bad", 100.0], "ret5": [None, "bad", 40.0]}))

        assert penalty.iloc[0] == 0.0
        assert penalty.iloc[1] == 0.0
        assert abs(penalty.iloc[2] - 0.4) < 1e-9


# ── tools/market_regime ──


class TestMarketRegime:
    def test_imports_callable(self):
        from tools.market_regime import (
            analyze_benchmark_and_tune_cfg,
            calc_amount_distribution_health,
            calc_market_breadth,
            calc_market_money_flow,
        )

        assert callable(analyze_benchmark_and_tune_cfg)
        assert callable(calc_amount_distribution_health)
        assert callable(calc_market_breadth)
        assert callable(calc_market_money_flow)

    def test_calc_market_breadth_empty(self):
        from tools.market_regime import calc_market_breadth

        result = calc_market_breadth({})
        assert result["ratio_pct"] is None
        assert result["sample_size"] == 0

    def test_calc_market_money_flow_detects_entry(self):
        from tools.market_regime import calc_market_money_flow

        df_map = {
            "000001": _money_flow_df(10.0, 11.0, 180_000_000),
            "000002": _money_flow_df(20.0, 21.0, 160_000_000),
            "000003": _money_flow_df(30.0, 29.7, 60_000_000),
        }
        result = calc_market_money_flow(df_map, {"delta_pct": 5.0})
        assert result["state"] == "主力进场"
        assert result["trend"] == "entry"
        assert result["amount_ratio_1_20"] > 1.1

    def test_calc_market_money_flow_detects_retreat(self):
        from tools.market_regime import calc_market_money_flow

        df_map = {
            "000001": _money_flow_df(10.0, 9.5, 180_000_000),
            "000002": _money_flow_df(20.0, 19.0, 160_000_000),
            "000003": _money_flow_df(30.0, 30.3, 50_000_000),
        }
        result = calc_market_money_flow(df_map, {"delta_pct": -6.0})
        assert result["state"] == "主力撤退"
        assert result["trend"] == "retreat"
        assert result["down_amount_yi"] > result["up_amount_yi"]

    def test_calc_amount_distribution_health_detects_thin_market(self):
        from tools.market_regime import calc_amount_distribution_health

        dates = pd.date_range("2026-01-01", periods=20, freq="D").strftime("%Y-%m-%d")
        df_map = {f"000{i:03d}": pd.DataFrame({"date": dates, "amount": [8_000_000.0] * 20}) for i in range(9)}
        df_map["000999"] = pd.DataFrame({"date": dates, "amount": [1_000_000_000.0] * 20})

        result = calc_amount_distribution_health(df_map, min_avg_amount_wan=5000.0)

        assert result["state"] == "thin"
        assert result["skewness"] > 2.0
        assert result["pass_ratio_pct"] < 35.0

    def test_market_regime_config_from_env_includes_pv_provider(self, monkeypatch):
        from workflows.market_regime_config import market_regime_config_from_env

        monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "efficiency")

        result = market_regime_config_from_env()

        assert result.pv_llm_provider == "efficiency"

    def test_holiday_grace_extends_when_money_flow_is_not_retreat(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")
        closes = list(pd.Series(range(220), dtype=float).map(lambda x: 100.0 + x * 0.2))
        bench = _benchmark_df(closes)
        prev_date = pd.to_datetime(bench.loc[len(bench) - 2, "date"])
        bench.loc[len(bench) - 1, "date"] = (prev_date + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        cfg = FunnelConfig()

        result = market_regime.analyze_benchmark_and_tune_cfg(
            bench,
            None,
            cfg,
            breadth={"ratio_pct": 70.0, "delta_pct": 5.0, "sample_size": 100},
            money_flow={"trend": "entry", "score": 25.0},
        )

        assert cfg.exit_holiday_grace_days == 2
        assert result["holiday_grace_dynamic"]["extended"] is True

    def test_market_pv_policy_shadow_structures_defensive_outlook(self):
        from core.wyckoff_engine import FunnelConfig
        from tools.market_regime import derive_market_pv_policy_shadow

        cfg = FunnelConfig()
        result = derive_market_pv_policy_shadow(
            outlook="次日推演：若放量跌破MA50，需转入防守；若缩量反弹，回避追高。",
            regime="RISK_ON",
            price_zone="多头上方",
            volume_state="放量",
            money_flow={"trend": "neutral"},
            cfg=cfg,
        )

        assert result["risk_bias"] == "defensive"
        assert result["conditions"][0]["if"] == "放量跌破MA50"
        assert result["funnel_config_overrides"]["rps_fast_min"] >= 80.0

    def test_breadth_risk_on_without_bull_structure_is_bear_rebound(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")
        closes = list(pd.Series(range(220), dtype=float).map(lambda x: 100.0 - x * 0.12))
        closes[-3:] = [75.0, 75.1, 75.2]

        cfg = FunnelConfig()
        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_df(closes),
            None,
            cfg,
            breadth={"ratio_pct": 70.0, "delta_pct": 5.0, "sample_size": 100},
        )

        assert result["regime"] == "BEAR_REBOUND"
        assert result["bear_rebound_triggered"] is True
        assert cfg.rps_fast_min >= 80.0

    def test_breadth_risk_on_with_bull_structure_stays_risk_on(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")
        closes = list(pd.Series(range(220), dtype=float).map(lambda x: 100.0 + x * 0.2))

        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_df(closes),
            None,
            FunnelConfig(),
            breadth={"ratio_pct": 70.0, "delta_pct": 5.0, "sample_size": 100},
        )

        assert result["regime"] == "RISK_ON"
        assert result["bear_rebound_triggered"] is False

    def test_single_index_drop_needs_confirmation_before_crash(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")

        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_with_last_drop(-1.5),
            None,
            FunnelConfig(),
            breadth={"ratio_pct": 60.0, "delta_pct": -2.0, "sample_size": 100},
            money_flow={"trend": "neutral", "score": 0.0},
        )

        assert result["regime"] == "RISK_OFF"
        assert result["panic_reasons"] == []

    def test_two_index_drop_without_breadth_or_money_confirmation_stays_risk_off(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")

        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_with_last_drop(-1.5),
            _benchmark_with_last_drop(-3.0),
            FunnelConfig(),
            breadth={"ratio_pct": 60.0, "delta_pct": -2.0, "sample_size": 100},
        )

        assert result["regime"] == "RISK_OFF"
        assert result["panic_reasons"] == []

    def test_index_drop_with_breadth_confirmation_confirms_crash(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")

        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_with_last_drop(-1.5),
            _benchmark_with_last_drop(-3.0),
            FunnelConfig(),
            breadth={"ratio_pct": 12.0, "delta_pct": -25.0, "sample_size": 100},
        )

        assert result["regime"] == "CRASH"
        assert any("main_day_drop" in item for item in result["panic_reasons"])
        assert any("breadth_" in item for item in result["panic_reasons"])

    def test_money_flow_retreat_confirms_crash(self, monkeypatch):
        import tools.market_regime as market_regime
        from core.wyckoff_engine import FunnelConfig

        monkeypatch.setattr(market_regime, "_generate_pv_outlook", lambda **_kwargs: "次日推演：测试")

        result = market_regime.analyze_benchmark_and_tune_cfg(
            _benchmark_with_last_drop(-1.5),
            None,
            FunnelConfig(),
            breadth={"ratio_pct": 60.0, "delta_pct": -2.0, "sample_size": 100},
            money_flow={"trend": "retreat", "score": -25.0},
        )

        assert result["regime"] == "CRASH"
        assert any("money_flow_retreat" in item for item in result["panic_reasons"])


# ── tools/data_fetcher ──


class TestDataFetcher:
    def test_latest_trade_date_from_hist_empty(self):
        from tools.data_fetcher import latest_trade_date_from_hist

        assert latest_trade_date_from_hist(pd.DataFrame()) is None

    def test_latest_trade_date_from_hist_no_date_col(self):
        from tools.data_fetcher import latest_trade_date_from_hist

        df = pd.DataFrame({"close": [1, 2, 3]})
        assert latest_trade_date_from_hist(df) is None

    def test_latest_trade_date_from_hist_valid(self):
        from tools.data_fetcher import latest_trade_date_from_hist

        df = pd.DataFrame({"date": ["2025-01-01", "2025-01-02"]})
        result = latest_trade_date_from_hist(df)
        assert result == date(2025, 1, 2)

    def test_tickflow_batch_partial_keeps_available_frames(self, monkeypatch):
        import tools.tickflow_batch_fetcher as batcher

        class FakeTickFlowClient:
            def __init__(self, api_key: str) -> None:
                self.api_key = api_key

            def get_klines_batch(self, *args, **kwargs):
                return {
                    "000001.SZ": pd.DataFrame(
                        {
                            "date": ["2025-01-01", "2025-01-02"],
                            "open": [10.0, 10.1],
                            "high": [10.5, 10.6],
                            "low": [9.8, 9.9],
                            "close": [10.2, 10.3],
                            "volume": [1000, 1100],
                        }
                    )
                }

        window = SimpleNamespace(start_trade_date=date(2025, 1, 1), end_trade_date=date(2025, 1, 2))
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy")
        monkeypatch.setattr(batcher, "TICKFLOW_BATCH_ENABLED", True)
        monkeypatch.setattr(batcher, "TickFlowClient", FakeTickFlowClient)

        result = batcher.fetch_tickflow_daily_batch(
            ["000001", "000002"],
            window,
            enforce_target_trade_date=False,
            batch_size=200,
            batch_sleep=0,
        )

        assert result is not None
        df_map, stats = result
        assert list(df_map) == ["000001"]
        assert stats["fetch_ok"] == 1
        assert stats["fetch_fail"] == 1

    def test_fetch_hist_direct_source_bypasses_cached_repository(self, monkeypatch):
        import integrations.data_source as data_source
        import integrations.fetch_a_share_csv as fetch_csv
        import tools.ohlcv_fallback_fetcher as fallback_fetcher

        calls: list[dict] = []

        def fake_source(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                {
                    "日期": ["2026-05-12", "2026-05-13"],
                    "开盘": [10.0, 10.5],
                    "最高": [10.2, 10.8],
                    "最低": [9.9, 10.4],
                    "收盘": [10.1, 10.7],
                    "成交量": [1000, 1200],
                    "成交额": [10100, 12840],
                    "涨跌幅": [0.0, 5.94],
                    "换手率": [pd.NA, pd.NA],
                    "振幅": [pd.NA, pd.NA],
                }
            )

        def cached_fetch(**kwargs):
            raise AssertionError(f"should bypass cached repository: {kwargs}")

        monkeypatch.setattr(data_source, "fetch_stock_hist", fake_source)
        monkeypatch.setattr(fetch_csv, "fetch_hist", cached_fetch)
        window = SimpleNamespace(start_trade_date=date(2026, 5, 12), end_trade_date=date(2026, 5, 13))

        result = fallback_fetcher._fetch_hist("000001", window, "qfq", direct_source=True)

        assert result["close"].tolist() == [10.1, 10.7]
        assert calls == [
            {
                "symbol": "000001",
                "start": date(2026, 5, 12),
                "end": date(2026, 5, 13),
                "adjust": "qfq",
            }
        ]

    def test_append_spot_bar_zero_fallback_avoids_turnover_pollution(self, monkeypatch):
        import tools.spot_patch as spot_patch

        target = pd.Timestamp.now(tz=spot_patch.CN_TZ).date()
        frame = pd.DataFrame(
            {
                "date": [(target - timedelta(days=1)).isoformat()],
                "open": [10.0],
                "high": [10.4],
                "low": [9.8],
                "close": [10.0],
                "volume": [12345.0],
                "amount": [123450.0],
            }
        )
        monkeypatch.setattr(
            spot_patch,
            "fetch_stock_spot_snapshot",
            lambda *_args, **_kwargs: {
                "open": 10.2,
                "high": 10.5,
                "low": 10.1,
                "close": 10.4,
                "turnover_unit_ok": 0.0,
            },
        )

        patched, ok = spot_patch.append_spot_bar_if_needed(
            "000001", frame, target, env_prefix="TEST", zero_fallback=True
        )

        assert ok is True
        assert patched.iloc[-1]["date"] == target.isoformat()
        assert patched.iloc[-1]["volume"] == 0.0
        assert patched.iloc[-1]["amount"] == 0.0
        assert round(float(patched.iloc[-1]["pct_chg"]), 2) == 4.0

    def test_fetch_all_ohlcv_thread_fallback_counts_success_and_failure(self, monkeypatch):
        import tools.ohlcv_fallback_fetcher as fallback_fetcher
        import tools.tickflow_batch_fetcher as batcher

        def fake_fetch(sym, *_args):
            if sym == "000001":
                return sym, pd.DataFrame({"date": ["2026-05-13"], "close": [10.0]})
            return sym, None

        monkeypatch.setattr(batcher, "fetch_tickflow_daily_batch", lambda **_kwargs: None)
        monkeypatch.setattr(fallback_fetcher, "fetch_one_with_retry_thread", fake_fetch)
        window = SimpleNamespace(start_trade_date=date(2026, 5, 12), end_trade_date=date(2026, 5, 13))

        df_map, stats = fallback_fetcher.fetch_ohlcv_fallback(
            ["000001", "000002"],
            window,
            enforce_target_trade_date=True,
            batch_size=2,
            max_workers=1,
            batch_timeout=10,
            batch_sleep=0,
            executor_mode="thread",
            direct_source=False,
        )

        assert list(df_map) == ["000001"]
        assert stats["fetch_ok"] == 1
        assert stats["fetch_fail"] == 1


# ── tools/symbol_pool ──


class TestSymbolPool:
    def test_load_stock_name_map_callable(self):
        from tools.symbol_pool import load_stock_name_map

        assert callable(load_stock_name_map)

    def test_default_pool_includes_star_and_bse_boards(self, monkeypatch):
        from tools import symbol_pool

        boards = {
            "main": [{"code": "000001", "name": "平安银行"}],
            "chinext": [{"code": "300001", "name": "特锐德"}],
            "star": [{"code": "688001", "name": "华兴源创"}],
            "bse": [{"code": "830000", "name": "北交样本"}],
        }

        monkeypatch.delenv("FUNNEL_POOL_MODE", raising=False)
        monkeypatch.delenv("FUNNEL_POOL_BOARD", raising=False)
        monkeypatch.delenv("FUNNEL_POOL_LIMIT_COUNT", raising=False)
        monkeypatch.setattr(symbol_pool, "get_stocks_by_board", lambda board: boards[board])

        symbols, name_map, stats = symbol_pool.resolve_symbol_pool_from_env()

        assert symbols == ["000001", "300001", "688001", "830000"]
        assert name_map["688001"] == "华兴源创"
        assert stats["pool_star"] == 1
        assert stats["pool_bse"] == 1

    def test_board_pool_accepts_star(self, monkeypatch):
        from tools import symbol_pool

        monkeypatch.setenv("FUNNEL_POOL_MODE", "board")
        monkeypatch.setenv("FUNNEL_POOL_BOARD", "star")
        monkeypatch.delenv("FUNNEL_POOL_LIMIT_COUNT", raising=False)
        monkeypatch.setattr(
            symbol_pool,
            "get_stocks_by_board",
            lambda board: [{"code": "688001", "name": "华兴源创"}] if board == "star" else [],
        )

        symbols, _name_map, stats = symbol_pool.resolve_symbol_pool_from_env()

        assert symbols == ["688001"]
        assert stats["pool_star"] == 1

    def test_board_pool_excludes_st_symbols(self, monkeypatch):
        from tools import symbol_pool

        monkeypatch.setenv("FUNNEL_POOL_MODE", "board")
        monkeypatch.setenv("FUNNEL_POOL_BOARD", "all")
        monkeypatch.delenv("FUNNEL_POOL_LIMIT_COUNT", raising=False)
        boards = {
            "all": [
                {"code": "000001", "name": "平安银行"},
                {"code": "000002", "name": "ST样本"},
                {"code": "830000", "name": "北交样本"},
            ],
            "main": [{"code": "000001", "name": "平安银行"}, {"code": "000002", "name": "ST样本"}],
            "chinext": [],
            "star": [],
            "bse": [{"code": "830000", "name": "北交样本"}],
        }
        monkeypatch.setattr(symbol_pool, "get_stocks_by_board", lambda board: boards[board])

        symbols, name_map, stats = symbol_pool.resolve_symbol_pool_from_env()

        assert symbols == ["000001", "830000"]
        assert name_map == {"000001": "平安银行", "830000": "北交样本"}
        assert stats["pool_merged"] == 3
        assert stats["pool_st_excluded"] == 1

    def test_explicit_board_pool_ignores_env_mode(self, monkeypatch):
        from tools import symbol_pool

        monkeypatch.setenv("FUNNEL_POOL_MODE", "manual")
        monkeypatch.setenv("FUNNEL_POOL_MANUAL_SYMBOLS", "000001")
        monkeypatch.setattr(
            symbol_pool,
            "get_stocks_by_board",
            lambda board: [{"code": "688001", "name": "华兴源创"}] if board == "star" else [],
        )

        symbols, _name_map, stats = symbol_pool.resolve_symbol_pool(pool_mode="board", board_name="star")

        assert symbols == ["688001"]
        assert stats["pool_mode"] == "board"
        assert stats["pool_star"] == 1

    def test_funnel_data_explicit_limit_overrides_env_without_mutation(self, monkeypatch):
        from workflows import funnel_data

        captured = {}

        def fake_resolve_symbol_pool(**kwargs):
            captured.update(kwargs)
            return (
                ["000001", "000002"],
                {"000001": "平安银行", "000002": "万科A"},
                {
                    "pool_mode": kwargs.get("pool_mode", ""),
                    "pool_main": 2,
                    "pool_chinext": 0,
                    "pool_star": 0,
                    "pool_bse": 0,
                    "pool_merged": 2,
                    "pool_st_excluded": 0,
                    "pool_limit": kwargs.get("limit_count", 0),
                },
            )

        monkeypatch.setenv("FUNNEL_POOL_LIMIT_COUNT", "99")
        monkeypatch.setattr(funnel_data, "resolve_symbol_pool", fake_resolve_symbol_pool)
        monkeypatch.setattr(
            funnel_data,
            "_resolve_external_seed_pool",
            lambda symbols: (SimpleNamespace(enabled=False), symbols, 0),
        )

        pool = funnel_data._resolve_funnel_symbol_pool("main", pool_limit_count=12)

        assert captured == {"pool_mode": "board", "board_name": "main", "limit_count": 12}
        assert pool.stats["pool_limit"] == 12
        assert os.environ["FUNNEL_POOL_LIMIT_COUNT"] == "99"

    def test_main_chinext_alias_keeps_legacy_non_bse_boards(self, monkeypatch):
        from tools import symbol_pool

        boards = {
            "main": [{"code": "000001", "name": "平安银行"}],
            "chinext": [{"code": "300001", "name": "特锐德"}],
            "star": [{"code": "688001", "name": "华兴源创"}],
            "bse": [{"code": "830000", "name": "北交样本"}],
        }

        monkeypatch.setenv("FUNNEL_POOL_MODE", "board")
        monkeypatch.setenv("FUNNEL_POOL_BOARD", "main_chinext")
        monkeypatch.delenv("FUNNEL_POOL_LIMIT_COUNT", raising=False)
        monkeypatch.setattr(symbol_pool, "get_stocks_by_board", lambda board: boards[board])

        symbols, _name_map, stats = symbol_pool.resolve_symbol_pool_from_env()

        assert symbols == ["000001", "300001", "688001"]
        assert stats["pool_main"] == 1
        assert stats["pool_chinext"] == 1
        assert stats["pool_star"] == 1
        assert stats["pool_bse"] == 0

    def test_screen_stocks_accepts_mcp_main_chinext_alias(self, monkeypatch):
        from agents import screen_tools

        captured_kwargs = {}
        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return (
                True,
                [],
                {},
                {
                    "metrics": {
                        "pool_limit": kwargs.get("pool_limit_count", 0),
                        "etf_enhancement": {"pool": 2, "fetched": 2, "l2_passed": 1},
                        "etf_candidates": [{"code": "512480", "name": "半导体ETF", "sector": "半导体"}],
                    },
                    "triggers": {},
                    "name_map": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setenv("FUNNEL_POOL_MODE", "manual")
        monkeypatch.setenv("FUNNEL_POOL_BOARD", "chinext")
        monkeypatch.setenv("FUNNEL_EXECUTOR_MODE", "process")

        result = screen_tools.screen_stocks(board="main_chinext", limit=25)

        assert "error" not in result
        assert captured_kwargs["pool_board"] == "main_chinext_star"
        assert captured_kwargs["pool_limit_count"] == 25
        assert captured_kwargs["executor_mode"] == "thread"
        assert result["etf_enhancement"] == {"pool": 2, "fetched": 2, "l2_passed": 1}
        assert result["etf_candidates"] == [{"code": "512480", "name": "半导体ETF", "sector": "半导体"}]
        assert result["scan_scope"] == {
            "scope": "bounded",
            "board": "main_chinext_star",
            "limit": 25,
            "total_scanned": 0,
            "financial_metrics": "requested_unavailable",
            "financial_metrics_count": 0,
        }
        assert os.environ["FUNNEL_POOL_MODE"] == "manual"
        assert os.environ["FUNNEL_POOL_BOARD"] == "chinext"
        assert os.environ["FUNNEL_EXECUTOR_MODE"] == "process"

        captured_kwargs.clear()
        result = screen_tools.screen_stocks(board="主板和科创板", limit=25)

        assert "error" not in result
        assert captured_kwargs["pool_board"] == "main_chinext_star"
        assert result["scan_scope"]["board"] == "main_chinext_star"

        captured_kwargs.clear()
        result = screen_tools.screen_stocks(board="沪深A股", limit=25)

        assert "error" not in result
        assert captured_kwargs["pool_board"] == "main_chinext_star"
        assert result["scan_scope"]["board"] == "main_chinext_star"

    def test_screen_stocks_surfaces_bounded_scan_scope(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {"metrics": {"total_symbols": 10, "pool_limit": 25}, "triggers": {}, "name_map": {}},
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(limit=25)

        assert result["scan_scope"] == {
            "scope": "bounded",
            "board": "all",
            "limit": 25,
            "total_scanned": 10,
            "financial_metrics": "requested_unavailable",
            "financial_metrics_count": 0,
        }
        assert result["summary"]["scan_limit"] == 25

    def test_screen_stocks_surfaces_data_quality_warnings(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {
                    "metrics": {
                        "total_symbols": 100,
                        "fetch_ok": 87,
                        "fetch_fail": 13,
                        "fetch_date_mismatch": 2,
                        "fetch_spot_patched": 5,
                        "end_trade_date": "2026-06-30",
                    },
                    "triggers": {},
                    "name_map": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["data_quality"]["status"] == "degraded"
        assert result["data_quality"]["coverage_pct"] == 87.0
        assert result["data_quality"]["end_trade_date"] == "2026-06-30"
        assert "13只股票拉取失败" in result["data_quality"]["warnings"]
        assert "2只股票交易日不匹配" in result["data_quality"]["warnings"]
        assert result["data_quality"]["action"] == "不要直接据此选股，先重跑或缩小扫描范围"

    def test_screen_stocks_without_limit_preserves_env_limit(self, monkeypatch):
        from agents import screen_tools

        captured_kwargs = {}
        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return True, [], {}, {"metrics": {"pool_limit": 99}, "triggers": {}, "name_map": {}}

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        monkeypatch.setenv("FUNNEL_POOL_LIMIT_COUNT", "99")

        result = screen_tools.screen_stocks()

        assert captured_kwargs["pool_limit_count"] is None
        assert result["scan_scope"]["limit"] == 99
        assert os.environ["FUNNEL_POOL_LIMIT_COUNT"] == "99"

    def test_screen_stocks_uses_agent_default_limit_for_chat_context(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        captured_kwargs = {}
        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return (
                True,
                [],
                {},
                {
                    "metrics": {
                        "total_symbols": 1200,
                        "pool_limit": kwargs.get("pool_limit_count", 0),
                    },
                    "triggers": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(tool_context=ToolContext())

        assert captured_kwargs["pool_limit_count"] == 1200
        assert captured_kwargs["include_financial_metrics"] is False
        assert result["scan_scope"] == {
            "scope": "bounded",
            "board": "all",
            "limit": 1200,
            "total_scanned": 1200,
            "financial_metrics": "skipped_quick_scan",
            "financial_metrics_count": 0,
        }

    def test_screen_stocks_allows_explicit_full_scan_in_chat_context(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        captured_kwargs = {}
        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return True, [], {}, {"metrics": {"total_symbols": 5000, "pool_limit": 0}, "triggers": {}}

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(limit=0, tool_context=ToolContext())

        assert captured_kwargs["pool_limit_count"] == 0
        assert captured_kwargs["include_financial_metrics"] is True
        assert result["scan_scope"] == {
            "scope": "full",
            "board": "all",
            "limit": 0,
            "total_scanned": 5000,
            "financial_metrics": "requested_unavailable",
            "financial_metrics_count": 0,
        }

    def test_screen_stocks_allows_explicit_financial_metrics_in_chat_context(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        captured_kwargs = {}
        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            return (
                True,
                [],
                {},
                {
                    "metrics": {
                        "total_symbols": 1200,
                        "pool_limit": kwargs.get("pool_limit_count", 0),
                        "financial_metrics_count": 1180,
                    },
                    "triggers": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(financial_metrics=True, tool_context=ToolContext())

        assert captured_kwargs["include_financial_metrics"] is True
        assert result["scan_scope"]["financial_metrics"] == "available"
        assert result["scan_scope"]["financial_metrics_count"] == 1180

    def test_screen_stocks_rejects_invalid_scan_limit(self, monkeypatch):
        from agents import screen_tools

        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(limit=3001)

        assert "limit 最大支持 3000" in result["error"]

    def test_screen_stocks_sanitizes_nonfinite_trigger_scores(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {
                    "metrics": {},
                    "triggers": {
                        "sos": [
                            ("000001", float("inf")),
                            ("000002", float("nan")),
                            ("000003", "bad"),
                            ("000004", 12.345),
                        ]
                    },
                    "name_map": {"000001": "平安银行"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert [row["score"] for row in result["trigger_groups"]["sos"]] == [0.0, 0.0, 0.0, 12.35]
        assert result["trigger_groups"]["sos"][0]["name"] == "平安银行"

    def test_screen_stocks_returns_ranked_unique_top_candidates(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                ["000002", "000004"],
                {},
                {
                    "metrics": {},
                    "triggers": {
                        "lps": [("000001", 6.0), ("000002", 8.0)],
                        "sos": [("000001", 9.5), ("000003", 7.0)],
                    },
                    "name_map": {"000001": "候选一", "000002": "候选二", "000004": "补充候选"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["board"] == "all"
        assert result["summary"]["report_candidates"] == 2
        candidates = result["top_candidates"]
        assert [row["code"] for row in candidates] == ["000002", "000004", "000001", "000003"]
        assert candidates[0]["selected_for_report"] is True
        assert candidates[0]["priority_rank"] == 1
        assert candidates[0]["score"] == 8.0
        assert candidates[0]["rank_reason"] == "研报候选#1；LPS"
        assert candidates[1]["selected_for_report"] is True
        assert candidates[1]["priority_rank"] == 2
        assert candidates[2]["selected_for_report"] is False
        assert candidates[2]["triggers"] == ["lps", "sos"]

    def test_screen_stocks_style_preference_reorders_report_candidates(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "低吸候选",
                        "priority_rank": 1,
                        "track": "Accum",
                        "stage": "Accum_B",
                        "entry_type": "springboard",
                    },
                    {
                        "code": "000002",
                        "name": "趋势候选",
                        "priority_rank": 2,
                        "track": "Trend",
                        "stage": "Markup",
                    },
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000002", 8.0)], "lps": [("000001", 8.5)]},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(style="trend")

        assert result["style_preference"] == {"raw": "trend", "styles": ["trend"]}
        assert result["preference_match"] == {"style": "hit"}
        assert result["scan_scope"]["style_preference"]["styles"] == ["trend"]
        assert result["scan_scope"]["preference_match"] == {"style": "hit"}
        assert [row["code"] for row in result["top_candidates"][:2]] == ["000002", "000001"]
        first = result["selection_brief"]["primary_pick"]
        assert first["code"] == "000002"
        assert first["style_match"] is True
        assert "趋势偏好: 趋势线" in first["style_match_reasons"]
        assert "趋势偏好: 趋势线" in first["quality_factors"]

        pullback_result = screen_tools.screen_stocks(style="不追高")
        assert pullback_result["style_preference"] == {"raw": "不追高", "styles": ["pullback"]}
        assert pullback_result["preference_match"] == {"style": "hit"}
        assert [row["code"] for row in pullback_result["top_candidates"][:2]] == ["000001", "000002"]

        quality_result = screen_tools.screen_stocks(style="流动性好")
        assert quality_result["style_preference"] == {"raw": "流动性好", "styles": ["quality"]}

        typo_result = screen_tools.screen_stocks(style="强事底吸")
        assert typo_result["style_preference"] == {"raw": "强事底吸", "styles": ["trend", "pullback"]}
        assert typo_result["preference_match"] == {"style": "partial"}

    def test_screen_stocks_combined_style_prefers_candidates_matching_all_styles(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "纯趋势",
                        "priority_rank": 1,
                        "track": "Trend",
                        "stage": "Markup",
                    },
                    {
                        "code": "000002",
                        "name": "趋势低吸",
                        "priority_rank": 2,
                        "track": "Trend",
                        "stage": "Setup",
                        "entry_type": "springboard",
                    },
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000001", 9.0), ("000002", 8.0)]},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(style="trend,pullback")

        assert result["preference_match"] == {"style": "hit"}
        assert [row["code"] for row in result["top_candidates"][:2]] == ["000002", "000001"]
        assert result["selection_brief"]["primary_pick"]["style_match_styles"] == ["trend", "pullback"]
        trend_only = next(row for row in result["top_candidates"] if row["code"] == "000001")
        assert trend_only["style_match_styles"] == ["trend"]
        assert "风格偏好未命中: 低吸" in trend_only["risk_factors"]

    def test_screen_stocks_combined_style_reports_partial_match(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "纯趋势研报",
                        "priority_rank": 1,
                        "track": "Trend",
                        "stage": "Markup",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000002", 8.0)]},
                    "name_map": {"000002": "纯趋势观察"},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(style="trend,pullback")

        assert result["preference_match"] == {"style": "partial"}
        assert result["scan_scope"]["preference_match"] == {"style": "partial"}
        assert "preference_alternatives" not in result["selection_brief"]
        assert "风格偏好未命中: 低吸" in result["selection_brief"]["primary_pick"]["risk_factors"]

    def test_screen_stocks_preference_alternatives_require_all_styles(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "纯趋势研报",
                        "priority_rank": 1,
                        "track": "Trend",
                        "stage": "Markup",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000002", 9.0), ("000003", 8.0)]},
                    "name_map": {"000002": "纯趋势观察", "000003": "趋势低吸观察"},
                    "candidate_entries": [
                        {"code": "000003", "entry_type": "springboard", "lane": "springboard", "score": 8.0}
                    ],
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(style="trend,pullback")

        alternatives = result["selection_brief"]["preference_alternatives"]
        assert [row["code"] for row in alternatives] == ["000003"]
        partial_watch = next(row for row in result["top_candidates"] if row["code"] == "000002")
        assert partial_watch["style_match_styles"] == ["trend"]
        assert "风格偏好未命中: 低吸" in partial_watch["risk_factors"]
        full_watch = next(row for row in result["top_candidates"] if row["code"] == "000003")
        assert full_watch["style_match_styles"] == ["trend", "pullback"]

    def test_screen_stocks_theme_preference_reorders_and_labels_candidates(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "普通候选",
                        "priority_rank": 1,
                        "priority_score": 20.0,
                        "strategic_theme": "芯片半导体",
                    },
                    {
                        "code": "000002",
                        "name": "机器人股",
                        "priority_rank": 2,
                        "priority_score": 1.0,
                        "strategic_theme": "机器人",
                    },
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(theme="机器人", limit=25)

        assert result["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
        assert result["preference_match"] == {"theme": "hit"}
        assert result["scan_scope"]["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
        assert result["scan_scope"]["preference_match"] == {"theme": "hit"}
        assert [row["code"] for row in result["top_candidates"][:2]] == ["000002", "000001"]
        first = result["selection_brief"]["primary_pick"]
        assert first["code"] == "000002"
        assert first["theme_match"] is True
        assert first["theme_match_reasons"] == ["主题偏好: 机器人"]
        assert "主题偏好: 机器人" in first["quality_factors"]

    def test_screen_stocks_surfaces_theme_preference_miss_in_result_and_handoff(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "芯片候选",
                        "priority_rank": 1,
                        "priority_score": 20.0,
                        "strategic_theme": "芯片半导体",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        ctx = ToolContext()

        result = screen_tools.screen_stocks(theme="机器人", limit=25, tool_context=ctx)

        assert result["theme_preference"] == {"raw": "机器人", "theme": "机器人"}
        assert result["preference_match"] == {"theme": "miss"}
        assert result["scan_scope"]["preference_match"] == {"theme": "miss"}
        assert result["selection_brief"]["primary_pick"]["code"] == "000001"
        assert "theme_match" not in result["selection_brief"]["primary_pick"]
        assert "主题偏好未命中: 机器人" in result["selection_brief"]["primary_pick"]["risk_factors"]
        assert "主题偏好未命中: 机器人" in result["action_plan"]["report_candidates"][0]["risk_factors"]
        assert "主题偏好未命中: 机器人" in result["top_candidates"][0]["risk_factors"]
        assert ctx.state["last_screen_result"]["preference_match"] == {"theme": "miss"}
        assert ctx.state["last_screen_result"]["scan_scope"]["preference_match"] == {"theme": "miss"}
        assert "主题偏好未命中: 机器人" in ctx.state["last_screen_result"]["report_candidates"][0]["risk_factors"]

    def test_screen_stocks_marks_report_candidate_miss_when_watch_candidate_matches_preference(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "芯片候选",
                        "priority_rank": 1,
                        "priority_score": 20.0,
                        "strategic_theme": "芯片半导体",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000002", 8.0)]},
                    "name_map": {"000002": "机器人观察"},
                    "trade_mode": {"allow_ai_review": True, "allow_recommendation_write": False},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks(theme="机器人", limit=25)

        assert result["preference_match"] == {"theme": "hit"}
        assert result["selection_brief"]["primary_pick"]["code"] == "000001"
        assert "主题偏好未命中: 机器人" in result["selection_brief"]["primary_pick"]["risk_factors"]
        assert "主题偏好未命中: 机器人" in result["action_plan"]["report_candidates"][0]["risk_factors"]
        assert result["selection_brief"]["preference_alternatives"][0]["code"] == "000002"
        assert result["selection_brief"]["preference_alternatives"][0]["action_status"] == "watch_only"
        matched_watch = next(row for row in result["top_candidates"] if row["code"] == "000002")
        assert matched_watch["theme_match"] is True
        assert "主题偏好未命中: 机器人" not in matched_watch.get("risk_factors", [])

    def test_screen_stocks_surfaces_shadow_score_for_candidate_review(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {
                    "metrics": {},
                    "triggers": {"lps": [("000001", 7.0), ("000002", 7.0)]},
                    "shadow_score_map": {"000001": 1.2, "000002": 5.5},
                    "name_map": {"000001": "低分观察", "000002": "高分观察"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        candidates = result["top_candidates"]
        assert [row["code"] for row in candidates[:2]] == ["000002", "000001"]
        assert candidates[0]["shadow_score"] == 5.5
        assert candidates[0]["rank_reason"] == "动态策略分 5.50；LPS"
        assert "动态策略分 5.50" in candidates[0]["quality_factors"]
        assert result["selection_brief"]["primary_pick"]["shadow_score"] == 5.5
        assert result["action_plan"]["watch_candidates"][0]["shadow_score"] == 5.5

    def test_screen_stocks_decision_state_prefers_market_reason_for_watch_pool(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {
                    "metrics": {"total_symbols": 1, "fetch_ok": 1, "fetch_fail": 0},
                    "triggers": {"sos": [("000001", 7.0)]},
                    "trade_mode": {
                        "mode": "observe_only",
                        "reason": "市场闸门关闭",
                        "allow_ai_review": False,
                        "allow_recommendation_write": False,
                    },
                    "name_map": {"000001": "观察候选"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["selection_brief"]["status"] == "watch_only"
        assert result["decision_state"] == {
            "status": "watch_only",
            "label": "观察候选",
            "trade_readiness": "watch_only",
            "new_buy_allowed": False,
            "candidate_direct_buy_allowed": False,
            "candidate_guard_reason": "候选状态 watch_only 不允许直接买入",
            "ai_review_allowed": False,
            "primary": "000001 观察候选",
            "reason": "市场闸门关闭",
            "next_step": "观察池跟踪，暂不进入本轮AI复核",
            "summary": (
                "筛股决策: 观察候选 · 首选: 000001 观察候选 · 市场新增: 关 · 候选直买: 禁 · "
                "AI复核: 不可 · 原因: 市场闸门关闭 · 下一步: 观察池跟踪，暂不进入本轮AI复核"
            ),
        }

    def test_screen_stocks_decision_state_separates_market_gate_from_candidate_guard(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000001",
                        "name": "待确认候选",
                        "priority_rank": 1,
                        "track": "Trend",
                        "stage": "Markup",
                    }
                ],
                {},
                {
                    "metrics": {"total_symbols": 1, "fetch_ok": 1, "fetch_fail": 0},
                    "triggers": {"sos": [("000001", 8.0)]},
                    "trade_mode": {
                        "mode": "confirmation_only",
                        "reason": "等待二次确认",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                    "name_map": {"000001": "待确认候选"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["decision_state"] == {
            "status": "ready_for_ai_review",
            "label": "AI复核候选",
            "trade_readiness": "confirmation_required",
            "new_buy_allowed": True,
            "candidate_direct_buy_allowed": False,
            "candidate_guard_reason": "候选状态 confirmation_required 不允许直接买入",
            "ai_review_allowed": True,
            "primary": "000001 待确认候选",
            "reason": "候选已可进入 AI 研报复核",
            "next_step": "进入AI复核，等待二次确认后再行动",
            "summary": (
                "筛股决策: AI复核候选 · 首选: 000001 待确认候选 · 市场新增: 开 · 候选直买: 禁 · "
                "AI复核: 可 · 原因: 候选已可进入 AI 研报复核 · 下一步: 进入AI复核，等待二次确认后再行动"
            ),
        }
        assert (
            result["candidate_guard_summary"]["candidates"][0]["reason"]
            == "候选状态 confirmation_required 不允许直接买入"
        )

    def test_screen_stocks_uses_report_row_metadata_for_top_candidates(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000004",
                        "name": "主线候选",
                        "priority_rank": 1,
                        "priority_score": 12.5,
                        "selection_source": "mainline",
                        "track": "Trend",
                        "stage": "Markup",
                        "tag": "主线买点确认 | 威科夫候选",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {"sos": [("000001", 99.0)]},
                    "priority_score_map": {"000004": 12.5},
                    "trade_mode": {
                        "regime": "RISK_OFF",
                        "mode": "observe_only",
                        "label": "风险规避",
                        "action": "不新增买入",
                        "reason": "大盘风险闸门关闭",
                        "allow_ai_review": False,
                        "allow_recommendation_write": False,
                        "internal_note": "not exposed",
                    },
                    "name_map": {"000001": "高分未选"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        first = result["top_candidates"][0]
        assert result["summary"]["report_candidates"] == 1
        assert result["trade_mode"] == {
            "regime": "RISK_OFF",
            "mode": "observe_only",
            "label": "风险规避",
            "action": "不新增买入",
            "reason": "大盘风险闸门关闭",
            "allow_ai_review": False,
            "allow_recommendation_write": False,
        }
        assert result["decision_brief"] == {
            "market_gate": "风险规避 / 不新增买入 / 大盘风险闸门关闭",
            "next_action": "只观察，不新增买入",
            "report_focus": [
                {
                    "code": "000004",
                    "name": "主线候选",
                    "quality": "高优先级研报候选",
                    "evidence": "趋势线 / 主升阶段 / 主线买点；研报候选#1；优先分 12.50",
                    "quality_factors": [
                        "高优先级研报候选",
                        "趋势线",
                        "主升阶段",
                        "主线买点",
                        "研报候选#1",
                        "优先分 12.50",
                        "只观察，等待市场风险闸门重新打开",
                    ],
                    "risk_factors": ["大盘风险闸门关闭"],
                    "action_status": "blocked_by_market_gate",
                    "action_label": "风险闸门关闭",
                    "action_level": "blocked",
                    "direct_buy_allowed": False,
                    "next_step": "只观察，等待市场风险闸门重新打开",
                    "summary": (
                        "000004 主线候选: 趋势线 / 主升阶段 / 主线买点；研报候选#1；优先分 12.50；"
                        "只观察，等待市场风险闸门重新打开"
                    ),
                }
            ],
            "watch_focus": [
                {
                    "code": "000001",
                    "name": "高分未选",
                    "quality": "强观察候选",
                    "evidence": "触发:SOS；SOS",
                    "quality_factors": ["强观察候选", "触发:SOS", "SOS", "观察池跟踪，暂不进入本轮AI复核"],
                    "risk_factors": ["未进入本轮研报候选", "观察池，不进入本轮AI复核"],
                    "action_status": "watch_only",
                    "action_label": "观察池",
                    "action_level": "watch",
                    "direct_buy_allowed": False,
                    "next_step": "观察池跟踪，暂不进入本轮AI复核",
                    "summary": "000001 高分未选: 触发:SOS；SOS；观察池跟踪，暂不进入本轮AI复核",
                }
            ],
        }
        assert result["selection_brief"] == {
            "status": "blocked_by_market_gate",
            "headline": "本轮有强候选，但市场闸门未打开: 000004 主线候选",
            "best_codes": ["000004"],
            "primary_pick": {
                "code": "000004",
                "name": "主线候选",
                "tier": "高优先级研报候选",
                "why": "趋势线 / 主升阶段 / 主线买点；研报候选#1；优先分 12.50",
                "quality_factors": [
                    "高优先级研报候选",
                    "趋势线",
                    "主升阶段",
                    "主线买点",
                    "研报候选#1",
                    "优先分 12.50",
                    "只观察，等待市场风险闸门重新打开",
                ],
                "risk_factors": ["大盘风险闸门关闭"],
                "action_status": "blocked_by_market_gate",
                "action_label": "风险闸门关闭",
                "action_level": "blocked",
                "direct_buy_allowed": False,
                "next_step": "只观察，等待市场风险闸门重新打开",
                "priority_score": 12.5,
                "score": 0.0,
                "track": "Trend",
                "stage": "Markup",
            },
            "best_candidates": [
                {
                    "code": "000004",
                    "name": "主线候选",
                    "tier": "高优先级研报候选",
                    "why": "趋势线 / 主升阶段 / 主线买点；研报候选#1；优先分 12.50",
                    "quality_factors": [
                        "高优先级研报候选",
                        "趋势线",
                        "主升阶段",
                        "主线买点",
                        "研报候选#1",
                        "优先分 12.50",
                        "只观察，等待市场风险闸门重新打开",
                    ],
                    "risk_factors": ["大盘风险闸门关闭"],
                    "action_status": "blocked_by_market_gate",
                    "action_label": "风险闸门关闭",
                    "action_level": "blocked",
                    "direct_buy_allowed": False,
                    "next_step": "只观察，等待市场风险闸门重新打开",
                    "priority_score": 12.5,
                    "score": 0.0,
                    "track": "Trend",
                    "stage": "Markup",
                }
            ],
        }
        assert result["action_plan"] == {
            "primary_action": "不新增买入",
            "candidate_action": "只观察，不新增买入",
            "new_buy_allowed": False,
            "ai_review_allowed": False,
            "trade_readiness": "observe_only",
            "review_targets": {
                "codes": ["000004"],
                "status": "blocked",
                "reason": "大盘风险闸门关闭",
            },
            "report_candidates": [
                {
                    "code": "000004",
                    "name": "主线候选",
                    "quality": "高优先级研报候选",
                    "profile": "趋势线 / 主升阶段 / 主线买点",
                    "next_step": "只观察，等待市场风险闸门重新打开",
                    "rank_reason": "研报候选#1；优先分 12.50",
                    "quality_factors": [
                        "高优先级研报候选",
                        "趋势线",
                        "主升阶段",
                        "主线买点",
                        "研报候选#1",
                        "优先分 12.50",
                        "只观察，等待市场风险闸门重新打开",
                    ],
                    "risk_factors": ["大盘风险闸门关闭"],
                    "action_status": "blocked_by_market_gate",
                    "action_label": "风险闸门关闭",
                    "action_level": "blocked",
                    "direct_buy_allowed": False,
                    "priority_score": 12.5,
                    "selection_source": "mainline",
                    "track": "Trend",
                    "stage": "Markup",
                    "tag": "主线买点确认 | 威科夫候选",
                }
            ],
            "watch_candidates": [
                {
                    "code": "000001",
                    "name": "高分未选",
                    "quality": "强观察候选",
                    "profile": "触发:SOS",
                    "next_step": "观察池跟踪，暂不进入本轮AI复核",
                    "rank_reason": "SOS",
                    "quality_factors": ["强观察候选", "触发:SOS", "SOS", "观察池跟踪，暂不进入本轮AI复核"],
                    "risk_factors": ["未进入本轮研报候选", "观察池，不进入本轮AI复核"],
                    "action_status": "watch_only",
                    "action_label": "观察池",
                    "action_level": "watch",
                    "direct_buy_allowed": False,
                    "priority_score": 0.0,
                    "triggers": ["sos"],
                }
            ],
        }
        assert result["decision_state"] == {
            "status": "blocked_by_market_gate",
            "label": "好股观察",
            "trade_readiness": "observe_only",
            "new_buy_allowed": False,
            "candidate_direct_buy_allowed": False,
            "candidate_guard_reason": "候选状态 blocked_by_market_gate 不允许直接买入",
            "ai_review_allowed": False,
            "primary": "000004 主线候选",
            "reason": "大盘风险闸门关闭",
            "next_step": "只观察，等待市场风险闸门重新打开",
            "summary": (
                "筛股决策: 好股观察 · 首选: 000004 主线候选 · 市场新增: 关 · 候选直买: 禁 · "
                "AI复核: 不可 · 原因: 大盘风险闸门关闭 · 下一步: 只观察，等待市场风险闸门重新打开"
            ),
        }
        assert first["code"] == "000004"
        assert first["selected_for_report"] is True
        assert first["priority_score"] == 12.5
        assert first["selection_source"] == "mainline"
        assert first["track"] == "Trend"
        assert first["stage"] == "Markup"
        assert first["tag"] == "主线买点确认 | 威科夫候选"
        assert first["rank_reason"] == "研报候选#1；优先分 12.50"
        assert first["action_status"] == "blocked_by_market_gate"
        assert first["next_step"] == "只观察，等待市场风险闸门重新打开"
        assert first["quality_factors"] == [
            "高优先级研报候选",
            "趋势线",
            "主升阶段",
            "主线买点",
            "研报候选#1",
            "优先分 12.50",
        ]
        assert first["risk_factors"] == ["大盘风险闸门关闭"]
        assert result["top_candidates"][1]["code"] == "000001"
        assert result["top_candidates"][1]["action_status"] == "watch_only"
        assert result["top_candidates"][1]["next_step"] == "观察池跟踪，暂不进入本轮AI复核"
        assert result["top_candidates"][1]["risk_factors"] == ["未进入本轮研报候选", "观察池，不进入本轮AI复核"]

    def test_screen_stocks_surfaces_candidate_quality_metrics_in_briefs(self, monkeypatch):
        from agents import screen_tools
        from utils.tool_result_preview import tool_result_brief_lines

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000004",
                        "name": "主线候选",
                        "priority_rank": 1,
                        "priority_score": 12.5,
                        "selection_source": "recommendation_event_eval",
                        "track": "Trend",
                        "stage": "Markup",
                        "funnel_score": 89.5,
                        "candidate_shadow_score": 92.0,
                        "candidate_shadow_grade": "S",
                        "entry_quality_score": 84.0,
                        "entry_quality_grade": "A",
                        "entry_quality_risk_flags": ["短线涨幅偏快"],
                        "selection_strategy": "candidate_shadow_then_score",
                        "recommend_date": "2026-06-30",
                        "is_ai_recommended": True,
                        "recommend_count": 2,
                        "label_ready": False,
                        "label_status": "pending",
                    }
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {},
                    "trade_mode": {
                        "regime": "RISK_ON",
                        "mode": "risk_on",
                        "label": "风险打开",
                        "action": "允许候选进入AI复核",
                        "reason": "市场闸门打开",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()
        rows = (
            result["top_candidates"][0],
            result["selection_brief"]["primary_pick"],
            result["decision_brief"]["report_focus"][0],
            result["action_plan"]["report_candidates"][0],
        )

        for row in rows:
            assert row["funnel_score"] == 89.5
            assert row["candidate_shadow_score"] == 92.0
            assert row["candidate_shadow_grade"] == "S"
            assert row["entry_quality_score"] == 84.0
            assert row["entry_quality_grade"] == "A"
            assert row["candidate_quality_score"] == 92.0
            assert row["risk_adjusted_quality_score"] == 87.0
            assert row["entry_risk_penalty"] == 5.0
            assert row["selection_strategy"] == "candidate_shadow_then_score"
            assert row["label_ready"] is False
            assert row["label_status"] == "pending"
            assert "候选影子评级 S" in row["quality_factors"]
            assert "入场质量评级 A" in row["quality_factors"]
            assert "短线涨幅偏快" in row["risk_factors"]

        assert result["top_candidates"][0]["quality_factors"][0] == "高质量研报候选"
        assert result["selection_brief"]["primary_pick"]["tier"] == "高质量研报候选"
        assert result["decision_brief"]["report_focus"][0]["quality"] == "高质量研报候选"
        assert result["action_plan"]["report_candidates"][0]["quality"] == "高质量研报候选"

        lines = tool_result_brief_lines("screen_stocks", result, max_lines=3)
        assert result["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
        assert result["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
        assert any(line.startswith("候选护栏: 1只禁止直接买入") for line in lines)
        assert any("候选影子S/92" in line and "入场A/84" in line and "风险调整分87" in line for line in lines)

    def test_screen_stocks_uses_quality_score_as_same_priority_tiebreaker(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000010",
                        "name": "高触发普通候选",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                    },
                    {
                        "code": "000011",
                        "name": "高质量候选",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                        "funnel_score": 88.0,
                        "candidate_shadow_score": 92.0,
                        "candidate_shadow_grade": "S",
                        "entry_quality_score": 84.0,
                        "entry_quality_grade": "A",
                    },
                ],
                {},
                {
                    "metrics": {},
                    "triggers": {
                        "sos": [("000010", 99.0), ("000011", 20.0)],
                    },
                    "trade_mode": {
                        "mode": "risk_on",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert [row["code"] for row in result["top_candidates"][:2]] == ["000011", "000010"]
        first = result["top_candidates"][0]
        assert first["rank_reason"] == "研报候选#1；优先分 10.00；质量分 92.00；SOS"
        assert first["quality_factors"][0] == "高质量研报候选"
        assert result["selection_brief"]["primary_pick"]["code"] == "000011"

    def test_screen_stocks_penalizes_entry_quality_risks_in_same_priority_sort(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000011",
                        "name": "带风险高分",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                        "candidate_shadow_score": 92.0,
                        "candidate_shadow_grade": "S",
                        "entry_quality_score": 84.0,
                        "entry_quality_grade": "A",
                        "entry_quality_risk_flags": ["短线涨幅偏快"],
                    },
                    {
                        "code": "000012",
                        "name": "无风险次高分",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                        "candidate_shadow_score": 90.0,
                        "candidate_shadow_grade": "S",
                        "entry_quality_score": 82.0,
                        "entry_quality_grade": "A",
                    },
                ],
                {},
                {
                    "metrics": {"total_symbols": 2, "fetch_ok": 2, "fetch_fail": 0},
                    "triggers": {"sos": [("000011", 20.0), ("000012", 20.0)]},
                    "trade_mode": {
                        "mode": "risk_on",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert [row["code"] for row in result["top_candidates"][:2]] == ["000012", "000011"]
        risky = result["top_candidates"][1]
        assert risky["rank_reason"] == "研报候选#1；优先分 10.00；质量分 87.00；入场风险扣减 5.00；SOS"
        assert risky["risk_factors"] == ["短线涨幅偏快"]

    def test_screen_stocks_downgrades_low_quality_report_candidate_to_watch(self, monkeypatch):
        from agents import report_tools, screen_tools
        from agents.tool_context import ToolContext

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000013",
                        "name": "低质量研报候选",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                        "candidate_shadow_score": 65.0,
                        "candidate_shadow_grade": "C",
                        "entry_quality_score": 60.0,
                        "entry_quality_grade": "C",
                    }
                ],
                {},
                {
                    "metrics": {"total_symbols": 1, "fetch_ok": 1, "fetch_fail": 0},
                    "triggers": {"sos": [("000013", 20.0)]},
                    "trade_mode": {
                        "mode": "risk_on",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        ctx = ToolContext()

        result = screen_tools.screen_stocks(tool_context=ctx)
        report = result["action_plan"]["review_targets"]

        assert result["selection_brief"]["status"] == "watch_only"
        assert "tool_handoff" not in result["selection_brief"]
        assert result["action_plan"]["ai_review_allowed"] is False
        assert result["action_plan"]["new_buy_allowed"] is False
        assert result["action_plan"]["report_candidates"] == []
        assert result["symbols_for_report"] == []
        assert result["report_candidates"] == []
        assert result["watch_candidates"][0]["code"] == "000013"
        assert result["quality_gate"]["status"] == "blocked_by_quality_gate"
        assert result["summary"]["report_candidates"] == 0
        assert result["summary"]["watch_candidates"] == 1
        assert result["diagnosis_targets"][0]["tool"] == "analyze_stock"
        assert result["diagnosis_targets"][0]["args"] == {"code": "000013", "mode": "diagnose"}
        assert result["next_tool"] == {
            "tool": "analyze_stock",
            "args": {"code": "000013", "mode": "diagnose"},
            "reason": "观察候选先做个股结构诊断",
        }
        assert result["next_action"] == "观察候选先做个股结构诊断"
        top = result["top_candidates"][0]
        assert top["selected_for_report"] is False
        assert top["raw_selected_for_report"] is True
        assert top["action_status"] == "watch_only"
        assert top["next_step"] == "观察池跟踪，暂不进入本轮AI复核"
        assert report["status"] == "blocked_by_quality_gate"
        assert "000013 低质量研报候选 风险调整质量分 65.00 低于AI复核门槛 70.00" in report["reason"]
        watch = result["action_plan"]["watch_candidates"][0]
        assert watch["code"] == "000013"
        assert watch["action_status"] == "watch_only"
        assert any("风险调整质量分 65.00 低于AI复核门槛 70.00" in item for item in watch["risk_factors"])
        assert result["candidate_guard_summary"]["candidates"][0]["reason"] == "候选状态 watch_only 不允许直接买入"
        assert ctx.state["last_screen_result"]["symbols_for_report"] == []
        assert ctx.state["last_screen_result"]["watch_candidates"][0]["code"] == "000013"
        assert ctx.state["last_screen_result"]["diagnosis_targets"][0]["args"]["code"] == "000013"
        assert ctx.state["last_screen_result"]["quality_gate"]["status"] == "blocked_by_quality_gate"

        monkeypatch.setattr(report_tools, "ensure_tushare_token", lambda _tool_context: None)
        monkeypatch.setattr(
            report_tools,
            "run_ai_report",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not auto-run report")),
        )

        blocked = report_tools.generate_ai_report(tool_context=ctx)

        assert blocked["status"] == "blocked_by_quality_gate"
        assert blocked["error"].startswith("上一轮候选质量门槛未过")
        assert "低于AI复核门槛 70.00" in blocked["reason"]

    def test_screen_stocks_keeps_ready_handoff_when_other_candidate_passes_quality_gate(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000014",
                        "name": "高质量候选",
                        "priority_rank": 1,
                        "priority_score": 10.0,
                        "candidate_shadow_score": 88.0,
                    },
                    {
                        "code": "000013",
                        "name": "低质量候选",
                        "priority_rank": 2,
                        "priority_score": 9.0,
                        "candidate_shadow_score": 65.0,
                    },
                ],
                {},
                {
                    "metrics": {"total_symbols": 2, "fetch_ok": 2, "fetch_fail": 0},
                    "triggers": {"sos": [("000014", 20.0), ("000013", 20.0)]},
                    "trade_mode": {
                        "mode": "risk_on",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["selection_brief"]["status"] == "ready_for_ai_review"
        assert result["selection_brief"]["tool_handoff"]["args"]["stock_codes"] == ["000014"]
        assert result["action_plan"]["ai_review_allowed"] is True
        assert result["action_plan"]["review_targets"]["status"] == "ready"
        assert [row["code"] for row in result["action_plan"]["report_candidates"]] == ["000014"]
        assert [row["code"] for row in result["symbols_for_report"]] == ["000014"]
        assert [row["code"] for row in result["report_candidates"]] == ["000014"]
        assert result["watch_candidates"][0]["code"] == "000013"
        assert result["quality_gate"]["blocked_count"] == 1
        assert result["summary"]["report_candidates"] == 1
        assert result["summary"]["watch_candidates"] == 1
        assert result["next_tool"] == {
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["000014"]},
            "reason": "首选候选已通过市场闸门，可进入 AI 研报复核",
        }
        assert result["next_action"] == "首选候选已通过市场闸门，可进入 AI 研报复核"
        assert result["action_plan"]["quality_gate"]["blocked_count"] == 1
        assert result["action_plan"]["watch_candidates"][0]["code"] == "000013"

    def test_screen_stocks_enriches_watch_candidates_from_candidate_metadata(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [],
                {},
                {
                    "metrics": {},
                    "triggers": {"launchpad": [("000007", 8.0)]},
                    "candidate_entries": [
                        {
                            "code": "000007",
                            "entry_type": "launchpad",
                            "state": "Markup",
                            "score": 80.0,
                        }
                    ],
                    "name_map": {"000007": "启动平台"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()
        candidate = result["top_candidates"][0]
        primary_pick = result["selection_brief"]["primary_pick"]
        watch_candidate = result["action_plan"]["watch_candidates"][0]

        assert candidate["track"] == "Trend"
        assert candidate["stage"] == "Markup"
        assert candidate["selection_source"] == "alpha_candidate"
        assert candidate["candidate_lane"] == "launchpad"
        assert candidate["entry_type"] == "launchpad"
        assert candidate["action_status"] == "watch_only"
        assert candidate["next_step"] == "观察池跟踪，暂不进入本轮AI复核"
        assert "趋势线 / 主升阶段 / 启动平台 / 候选车道" in primary_pick["why"]
        assert primary_pick["candidate_lane"] == "launchpad"
        assert watch_candidate["candidate_lane"] == "launchpad"

    def test_screen_stocks_surfaces_theme_context_and_event_attribution(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [{"code": "000012", "name": "事件机器人", "priority_rank": 1, "priority_score": 13.2}],
                {},
                {
                    "metrics": {
                        "total_symbols": 100,
                        "fetch_ok": 100,
                        "fetch_fail": 0,
                        "theme_activity_summary": "机器人 0.76/活跃",
                        "ths_hot_events_summary": "机器人 0.82/爆发",
                        "theme_radar": {"themes": [{"theme": "人形机器人", "score": 0.78, "state": "confirmed"}]},
                        "theme_radar_source": "current",
                        "theme_lines": ["机器人", "灵巧手"],
                    },
                    "triggers": {"event_reversal": [("000012", 8.8)]},
                    "trade_mode": {
                        "mode": "risk_on",
                        "action": "允许新增买入",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                    "mainline_candidates": [
                        {
                            "code": "000012",
                            "theme": "机器人",
                            "theme_score": 0.72,
                            "theme_source": "ths_hot_event",
                            "theme_event_id": "evt-robot",
                            "theme_event_reason": "灵巧手",
                            "entry_type": "事件主题低位修复",
                            "status": "事件主题修复候选",
                        }
                    ],
                    "name_map": {"000012": "事件机器人"},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        ctx = ToolContext()

        result = screen_tools.screen_stocks(tool_context=ctx)
        candidate = result["top_candidates"][0]
        primary_pick = result["selection_brief"]["primary_pick"]

        assert result["theme_context"] == {
            "today_activity": "机器人 0.76/活跃",
            "event_mainlines": "机器人 0.82/爆发",
            "theme_radar": "人形机器人 0.78/confirmed",
            "theme_radar_source": "current",
            "hot_concepts": ["机器人", "灵巧手"],
        }
        assert candidate["strategic_theme"] == "机器人"
        assert candidate["theme_source"] == "ths_hot_event"
        assert candidate["theme_event_id"] == "evt-robot"
        assert "事件主线:机器人" in primary_pick["quality_factors"]
        assert ctx.state["last_screen_result"]["theme_context"]["event_mainlines"] == "机器人 0.82/爆发"

    def test_screen_stocks_exposes_ready_ai_review_targets(self, monkeypatch):
        from agents import screen_tools
        from agents.tool_context import ToolContext

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000004",
                        "name": "主线候选",
                        "priority_rank": 1,
                        "priority_score": 11.0,
                    },
                    {
                        "code": "000005",
                        "name": "二号候选",
                        "priority_rank": 2,
                        "priority_score": 8.0,
                    },
                ],
                {},
                {
                    "metrics": {"total_symbols": 100, "fetch_ok": 100, "fetch_fail": 0},
                    "triggers": {},
                    "trade_mode": {
                        "mode": "risk_on",
                        "action": "允许新增买入",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                    "strategy_policy": {
                        "dynamic_mode": "shadow",
                        "signal_weights": {"lps": 0.5},
                        "attribution_signal_weights": {"lps": 0.5},
                        "selection_action_count": 1,
                        "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
                        "formal_dynamic_allowed": False,
                        "policy_weight_active_scope": "尾盘+漏斗shadow",
                        "execution_policy": "shadow",
                        "next_action": "manual_review_dynamic_on",
                    },
                    "name_map": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)
        ctx = ToolContext()

        result = screen_tools.screen_stocks(tool_context=ctx)

        assert result["action_plan"]["review_targets"] == {
            "codes": ["000004", "000005"],
            "status": "ready",
            "reason": "候选已可进入 AI 研报复核",
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["000004", "000005"]},
        }
        assert result["selection_brief"]["status"] == "ready_for_ai_review"
        assert result["selection_brief"]["best_codes"] == ["000004", "000005"]
        assert result["selection_brief"]["tool_handoff"] == {
            "tool": "generate_ai_report",
            "args": {"stock_codes": ["000004", "000005"]},
            "reason": "首选候选已通过市场闸门，可进入 AI 研报复核",
        }
        assert result["summary"]["watch_candidates"] == 0
        assert result["next_tool"] == result["selection_brief"]["tool_handoff"]
        assert result["next_action"] == "首选候选已通过市场闸门，可进入 AI 研报复核"
        assert result["strategy_policy"]["selection_action_count"] == 1
        assert result["strategy_policy"]["execution_policy"] == "shadow"
        assert result["strategy_policy"]["execution_policy_label"] == "shadow 对照(shadow)"
        assert result["strategy_policy"]["next_action"] == "manual_review_dynamic_on"
        assert result["strategy_policy"]["next_action_label"] == "进入人工晋级评审（非正式生效）"
        assert "candidate_lane=trend_pullback" in result["strategy_policy"]["selection_action_summary"]
        assert result["diagnosis_targets"][0]["tool"] == "analyze_stock"
        assert result["diagnosis_targets"][0]["args"] == {"code": "000004", "mode": "diagnose"}
        assert ctx.state["last_screen_result"]["symbols_for_report"][0]["code"] == "000004"
        assert ctx.state["last_screen_result"]["selection_brief"]["best_codes"] == ["000004", "000005"]
        assert ctx.state["last_screen_result"]["strategy_policy"]["policy_weight_active_scope"] == "尾盘+漏斗shadow"
        assert ctx.state["last_screen_result"]["strategy_policy"]["execution_policy_label"] == "shadow 对照(shadow)"
        assert (
            ctx.state["last_screen_result"]["strategy_policy"]["next_action_label"] == "进入人工晋级评审（非正式生效）"
        )
        assert ctx.state["last_screen_result"]["next_tool"] == result["next_tool"]
        assert "trigger_groups" not in ctx.state["last_screen_result"]

    def test_screen_stocks_blocks_ai_review_on_degraded_data_quality(self, monkeypatch):
        from agents import screen_tools

        fake_pipeline = ModuleType("workflows.wyckoff_funnel")

        def fake_run_funnel(*_args, **_kwargs):
            return (
                True,
                [
                    {
                        "code": "000004",
                        "name": "主线候选",
                        "priority_rank": 1,
                        "priority_score": 11.0,
                    }
                ],
                {},
                {
                    "metrics": {
                        "total_symbols": 100,
                        "fetch_ok": 80,
                        "fetch_fail": 20,
                    },
                    "triggers": {},
                    "trade_mode": {
                        "mode": "risk_on",
                        "action": "允许新增买入",
                        "allow_ai_review": True,
                        "allow_recommendation_write": True,
                    },
                    "name_map": {},
                },
            )

        fake_pipeline.run = fake_run_funnel
        monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)
        monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda tool_context: None)

        result = screen_tools.screen_stocks()

        assert result["selection_brief"]["status"] == "blocked_by_data_quality"
        assert result["selection_brief"]["headline"] == "本轮有候选，但数据质量未过关: 000004 主线候选"
        assert "tool_handoff" not in result["selection_brief"]
        assert result["selection_brief"]["primary_pick"]["action_status"] == "blocked_by_data_quality"
        assert result["selection_brief"]["primary_pick"]["next_step"] == "数据质量不足，先重跑或缩小扫描范围"
        assert result["action_plan"]["ai_review_allowed"] is False
        assert result["action_plan"]["new_buy_allowed"] is False
        assert result["action_plan"]["review_targets"] == {
            "codes": ["000004"],
            "status": "blocked_by_data_quality",
            "reason": "不要直接据此选股，先重跑或缩小扫描范围；20只股票拉取失败；数据覆盖率 80.0%",
        }
        assert result["action_plan"]["data_quality_gate"]["status"] == "degraded"
        assert result["action_plan"]["report_candidates"][0]["action_status"] == "blocked_by_data_quality"
