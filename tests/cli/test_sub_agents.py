from __future__ import annotations

import time
from copy import deepcopy

from cli.sub_agents import (
    ANALYSIS_AGENT,
    RESEARCH_AGENT,
    TRADING_AGENT,
    WORKFLOW_TASK_AGENT,
    SubAgentToolProxy,
    _delegate_result_policy,
    delegate_to_analysis,
    run_sub_agent,
)
from cli.tools import TOOL_SCHEMAS
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry

# ---------------------------------------------------------------------------
# SubAgentToolProxy 过滤测试
# ---------------------------------------------------------------------------


class TestSubAgentToolProxy:
    def test_schemas_only_returns_allowed(self):
        registry = StubToolRegistry(schemas=deepcopy(TOOL_SCHEMAS))
        allowed = {"analyze_stock", "portfolio"}
        proxy = SubAgentToolProxy(registry, allowed)

        names = {s["name"] for s in proxy.schemas()}
        assert names == allowed

    def test_execute_allowed_tool(self):
        registry = StubToolRegistry(tool_results={"analyze_stock": {"health": "OK"}})
        proxy = SubAgentToolProxy(registry, {"analyze_stock"})

        result = proxy.execute("analyze_stock", {"code": "000001"})
        assert result == {"health": "OK"}
        assert registry.calls[0]["name"] == "analyze_stock"

    def test_execute_blocked_tool_returns_error(self):
        registry = StubToolRegistry()
        proxy = SubAgentToolProxy(registry, {"analyze_stock"})

        result = proxy.execute("update_portfolio", {"action": "add"})
        assert "error" in result
        assert "无权" in result["error"]
        assert len(registry.calls) == 0

    def test_concurrency_metadata_respects_allowed_tools(self):
        registry = StubToolRegistry(concurrency_safe_tools={"analyze_stock", "portfolio"})
        proxy = SubAgentToolProxy(registry, {"analyze_stock"})

        assert proxy.concurrency_safe("analyze_stock")
        assert not proxy.concurrency_safe("portfolio")

    def test_execute_tool_timeout_returns_error(self):
        def slow_tool(_name, _args):
            time.sleep(0.2)
            return {"ok": True}

        registry = StubToolRegistry(tool_results={"analyze_stock": slow_tool})
        proxy = SubAgentToolProxy(
            registry,
            {"analyze_stock"},
            tool_timeout_seconds=1,
            deadline=time.monotonic() + 0.01,
        )

        result = proxy.execute("analyze_stock", {"code": "000001"})
        assert "error" in result
        assert "工具调用超时" in result["error"]


# ---------------------------------------------------------------------------
# SubAgent 定义一致性
# ---------------------------------------------------------------------------


def test_agent_tool_names_exist_in_schemas():
    schema_names = {s["name"] for s in TOOL_SCHEMAS}
    for agent in (RESEARCH_AGENT, ANALYSIS_AGENT, TRADING_AGENT):
        missing = set(agent.tool_names) - schema_names
        assert not missing, f"{agent.name} references unknown tools: {missing}"


def test_trading_agent_does_not_execute_portfolio_updates():
    assert "update_portfolio" not in TRADING_AGENT.tool_names


# ---------------------------------------------------------------------------
# run_sub_agent 集成测试
# ---------------------------------------------------------------------------


def test_run_sub_agent_basic():
    provider = ScriptedProvider(
        [
            [
                {"type": "text_delta", "text": "大盘水温偏暖，上证涨 0.5%。"},
                {"type": "usage", "input_tokens": 50, "output_tokens": 15},
            ],
        ]
    )
    registry = StubToolRegistry()

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="查看大盘水温",
        context="",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "research"
    assert result["status"] == "completed"
    assert "大盘水温偏暖" in result["result"]
    assert result["usage"]["output_tokens"] == 15
    assert result["rounds"] == 1
    assert result["tool_calls"] == []
    assert not result["context_truncated"]
    assert not result["result_truncated"]
    assert result["policy"]["next_action"] == "use_result"
    assert result["policy"]["fallback_tools"] == []


def test_run_sub_agent_does_not_enforce_top_level_screening_expectation():
    provider = ScriptedProvider([[{"type": "text_delta", "text": "扫描完成，候选 A。"}]])
    registry = StubToolRegistry(tool_results={"screen_stocks": {"symbols_for_report": ["000001"]}})

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="先扫描候选",
        context="phase=review",
        provider=provider,
        registry=registry,
    )

    assert result["status"] == "completed"
    assert result["result"] == "扫描完成，候选 A。"
    assert result["tool_calls"] == []
    assert registry.calls == []


def test_workflow_task_agent_continues_attack_plan_after_screen():
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选已出，风险边界我先口头整理。"}],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_strategy", "name": "generate_strategy_decision", "args": {}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "攻防计划已基于工具结果生成。"}],
        ]
    )
    registry = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={
            "screen_stocks": {"symbols_for_report": ["300750"]},
            "generate_strategy_decision": {"status": "skipped_notify_unconfigured", "reviewed_codes": ["300750"]},
        },
    )

    result = run_sub_agent(
        WORKFLOW_TASK_AGENT,
        task="给我找几只值得复核的票，带理由和风险边界",
        context="",
        provider=provider,
        registry=registry,
        enforce_turn_expectations=True,
    )

    assert result["status"] == "completed"
    assert result["tool_calls"] == ["screen_stocks", "generate_strategy_decision"]
    assert result["result"] == "攻防计划已基于工具结果生成。"


def test_workflow_task_agent_does_not_require_scoped_out_strategy_tool():
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {"board": "all"}}],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "候选已出，等待后续攻防任务。"}],
        ]
    )
    registry = StubToolRegistry(
        schemas=deepcopy(TOOL_SCHEMAS),
        tool_results={"screen_stocks": {"symbols_for_report": ["300750"]}},
    )

    result = run_sub_agent(
        WORKFLOW_TASK_AGENT,
        task="给我找几只值得复核的票，带理由和风险边界",
        context="",
        provider=provider,
        registry=registry,
        tool_names=("screen_stocks",),
        enforce_turn_expectations=True,
    )

    assert result["status"] == "completed"
    assert result["tool_calls"] == ["screen_stocks"]
    assert result["result"] == "候选已出，等待后续攻防任务。"


def test_run_sub_agent_with_tool_call():
    provider = ScriptedProvider(
        [
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc1", "name": "get_market_overview", "args": {}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 30, "output_tokens": 5},
            ],
            [
                {"type": "text_delta", "text": "上证指数涨 0.3%，市场偏暖。"},
                {"type": "usage", "input_tokens": 60, "output_tokens": 12},
            ],
        ]
    )
    registry = StubToolRegistry(tool_results={"get_market_overview": {"sh": "+0.3%", "sz": "+0.1%"}})

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="查看大盘水温",
        context="用户想了解市场环境",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "research"
    assert result["status"] == "completed"
    assert "上证" in result["result"]
    assert result["tool_calls"] == ["get_market_overview"]
    assert registry.calls[0]["name"] == "get_market_overview"


def test_run_sub_agent_captures_background_task_ids():
    provider = ScriptedProvider(
        [
            [{"type": "tool_calls", "tool_calls": [{"id": "tc_screen", "name": "screen_stocks", "args": {}}]}],
            [{"type": "text_delta", "text": "筛选已提交后台。"}],
        ]
    )
    registry = StubToolRegistry(tool_results={"screen_stocks": {"status": "background", "task_id": "bg_screen"}})

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="扫描候选",
        context="phase=scan",
        provider=provider,
        registry=registry,
    )

    assert result["status"] == "completed"
    assert result["tool_calls"] == ["screen_stocks"]
    assert result["background_task_ids"] == ["bg_screen"]


def test_run_sub_agent_trims_large_context():
    provider = ScriptedProvider([[{"type": "text_delta", "text": "收到"}]])
    registry = StubToolRegistry()
    small_context_agent = deepcopy(RESEARCH_AGENT)
    object.__setattr__(small_context_agent, "context_budget_tokens", 80)
    context = "最早唯一材料" + "早期材料" * 500 + "最新关键材料"

    result = run_sub_agent(
        small_context_agent,
        task="整理材料",
        context=context,
        provider=provider,
        registry=registry,
    )

    sent = provider.calls[0]["messages"][0]["content"]
    assert result["context_truncated"]
    assert "上下文已按预算裁剪" in sent
    assert "最新关键材料" in sent
    assert "最早唯一材料" not in sent


def test_run_sub_agent_trims_large_result():
    provider = ScriptedProvider([[{"type": "text_delta", "text": "A" * 200}]])
    registry = StubToolRegistry()
    small_result_agent = deepcopy(RESEARCH_AGENT)
    object.__setattr__(small_result_agent, "result_budget_chars", 80)

    result = run_sub_agent(
        small_result_agent,
        task="输出摘要",
        context="",
        provider=provider,
        registry=registry,
    )

    assert result["status"] == "completed"
    assert result["result_truncated"]
    assert len(result["result"]) <= 80
    assert "结果已按输出预算截断" in result["result"]


def test_run_sub_agent_cancelled():
    provider = ScriptedProvider(
        [
            [
                {"type": "text_delta", "text": "开始分析"},
                {"type": "usage", "input_tokens": 10, "output_tokens": 2},
            ],
        ]
    )
    registry = StubToolRegistry()

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="查看大盘水温",
        context="",
        provider=provider,
        registry=registry,
        cancel_check=lambda: True,
    )

    assert result["agent"] == "research"
    assert result["status"] == "cancelled"
    assert result["result"] == ""
    assert "cancelled" in result["error"]
    assert result["policy"]["next_action"] == "stop_and_report_cancelled"
    assert result["policy"]["retryable"] is False


def test_run_sub_agent_timeout():
    def slow_round(_messages, _tools, _system_prompt):
        time.sleep(1.2)
        return [{"type": "text_delta", "text": "迟到的分析"}]

    provider = ScriptedProvider([slow_round])
    registry = StubToolRegistry()
    expired_agent = deepcopy(RESEARCH_AGENT)
    object.__setattr__(expired_agent, "timeout_seconds", 1)

    result = run_sub_agent(
        expired_agent,
        task="查看大盘水温",
        context="",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "research"
    assert result["status"] == "timeout"
    assert "timeout" in result["error"]
    assert result["policy"]["next_action"] == "fallback_to_direct_tools"
    assert result["policy"]["retryable"] is True
    assert "get_market_overview" in result["policy"]["fallback_tools"]


def test_run_sub_agent_error_policy():
    def failed_round(_messages, _tools, _system_prompt):
        raise RuntimeError("provider failed")

    provider = ScriptedProvider([failed_round])
    registry = StubToolRegistry()

    result = run_sub_agent(
        ANALYSIS_AGENT,
        task="诊断持仓",
        context="",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "analysis"
    assert result["status"] == "error"
    assert "provider failed" in result["error"]
    assert result["policy"]["next_action"] == "fallback_to_direct_tools"
    assert "analyze_stock" in result["policy"]["fallback_tools"]


def test_empty_policy_uses_fallback_tools():
    policy = _delegate_result_policy(TRADING_AGENT, "empty")

    assert policy["next_action"] == "fallback_to_direct_tools"
    assert policy["retryable"] is False
    assert "generate_strategy_decision" in policy["fallback_tools"]


def test_delegate_start_error_has_policy():
    result = delegate_to_analysis("诊断持仓")

    assert result["agent"] == "analysis"
    assert result["status"] == "error"
    assert "无法启动" in result["error"]
    assert result["policy"]["next_action"] == "fallback_to_direct_tools"
