"""Conversation turn state machine for CLI/TUI."""

from cli.conversation.events import SessionUiEvent
from cli.conversation.failures import classify_failure
from cli.conversation.intents import ResolvedIntent, UserIntent, has_explicit_workflow_ref, is_resume_phrase
from cli.conversation.session import ConversationSession, QueuedInput
from cli.conversation.state import (
    RESUMABLE_PHASES,
    RUNNING_PHASES,
    ActiveTurn,
    FailureInfo,
    FailureKind,
    TurnCheckpoint,
    TurnPhase,
)

__all__ = [
    "ActiveTurn",
    "ConversationSession",
    "FailureInfo",
    "FailureKind",
    "QueuedInput",
    "RESUMABLE_PHASES",
    "RUNNING_PHASES",
    "ResolvedIntent",
    "SessionUiEvent",
    "TurnCheckpoint",
    "TurnPhase",
    "UserIntent",
    "classify_failure",
    "has_explicit_workflow_ref",
    "is_resume_phrase",
]
