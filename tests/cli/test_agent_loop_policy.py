"""Unit tests for decide_agent_loop / prepare / steering / continuation fixes."""

from __future__ import annotations

from cli.agent_loop import CONTINUATION_PROMPT, decide_agent_loop, has_incomplete_tool_calls
from cli.conversation import ConversationSession, UserIntent
from cli.conversation.intents import is_steer_text, strip_steer_prefix
from cli.prepare_tool_call import accept, reject
from cli.runtime import AgentRuntime
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry


def test_decide_agent_loop_reasons():
    assert (
        decide_agent_loop(
            finish_reason="stop",
            step_count=1,
            max_steps=15,
            has_tool_calls=False,
        ).kind
        == "complete"
    )
    assert (
        decide_agent_loop(
            finish_reason="length",
            step_count=1,
            max_steps=15,
            has_tool_calls=False,
        ).reason
        == "output-length"
    )
    assert (
        decide_agent_loop(
            finish_reason="step_limit",
            step_count=15,
            max_steps=15,
            has_tool_calls=True,
        ).reason
        == "step-limit"
    )
    assert (
        decide_agent_loop(
            finish_reason="stop",
            step_count=2,
            max_steps=15,
            has_tool_calls=False,
            unfinished_required_work=True,
        ).reason
        == "unfinished-work"
    )
    assert (
        decide_agent_loop(
            finish_reason="stop",
            step_count=1,
            max_steps=15,
            has_tool_calls=False,
            has_incomplete_tool_call=True,
        ).kind
        == "error"
    )


def test_has_incomplete_tool_calls():
    assert has_incomplete_tool_calls([{"id": "1", "name": "", "args": {}}])
    assert not has_incomplete_tool_calls([{"id": "1", "name": "portfolio", "args": {}}])


def test_steer_intent_and_queue():
    assert is_steer_text("!改看持仓")
    assert strip_steer_prefix("!改看持仓") == "改看持仓"
    assert strip_steer_prefix("/steer 停下来看大盘") == "停下来看大盘"

    session = ConversationSession()
    session.begin_turn("筛股")
    session.mark_running()
    intent = session.resolve_text("!改看持仓")
    assert intent.kind == UserIntent.STEER_TURN
    assert intent.text == "改看持仓"
    session.enqueue_steer(intent.text)
    assert session.drain_steering() == ["改看持仓"]
    assert session.drain_steering() == []
    assert session.resolve_text("下一个问题").kind == UserIntent.ENQUEUE_INPUT
    session.enqueue_steer("残留")
    session.on_turn_completed()
    assert session.drain_steering() == []


def test_runtime_prepare_rejects_unknown_tool():
    class StrictStub(StubToolRegistry):
        def has_tool(self, name: str) -> bool:
            return any(schema.get("name") == name for schema in self._schemas)

    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc1", "name": "not_a_real_tool", "args": {}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 1, "output_tokens": 1},
            ],
            [
                {"type": "text_delta", "text": "已拦截未知工具。"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 1},
            ],
        ]
    )
    events = list(AgentRuntime(provider, StrictStub()).run_stream([{"role": "user", "content": "测一下"}]))
    assert any(e.get("type") == "tool_error" and e.get("name") == "not_a_real_tool" for e in events)
    err = next(e for e in events if e.get("type") == "tool_error")
    assert err.get("result", {}).get("code") == "tool_not_found"
    assert events[-1]["type"] == "done"


def test_runtime_auto_continue_keeps_full_answer():
    """P1: length 续写必须合并首尾段，且合成 continuation prompt 不落库。"""

    messages = [{"role": "user", "content": "写长文"}]
    provider = ScriptedProvider(
        rounds=[
            [
                {"type": "text_delta", "text": "第一部分…"},
                {"type": "finish", "reason": "length"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 8},
            ],
            [
                {"type": "text_delta", "text": "续写完成。"},
                {"type": "finish", "reason": "stop"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 4},
            ],
        ]
    )
    events = list(AgentRuntime(provider, StubToolRegistry()).run_stream(messages))
    assert any(e.get("type") == "continuation" and e.get("reason") == "output-length" for e in events)
    done = events[-1]
    assert done["type"] == "done"
    assert "第一部分" in done["text"] and "续写完成" in done["text"]
    assert not any(CONTINUATION_PROMPT in str(m.get("content") or "") for m in messages)
    assistants = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert "第一部分" in assistants[0]["content"] and "续写完成" in assistants[0]["content"]


def test_runtime_auto_continue_replays_reasoning_without_text():
    provider = ScriptedProvider(
        rounds=[
            [
                {"type": "thinking_delta", "text": "上一轮推理"},
                {"type": "finish", "reason": "length"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 8},
            ],
            [
                {"type": "text_delta", "text": "续写完成。"},
                {"type": "finish", "reason": "stop"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 4},
            ],
        ]
    )

    events = list(AgentRuntime(provider, StubToolRegistry()).run_stream([{"role": "user", "content": "写长文"}]))

    second_messages = provider.calls[1]["messages"]
    assistant = next(message for message in second_messages if message["role"] == "assistant")
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "上一轮推理"
    assert events[-1]["text"] == "续写完成。"


def test_runtime_natural_finish_at_max_rounds_no_false_continue():
    """P1: 恰好在轮次上限自然 stop 时不得误判 step-limit。"""

    messages = [{"role": "user", "content": "查持仓"}]
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc1", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 1, "output_tokens": 1},
            ],
            [
                {"type": "text_delta", "text": "这是完整的最终答案。"},
                {"type": "finish", "reason": "stop"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 2},
            ],
        ]
    )
    events = list(
        AgentRuntime(
            provider, StubToolRegistry(tool_results={"portfolio": {"positions": []}}), max_tool_rounds=2
        ).run_stream(messages)
    )
    assert not any(e.get("type") == "continuation" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "这是完整的最终答案。"


def test_runtime_steering_persists_in_history():
    """P2: 转向指令与打断前正文必须留在 messages。"""

    drained = {"n": 0}

    def steer_drain():
        drained["n"] += 1
        if drained["n"] == 2:
            return ["改看持仓，不要再筛股"]
        return []

    messages = [{"role": "user", "content": "帮我筛股"}]
    provider = ScriptedProvider(
        rounds=[
            [
                {"type": "text_delta", "text": "正在筛股…"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 2},
            ],
            [
                {"type": "text_delta", "text": "已改为查看持仓。"},
                {"type": "usage", "input_tokens": 1, "output_tokens": 2},
            ],
        ]
    )
    events = list(AgentRuntime(provider, StubToolRegistry(), steer_drain=steer_drain).run_stream(messages))
    assert any(e.get("type") == "steered" for e in events)
    assert events[-1]["text"] == "已改为查看持仓。"
    assert any(m.get("role") == "assistant" and "正在筛股" in str(m.get("content") or "") for m in messages)
    assert any(m.get("_steering") and "改看持仓" in str(m.get("content") or "") for m in messages)


def test_prepare_helpers():
    assert accept({"x": 1}).action == "accept"
    denied = reject("tool_not_found", "未知工具", args={})
    assert denied.action == "reject"
    assert denied.error_result()["code"] == "tool_not_found"


def test_runtime_cancel_pairs_unanswered_tool_results():
    """取消时为未完成 tool_call 补齐 toolResult，历史不撕。"""

    import threading
    import time

    flag = threading.Event()

    class SlowTools(StubToolRegistry):
        def execute(self, name, args, messages=None):
            self.calls.append({"name": name, "args": dict(args)})
            if name == "portfolio":
                flag.set()
                return {"ok": True}
            time.sleep(0.8)
            return {"ok": True}

    messages = [{"role": "user", "content": "并行查"}]
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_a", "name": "portfolio", "args": {}},
                        {"id": "tc_b", "name": "analyze_stock", "args": {"code": "600519"}},
                    ],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 1, "output_tokens": 1},
            ],
        ]
    )
    tools = SlowTools(
        concurrency_safe_tools={"portfolio", "analyze_stock"},
        schemas=[
            {"name": "portfolio", "description": "", "parameters": {"type": "object", "properties": {}}},
            {
                "name": "analyze_stock",
                "description": "",
                "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
            },
        ],
    )
    events = list(AgentRuntime(provider, tools, cancel_check=flag.is_set, stream_chunk_timeout=30).run_stream(messages))
    assert events[-1]["type"] == "turn_cancelled"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert {m.get("tool_call_id") for m in tool_msgs} == {"tc_a", "tc_b"}
    assert any(
        "aborted" in str(m.get("content") or "").lower() or "中止" in str(m.get("content") or "") for m in tool_msgs
    )


def test_runtime_concurrent_results_keep_call_order():
    """并发工具按原始 tool_call 顺序写回 messages。"""

    import time

    class OrderedTools(StubToolRegistry):
        def execute(self, name, args, messages=None):
            self.calls.append({"name": name, "args": dict(args)})
            if name == "analyze_stock":
                time.sleep(0.15)
                return {"slow": True}
            return {"fast": True}

    messages = [{"role": "user", "content": "并行"}]
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {"id": "tc_slow", "name": "analyze_stock", "args": {"code": "1"}},
                        {"id": "tc_fast", "name": "portfolio", "args": {}},
                    ],
                    "text": "",
                }
            ],
            [{"type": "text_delta", "text": "ok"}],
        ]
    )
    tools = OrderedTools(
        concurrency_safe_tools={"portfolio", "analyze_stock"},
        schemas=[
            {"name": "portfolio", "description": "", "parameters": {"type": "object", "properties": {}}},
            {
                "name": "analyze_stock",
                "description": "",
                "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
            },
        ],
    )
    list(AgentRuntime(provider, tools).run_stream(messages))
    tool_ids = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
    assert tool_ids == ["tc_slow", "tc_fast"]
