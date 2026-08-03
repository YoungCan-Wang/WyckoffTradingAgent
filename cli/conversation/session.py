"""ConversationSession: turn lifecycle controller for CLI chat."""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from cli.conversation.events import SessionUiEvent
from cli.conversation.failures import classify_failure
from cli.conversation.intents import ResolvedIntent, resolve_user_intent
from cli.conversation.state import (
    RESUMABLE_PHASES,
    RUNNING_PHASES,
    ActiveTurn,
    FailureInfo,
    TurnCheckpoint,
    TurnPhase,
)


@dataclass
class QueuedInput:
    kind: str  # user | system_notification | schedule
    content: str
    meta: dict[str, Any] | None = None


class ConversationSession:
    """Owns ActiveTurn phase transitions and input queue (ViewModel-like)."""

    def __init__(self) -> None:
        self.active_turn: ActiveTurn | None = None
        self.input_queue: deque[QueuedInput] = deque()

    @property
    def phase(self) -> TurnPhase:
        return self.active_turn.phase if self.active_turn else TurnPhase.IDLE

    @property
    def is_busy(self) -> bool:
        return self.phase in RUNNING_PHASES

    @property
    def resumable_user_text(self) -> str:
        turn = self.active_turn
        if not turn or turn.phase not in RESUMABLE_PHASES or turn.system_notification:
            return ""
        return turn.user_text.strip()

    def resolve_text(
        self,
        text: str,
        *,
        has_pending_question: bool = False,
        has_pending_workflow: bool = False,
        resumable_workflow_exists: bool = False,
    ) -> ResolvedIntent:
        return resolve_user_intent(
            text,
            phase=self.phase,
            has_pending_question=has_pending_question,
            has_pending_workflow=has_pending_workflow,
            resumable_workflow_exists=resumable_workflow_exists,
            resumable_user_text=self.resumable_user_text,
        )

    def begin_turn(self, user_text: str, *, system_notification: bool = False) -> ActiveTurn:
        self.active_turn = ActiveTurn(
            turn_id=uuid.uuid4().hex[:12],
            user_text=user_text,
            phase=TurnPhase.SUBMITTED,
            system_notification=system_notification,
        )
        return self.active_turn

    def mark_running(self) -> list[SessionUiEvent]:
        if self.phase == TurnPhase.CANCELLING:
            return []
        return self._set_phase(TurnPhase.RUNNING)

    def request_cancel(self) -> list[SessionUiEvent]:
        if not self.is_busy:
            return []
        events = self._set_phase(TurnPhase.CANCELLING)
        events.append(SessionUiEvent.spinner_stop())
        events.append(SessionUiEvent.status_refresh())
        return events

    def on_runtime_event(self, event: dict[str, Any]) -> list[SessionUiEvent]:
        etype = str(event.get("type") or "")
        if etype in {"model_start", "text_delta", "thinking_delta"}:
            return self._set_phase(TurnPhase.STREAMING)
        if etype == "tool_start":
            return self._set_phase(TurnPhase.TOOL_RUNNING)
        if etype in {"tool_result", "tool_error"}:
            self._record_tool_result(event)
            return self._set_phase(TurnPhase.TOOL_RUNNING)
        if etype == "ask_user":
            return self._set_phase(TurnPhase.AWAITING_USER)
        if etype == "done":
            return self.on_turn_completed()
        if etype == "turn_cancelled":
            return self.on_turn_cancelled()
        if etype == "turn_failed":
            failure = (
                _failure_from_payload(event.get("failure"))
                if event.get("failure")
                else classify_failure(RuntimeError(str(event.get("message") or "turn failed")))
            )
            return self.on_turn_failed(failure)
        return []

    def on_turn_completed(self) -> list[SessionUiEvent]:
        events = self._set_phase(TurnPhase.COMPLETED)
        events.append(SessionUiEvent.spinner_stop())
        events.append(SessionUiEvent.status_refresh())
        self.active_turn = None
        return events

    def on_turn_cancelled(
        self,
        *,
        messages_checkpoint: list[dict[str, Any]] | None = None,
    ) -> list[SessionUiEvent]:
        turn = self.active_turn
        if turn:
            if messages_checkpoint is not None:
                turn.messages_checkpoint = [dict(item) for item in messages_checkpoint]
            turn.failure = None
        events = self._set_phase(TurnPhase.CANCELLED)
        events.append(SessionUiEvent.spinner_stop())
        events.append(SessionUiEvent.resume_hint(cancelled=True))
        events.append(SessionUiEvent.status_refresh())
        return events

    def on_turn_failed(
        self,
        failure: FailureInfo | BaseException,
        *,
        messages_checkpoint: list[dict[str, Any]] | None = None,
    ) -> list[SessionUiEvent]:
        info = failure if isinstance(failure, FailureInfo) else classify_failure(failure)
        turn = self.active_turn
        if turn:
            turn.failure = info
            if messages_checkpoint is not None:
                turn.messages_checkpoint = [dict(item) for item in messages_checkpoint]
        events = self._set_phase(TurnPhase.FAILED)
        events.append(SessionUiEvent.spinner_stop())
        events.append(SessionUiEvent.resume_hint(cancelled=False))
        events.append(SessionUiEvent.status_refresh())
        return events

    def abandon_active_turn(self) -> None:
        self.active_turn = None

    def enqueue(self, item: QueuedInput | str, *, kind: str = "user") -> list[SessionUiEvent]:
        queued = item if isinstance(item, QueuedInput) else QueuedInput(kind=kind, content=str(item))
        self.input_queue.append(queued)
        return [SessionUiEvent.queued(depth=len(self.input_queue)), SessionUiEvent.status_refresh()]

    def dequeue_next(self) -> QueuedInput | None:
        if not self.input_queue:
            return None
        return self.input_queue.popleft()

    def clear_queue(self) -> None:
        self.input_queue.clear()

    def build_resume_user_text(self) -> str:
        """Soft checkpoint: inject completed tool briefs into resume prompt."""

        turn = self.active_turn
        if not turn or not turn.user_text.strip():
            return ""
        if not turn.tool_result_briefs and not turn.failure:
            return turn.user_text
        lines = ["<turn-resume-context>", f"原问题: {turn.user_text}"]
        if turn.failure:
            lines.append(f"失败原因: {turn.failure.kind.value}: {turn.failure.message}")
        if turn.tool_result_briefs:
            lines.append("已完成工具摘要:")
            for brief in turn.tool_result_briefs[:12]:
                name = brief.get("name") or brief.get("tool") or "tool"
                summary = brief.get("brief") or brief.get("summary") or ""
                lines.append(f"- {name}: {summary}")
        lines.extend(
            [
                "请基于以上进度继续；不要重复已成功完成的只读工具，写操作需重新确认。",
                "</turn-resume-context>",
                "",
                turn.user_text,
            ]
        )
        return "\n".join(lines)

    def take_hard_checkpoint(self) -> TurnCheckpoint | None:
        turn = self.active_turn
        if not turn or turn.phase not in RESUMABLE_PHASES:
            return None
        return turn.to_checkpoint()

    def status_label(self) -> str:
        labels = {
            TurnPhase.IDLE: "",
            TurnPhase.SUBMITTED: "submitted",
            TurnPhase.RUNNING: "running",
            TurnPhase.STREAMING: "streaming",
            TurnPhase.TOOL_RUNNING: "tools",
            TurnPhase.AWAITING_USER: "awaiting",
            TurnPhase.CANCELLING: "cancelling",
            TurnPhase.COMPLETED: "",
            TurnPhase.FAILED: "failed",
            TurnPhase.CANCELLED: "cancelled",
        }
        return labels.get(self.phase, "")

    def _record_tool_result(self, event: dict[str, Any]) -> None:
        turn = self.active_turn
        if not turn:
            return
        call_id = str(event.get("tool_call_id") or event.get("step") or "")
        if call_id and call_id not in turn.completed_tool_ids:
            turn.completed_tool_ids.append(call_id)
        name = str(event.get("name") or event.get("tool") or "")
        brief_lines = event.get("brief_lines")
        if isinstance(brief_lines, list) and brief_lines:
            brief = "; ".join(str(line) for line in brief_lines[:3])
        else:
            result = event.get("result")
            brief = _brief_from_result(result)
        if name or brief:
            turn.tool_result_briefs.append({"name": name, "brief": brief, "tool_call_id": call_id})

    def _set_phase(self, phase: TurnPhase) -> list[SessionUiEvent]:
        previous = self.phase
        if self.active_turn is None and phase not in {TurnPhase.IDLE, TurnPhase.COMPLETED}:
            return []
        if self.active_turn is not None:
            if self.active_turn.phase == phase:
                return []
            self.active_turn.phase = phase
        elif phase == TurnPhase.IDLE:
            return [SessionUiEvent.phase_changed(phase, previous=previous)]
        return [SessionUiEvent.phase_changed(phase, previous=previous), SessionUiEvent.status_refresh()]


def _failure_from_payload(payload: Any) -> FailureInfo:
    from cli.conversation.state import FailureKind

    data = payload if isinstance(payload, dict) else {}
    kind_raw = str(data.get("kind") or FailureKind.UNKNOWN.value)
    try:
        kind = FailureKind(kind_raw)
    except ValueError:
        kind = FailureKind.UNKNOWN
    return FailureInfo(
        kind=kind,
        message=str(data.get("message") or "")[:500],
        exception_type=str(data.get("exception_type") or ""),
    )


def _brief_from_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        if err := result.get("error"):
            return f"error: {err}"[:200]
        for key in ("summary", "message", "status"):
            if value := result.get(key):
                return str(value)[:200]
        return f"keys={sorted(result.keys())[:8]}"[:200]
    return str(result)[:200]
