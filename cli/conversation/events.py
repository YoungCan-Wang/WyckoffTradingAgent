"""UI-consumable session events emitted by ConversationSession."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cli.conversation.state import TurnPhase


@dataclass(frozen=True)
class SessionUiEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def phase_changed(cls, phase: TurnPhase, *, previous: TurnPhase | None = None) -> SessionUiEvent:
        return cls(
            "phase_changed",
            {"phase": phase.value, "previous": previous.value if previous else ""},
        )

    @classmethod
    def spinner_stop(cls) -> SessionUiEvent:
        return cls("spinner_stop")

    @classmethod
    def resume_hint(cls, *, cancelled: bool = False) -> SessionUiEvent:
        return cls("resume_hint", {"cancelled": cancelled})

    @classmethod
    def queued(cls, *, depth: int) -> SessionUiEvent:
        return cls("queued", {"depth": depth})

    @classmethod
    def steered(cls, *, depth: int) -> SessionUiEvent:
        return cls("steered", {"depth": depth})

    @classmethod
    def interrupted_banner(cls, *, session_id: str, query: str) -> SessionUiEvent:
        return cls("interrupted_banner", {"session_id": session_id, "query": query})

    @classmethod
    def status_refresh(cls) -> SessionUiEvent:
        return cls("status_refresh")
