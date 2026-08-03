"""User-text intent arbitration for conversation turns."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cli.conversation.state import RESUMABLE_PHASES, RUNNING_PHASES, TurnPhase
from cli.workflows.resume import is_recent_workflow_followup

_EXPLICIT_WF_RE = re.compile(r"\bwf_[A-Za-z0-9_-]+\b")


class UserIntent(StrEnum):
    ANSWER_PENDING_QUESTION = "answer_pending_question"
    PENDING_WORKFLOW_REPLY = "pending_workflow_reply"
    RESUME_TURN = "resume_turn"
    RESUME_WORKFLOW = "resume_workflow"
    ENQUEUE_INPUT = "enqueue_input"
    SUBMIT_NEW_TURN = "submit_new_turn"


@dataclass(frozen=True)
class ResolvedIntent:
    kind: UserIntent
    text: str = ""
    resume_user_text: str = ""


def has_explicit_workflow_ref(text: str) -> bool:
    lowered = (text or "").lower()
    return "workflow" in lowered or "工作流" in (text or "") or bool(_EXPLICIT_WF_RE.search(text or ""))


def is_resume_phrase(text: str) -> bool:
    """Short continuation phrases that mean 'continue prior work'."""

    return is_recent_workflow_followup(text)


def resolve_user_intent(
    text: str,
    *,
    phase: TurnPhase,
    has_pending_question: bool,
    has_pending_workflow: bool,
    resumable_workflow_exists: bool,
    resumable_user_text: str = "",
) -> ResolvedIntent:
    """Single entry for chat-text routing (priority order from plan)."""

    stripped = (text or "").strip()
    if phase == TurnPhase.AWAITING_USER or (has_pending_question and phase in RUNNING_PHASES):
        return ResolvedIntent(UserIntent.ANSWER_PENDING_QUESTION, text=stripped)
    if has_pending_workflow:
        return ResolvedIntent(UserIntent.PENDING_WORKFLOW_REPLY, text=stripped)
    if (
        phase in RESUMABLE_PHASES
        and resumable_user_text
        and is_resume_phrase(stripped)
        and not has_explicit_workflow_ref(stripped)
    ):
        return ResolvedIntent(
            UserIntent.RESUME_TURN,
            text=stripped,
            resume_user_text=resumable_user_text,
        )
    if is_resume_phrase(stripped) and resumable_workflow_exists and not has_explicit_workflow_ref(stripped):
        return ResolvedIntent(UserIntent.RESUME_WORKFLOW, text=stripped)
    if phase in RUNNING_PHASES and phase != TurnPhase.AWAITING_USER:
        return ResolvedIntent(UserIntent.ENQUEUE_INPUT, text=stripped)
    return ResolvedIntent(UserIntent.SUBMIT_NEW_TURN, text=stripped)
