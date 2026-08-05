from dataclasses import dataclass, field

from workflows import holding_diagnosis_llm, step4_rebalancer
from workflows.step4_models import PortfolioState, PositionItem, Step4OrderConfig


@dataclass
class _Advice:
    code: str = "600611"
    name: str = "大众交通"
    action: str = "HOLD"
    shares: int = 100
    cost: float = 4.0
    current_price: float = 4.5
    pnl_pct: float = 12.5
    rule_score: float = 70.0
    reasons: list[str] = field(default_factory=lambda: ["结构未确认破坏"])
    features: dict = field(default_factory=dict)
    risk_tag: str = ""


def test_holding_prompt_injects_symbol_scoped_llm_docs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        holding_diagnosis_llm,
        "build_llm_doc_context",
        lambda scope, **kwargs: calls.append((scope, kwargs["symbols"])) or "DOC-CONTEXT",
    )

    prompt = holding_diagnosis_llm._build_holding_llm_prompt(_Advice(), 10_000, 100_000)

    assert calls == [("holding_diagnosis", ["600611"])]
    assert "DOC-CONTEXT" in prompt


def test_step4_prompt_injects_docs_for_positions_and_candidates(monkeypatch):
    calls = []
    monkeypatch.setattr(
        step4_rebalancer,
        "build_llm_doc_context",
        lambda scope, **kwargs: calls.append((scope, kwargs["symbols"], kwargs["as_of"])) or "DOC-CONTEXT",
    )
    portfolio = PortfolioState(
        free_cash=10_000,
        total_equity=20_000,
        positions=[PositionItem("600611", "大众交通", 4.0, "2026-08-01", 100)],
    )

    prompt = step4_rebalancer._build_user_message(
        benchmark_text="",
        portfolio=portfolio,
        total_equity=20_000,
        candidate_codes=["600000"],
        allowed_codes={"600000"},
        max_new_buy_names=1,
        positions_payload="positions",
        candidate_payload="candidates",
        position_failures=[],
        candidate_failures=[],
        holdings_intraday_report="",
        external_report="",
        trade_date="2026-08-05",
        order_config=Step4OrderConfig(),
        ai_candidate_policy="veto_only",
    )

    assert calls[0][0:2] == ("step4", ["600611", "600000"])
    assert calls[0][2].isoformat() == "2026-08-05"
    assert "DOC-CONTEXT" in prompt
