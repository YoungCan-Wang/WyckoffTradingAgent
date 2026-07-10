from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from workflows import holding_diagnosis_llm as llm


@dataclass
class Advice:
    code: str = "000001"
    name: str = "平安银行"
    action: str = "HOLD"
    shares: int = 1000
    cost: float = 10.0
    current_price: float = 10.8
    pnl_pct: float = 8.0
    rule_score: float = 72.0
    reasons: list[str] | None = None
    features: dict | None = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = ["站上VWAP"]
        if self.features is None:
            self.features = {"close_pos": 0.8, "dist_vwap_pct": 1.2}


def test_run_holding_llm_report_overrides_rule_action(monkeypatch):
    logs: list[str] = []
    monkeypatch.setattr(llm, "load_portfolio_state", lambda _portfolio_id: {"free_cash": 20000, "total_equity": 120000})
    monkeypatch.setattr(
        llm,
        "_build_llm_routes",
        lambda: [{"name": "route1", "provider": "p", "model": "m", "api_key": "k"}],
    )
    monkeypatch.setattr(llm, "call_llm", lambda **_kwargs: '{"action":"TRIM","reason":"尾盘转弱","confidence":0.76}')

    report = llm.run_holding_llm_report(
        holdings=[Advice()],
        rule_section="规则明细",
        portfolio_id="USER_LIVE:test",
        deadline_at=datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5),
        started_at=0,
        log=logs.append,
    )

    assert "## TRIM（确认破位减仓）" in report
    assert "000001 平安银行 [规则:HOLD] conf=76% | 尾盘转弱" in report
    assert "规则明细" in report
    assert any("LLM: 1/1 success" in line for line in logs)


def test_run_holding_llm_report_falls_back_to_rule_when_routes_missing(monkeypatch):
    monkeypatch.setattr(llm, "load_portfolio_state", lambda _portfolio_id: {"free_cash": 0, "total_equity": 0})
    monkeypatch.setattr(llm, "_build_llm_routes", lambda: [])

    report = llm.run_holding_llm_report(
        holdings=[Advice(action="EXIT")],
        rule_section="规则明细",
        portfolio_id="USER_LIVE:test",
        deadline_at=datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5),
        started_at=0,
    )

    assert "## EXIT（清仓）" in report
    assert "000001 平安银行 | (规则判断)" in report
