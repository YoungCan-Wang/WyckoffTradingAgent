"""ConversationSession intents, lifecycle, and Runtime TurnOutcome."""

from __future__ import annotations

from cli.conversation import (
    ConversationSession,
    FailureKind,
    QueuedInput,
    TurnCheckpoint,
    TurnPhase,
    UserIntent,
)
from cli.conversation.failures import classify_failure
from cli.conversation.intents import has_explicit_workflow_ref, resolve_user_intent
from cli.runtime import AgentCancelled, AgentRuntime
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry


def test_intent_priority_matrix():
    failed = ConversationSession()
    failed.begin_turn("我当前的持仓满足宇树科技打新的条件么")
    failed.on_turn_failed(RuntimeError("ResourceExhausted"))
    resume = failed.resolve_text("继续", resumable_workflow_exists=True)
    assert resume.kind == UserIntent.RESUME_TURN
    assert "宇树科技" in resume.resume_user_text

    explicit = failed.resolve_text("继续 workflow wf_15acf7a34f9f", resumable_workflow_exists=True)
    assert explicit.kind == UserIntent.SUBMIT_NEW_TURN
    assert has_explicit_workflow_ref(explicit.text)

    assert (
        resolve_user_intent(
            "接着刚才那个",
            phase=TurnPhase.IDLE,
            has_pending_question=False,
            has_pending_workflow=False,
            resumable_workflow_exists=True,
        ).kind
        == UserIntent.RESUME_WORKFLOW
    )
    assert (
        resolve_user_intent(
            "继续",
            phase=TurnPhase.IDLE,
            has_pending_question=False,
            has_pending_workflow=True,
            resumable_workflow_exists=True,
            resumable_user_text="old failed",
        ).kind
        == UserIntent.PENDING_WORKFLOW_REPLY
    )

    running = ConversationSession()
    running.begin_turn("q1")
    running.mark_running()
    assert running.resolve_text("第二个问题").kind == UserIntent.ENQUEUE_INPUT
    assert failed.resolve_text("新的问题是什么").kind == UserIntent.SUBMIT_NEW_TURN

    notify = ConversationSession()
    notify.begin_turn("后台结果", system_notification=True)
    notify.on_turn_failed(RuntimeError("x"))
    assert notify.resumable_user_text == ""


def test_session_lifecycle_resume_queue_and_failures():
    cancelling = ConversationSession()
    cancelling.begin_turn("hello")
    cancelling.mark_running()
    cancel_events = cancelling.request_cancel()
    assert cancelling.phase == TurnPhase.CANCELLING
    assert any(event.type == "spinner_stop" for event in cancel_events)

    failed = ConversationSession()
    turn = failed.begin_turn("分析宁德时代")
    turn.tool_result_briefs.append({"name": "portfolio", "brief": "3 positions"})
    fail_events = failed.on_turn_failed(
        RuntimeError("ResourceExhausted: 429"),
        messages_checkpoint=[{"role": "user", "content": "分析宁德时代"}],
    )
    assert failed.phase == TurnPhase.FAILED
    assert any(event.type == "resume_hint" for event in fail_events)
    soft = failed.build_resume_user_text()
    assert "<turn-resume-context>" in soft and "portfolio" in soft
    failed.active_turn.completed_tool_ids.append("call_1")
    checkpoint = failed.take_hard_checkpoint()
    assert checkpoint is not None
    assert checkpoint.completed_tool_call_ids == ["call_1"]
    assert checkpoint.messages[0]["content"] == "分析宁德时代"

    done = ConversationSession()
    done.begin_turn("ok")
    done.on_turn_completed()
    assert done.active_turn is None
    assert done.phase == TurnPhase.IDLE

    queue = ConversationSession()
    queue.enqueue("a")
    queue.enqueue(QueuedInput(kind="system_notification", content="bg"))
    assert queue.dequeue_next().content == "a"
    assert queue.dequeue_next().kind == "system_notification"

    assert classify_failure(RuntimeError("ResourceExhausted 429")).kind == FailureKind.RATE_LIMIT
    assert classify_failure(TimeoutError("timed out")).kind == FailureKind.TIMEOUT
    assert classify_failure(ConnectionError("connection reset")).kind == FailureKind.NETWORK


def test_mark_running_preserves_cancelling():
    session = ConversationSession()
    session.begin_turn("hi")
    session.mark_running()
    session.request_cancel()
    session.mark_running()
    assert session.phase == TurnPhase.CANCELLING


def test_runtime_turn_cancelled_and_hard_resume_skip():
    def cancelled_round(_messages, _tools, _system_prompt):
        raise AgentCancelled()

    cancelled_events = list(
        AgentRuntime(ScriptedProvider(rounds=[cancelled_round]), StubToolRegistry()).run_stream(
            [{"role": "user", "content": "hi"}]
        )
    )
    assert cancelled_events[-1]["type"] == "turn_cancelled"
    assert not any(event.get("type") == "done" for event in cancelled_events)

    executed: list[str] = []

    class TrackingTools(StubToolRegistry):
        def execute(self, name, args, messages=None):
            executed.append(name)
            return {"ok": True}

    # serial path: portfolio safe, screen_stocks not in default concurrency set
    serial_events = list(
        AgentRuntime(
            ScriptedProvider(
                rounds=[
                    [
                        {
                            "type": "tool_calls",
                            "tool_calls": [
                                {"id": "call_done", "name": "portfolio", "args": {}},
                                {"id": "call_new", "name": "screen_stocks", "args": {}},
                            ],
                            "text": "",
                        }
                    ],
                    [{"type": "text_delta", "text": "ok"}],
                ]
            ),
            TrackingTools(),
        ).run_stream(
            [{"role": "user", "content": "hi"}],
            resume_from=TurnCheckpoint(
                messages=[{"role": "user", "content": "hi"}],
                completed_tool_call_ids=["call_done"],
            ),
        )
    )
    assert executed == ["screen_stocks"]
    assert any(event.get("type") == "done" for event in serial_events)

    # concurrent path: both tools concurrency-safe
    executed.clear()
    concurrent_tools = TrackingTools(
        concurrency_safe_tools={"portfolio", "analyze_stock"},
        schemas=[
            {"name": "portfolio", "description": "", "parameters": {"type": "object", "properties": {}}},
            {"name": "analyze_stock", "description": "", "parameters": {"type": "object", "properties": {}}},
        ],
    )
    concurrent_events = list(
        AgentRuntime(
            ScriptedProvider(
                rounds=[
                    [
                        {
                            "type": "tool_calls",
                            "tool_calls": [
                                {"id": "call_done", "name": "portfolio", "args": {}},
                                {"id": "call_new", "name": "analyze_stock", "args": {}},
                            ],
                            "text": "",
                        }
                    ],
                    [{"type": "text_delta", "text": "ok"}],
                ]
            ),
            concurrent_tools,
        ).run_stream(
            [{"role": "user", "content": "hi"}],
            resume_from=TurnCheckpoint(
                messages=[{"role": "user", "content": "hi"}],
                completed_tool_call_ids=["call_done"],
            ),
        )
    )
    assert executed == ["analyze_stock"]
    assert any(event.get("type") == "done" for event in concurrent_events)
