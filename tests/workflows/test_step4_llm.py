from __future__ import annotations

from datetime import date

import workflows.step4_llm as step4_llm
from integrations.fetch_a_share_csv import TradingWindow
from workflows.step4_models import (
    PortfolioState,
    Step4InputContext,
    Step4OrderConfig,
    Step4RunOptions,
    Step4RuntimeConfig,
)


def _options() -> Step4RunOptions:
    return Step4RunOptions(
        provider="openai",
        model="gpt-test",
        api_key="key",
        llm_base_url="https://llm.example/v1",
        portfolio_id="P1",
        tg_bot_token="tg",
        tg_chat_id="chat",
        runtime_config=Step4RuntimeConfig(max_output_tokens=1234),
        order_config=Step4OrderConfig(),
    )


def _context() -> Step4InputContext:
    return Step4InputContext(
        portfolio=PortfolioState(free_cash=10000, total_equity=20000, positions=[]),
        state_signature="sig",
        window=TradingWindow(date(2026, 5, 1), date(2026, 5, 15)),
        trade_date="2026-05-15",
        total_equity=20000,
        latest_price_map={"000001": 9.8},
        atr_map={"000001": 0.2},
        allowed_codes={"000001"},
        candidate_meta_map={},
        name_map={"000001": "平安银行"},
        market_regime="NEUTRAL",
        system_market_view="系统风控",
        user_message="用户消息",
    )


def test_call_step4_decision_model_parses_valid_json(monkeypatch):
    calls: dict[str, object] = {}
    progress: list[tuple[str, str, float]] = []
    monkeypatch.setattr(step4_llm, "dump_model_input", lambda **kwargs: calls.setdefault("dump", kwargs))

    def fake_call_llm(**kwargs):
        calls["llm"] = kwargs
        return """
        {
          "market_view": "谨慎观察",
          "decisions": [
            {"code": "000001", "action": "HOLD", "reason": "结构未坏", "confidence": 0.7}
          ]
        }
        """

    monkeypatch.setattr(step4_llm, "call_llm", fake_call_llm)

    ok, status, result = step4_llm.call_step4_decision_model(
        _options(),
        _context(),
        lambda title, detail, ratio: progress.append((title, detail, ratio)),
    )

    assert (ok, status) == (True, "ok")
    assert result is not None
    assert result.market_view == "谨慎观察"
    assert result.decisions[0].code == "000001"
    assert result.decisions[0].name == "平安银行"
    assert calls["llm"]["max_output_tokens"] == 1234
    assert calls["llm"]["base_url"] == "https://llm.example/v1"
    assert calls["dump"]["symbols"] == ["000001"]
    assert progress == [("LLM决策", "计算中", 0.5)]


def test_call_step4_decision_model_reports_parse_failure(monkeypatch):
    monkeypatch.setattr(step4_llm, "dump_model_input", lambda **_kwargs: None)
    monkeypatch.setattr(step4_llm, "call_llm", lambda **_kwargs: "not-json")

    ok, status, result = step4_llm.call_step4_decision_model(_options(), _context(), lambda *_args: None)

    assert (ok, status, result) == (False, "llm_failed", None)
