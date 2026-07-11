from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.candidate_metadata import code6 as normalize_cn_code  # noqa: F401 re-export
from core.candidate_report_semantics import candidate_phase, candidate_role, candidate_theme
from utils.safe import safe_float  # noqa: F401 re-export

DECISION_BUY = "BUY"
DECISION_WATCH = "WATCH"
DECISION_SKIP = "SKIP"
VALID_DECISIONS = {DECISION_BUY, DECISION_WATCH, DECISION_SKIP}


@dataclass
class TailBuyCandidate:
    code: str
    name: str
    signal_date: str
    status: str
    signal_type: str
    signal_score: float
    market_regime: str = ""
    candidate_lane: str = ""
    entry_type: str = ""
    signal_key: str = ""
    candidate_status: str = ""
    candidate_reasons: dict[str, Any] = field(default_factory=dict)
    candidate_theme: str = ""
    candidate_phase: str = ""
    candidate_role: str = ""
    mainline_score: float | None = None
    theme_score: float | None = None
    stock_role_score: float | None = None
    snap: dict[str, Any] = field(default_factory=dict)
    rule_score: float = 0.0
    rule_decision: str = DECISION_SKIP
    rule_reasons: list[str] = field(default_factory=list)
    llm_decision: str | None = None
    llm_reason: str = ""
    llm_confidence: float | None = None
    llm_model_used: str = ""
    final_decision: str = DECISION_SKIP
    priority_score: float = 0.0
    fetch_error: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    summary_5m: str = ""

    def __post_init__(self) -> None:
        self.candidate_theme = self.candidate_theme or candidate_theme(self.candidate_reasons)
        self.candidate_phase = self.candidate_phase or candidate_phase(self.candidate_status)
        self.candidate_role = self.candidate_role or candidate_role(self.stock_role_score, self.candidate_lane)


def normalize_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text else "pending"


def normalize_regime(raw: Any) -> str:
    return str(raw or "").strip().upper()
