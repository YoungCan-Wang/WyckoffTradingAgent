"""Conversation turn state model (phase, failure, ActiveTurn)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnPhase(StrEnum):
    IDLE = "idle"
    SUBMITTED = "submitted"
    RUNNING = "running"
    STREAMING = "streaming"
    TOOL_RUNNING = "tool_running"
    AWAITING_USER = "awaiting_user"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUNNING_PHASES = frozenset(
    {
        TurnPhase.SUBMITTED,
        TurnPhase.RUNNING,
        TurnPhase.STREAMING,
        TurnPhase.TOOL_RUNNING,
        TurnPhase.AWAITING_USER,
        TurnPhase.CANCELLING,
    }
)

RESUMABLE_PHASES = frozenset({TurnPhase.FAILED, TurnPhase.CANCELLED})


class FailureKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    AUTH = "auth"
    TIMEOUT = "timeout"
    PROVIDER = "provider"
    UNKNOWN = "unknown"


@dataclass
class FailureInfo:
    kind: FailureKind
    message: str
    exception_type: str = ""


@dataclass
class TurnCheckpoint:
    """Hard mid-tool resume payload (Phase 4)."""

    messages: list[dict[str, Any]]
    completed_tool_call_ids: list[str] = field(default_factory=list)
    tool_result_briefs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ActiveTurn:
    turn_id: str
    user_text: str
    phase: TurnPhase
    failure: FailureInfo | None = None
    partial_assistant: str = ""
    completed_tool_ids: list[str] = field(default_factory=list)
    tool_result_briefs: list[dict[str, Any]] = field(default_factory=list)
    scratchpad_path: str = ""
    system_notification: bool = False
    # Phase 4 hard resume fields
    messages_checkpoint: list[dict[str, Any]] | None = None
    run_state_blob: dict[str, Any] | None = None

    def to_checkpoint(self) -> TurnCheckpoint | None:
        if self.messages_checkpoint is None:
            return None
        return TurnCheckpoint(
            messages=[dict(item) for item in self.messages_checkpoint],
            completed_tool_call_ids=list(self.completed_tool_ids),
            tool_result_briefs=[dict(item) for item in self.tool_result_briefs],
        )
