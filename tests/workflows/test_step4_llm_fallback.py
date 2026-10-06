"""Step4 决策模型的备用 provider 与降级路径。"""

from __future__ import annotations

from types import SimpleNamespace

import workflows.step4_llm as mod
import workflows.step4_rebalancer as step4


def _options(provider="efficiency", model="m1"):
    return SimpleNamespace(
        provider=provider,
        model=model,
        api_key="k",
        llm_base_url="",
        runtime_config=SimpleNamespace(max_output_tokens=1024),
    )


def _context():
    return SimpleNamespace(user_message="u", allowed_codes={"000001"}, name_map={})


def test_errors_and_empty_content_fall_back_to_next_provider(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return "" if len(calls) == 2 else '{"decisions": []}'

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    raw = mod._call_with_fallback(_options(), _context())

    assert raw == '{"decisions": []}'
    assert len(calls) == 3


def test_all_providers_failing_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "call_llm", lambda **kw: "")
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("key", f"model-{name}", None))

    assert mod._call_with_fallback(_options(), _context()) is None


def test_providers_without_key_are_skipped(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs["provider"])
        return ""

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(mod, "get_provider_credentials", lambda name: ("", "", None))

    mod._call_with_fallback(_options(), _context())

    assert calls == ["efficiency"]


def test_stop_loss_only_fallback_persists_orders_and_stops(monkeypatch):
    """LLM 全挂时降级工单必须落库，不能只发 Telegram。

    否则 ATR 上移止损不会写入 portfolio_positions，强制 EXIT 也不进 trade_orders，
    次日高水位止损丢失，未执行离场审计也会漏掉本轮保护指令。
    """
    persist_calls: list[dict] = []
    tickets = [SimpleNamespace(action="HOLD"), SimpleNamespace(action="EXIT")]

    monkeypatch.setattr(
        step4,
        "execute_step4_decisions",
        lambda *_args, **_kwargs: (tickets, 50_000.0),
    )

    def fake_persist(**kwargs):
        persist_calls.append(kwargs)
        return True, "ok"

    monkeypatch.setattr(step4, "_send_and_persist_step4_results", fake_persist)

    options = SimpleNamespace(
        order_config=SimpleNamespace(),
        runtime_config=SimpleNamespace(atr_period=14),
        portfolio_id="P1",
        tg_bot_token="t",
        tg_chat_id="c",
        model="m",
    )
    context = SimpleNamespace(
        portfolio=SimpleNamespace(
            free_cash=50_000.0,
            positions=[SimpleNamespace(code="000001", name="平安银行", stop_loss=9.0)],
        ),
        total_equity=100_000.0,
        trade_date="2026-09-29",
        state_signature="sig",
    )

    ok, status = step4._run_stop_loss_only_fallback(
        options,
        context,
        report_progress=lambda *_a, **_k: None,
        status="empty_content",
    )

    assert ok is False
    assert status == "empty_content_degraded"
    assert len(persist_calls) == 1
    assert persist_calls[0]["tickets"] is tickets
    assert persist_calls[0]["model_label"] == "degraded:empty_content"
    assert persist_calls[0]["supersede_previous"] is False
    assert "仅执行止损保护" in persist_calls[0]["rendered_market_view"]


def test_stop_loss_only_fallback_surfaces_persist_failure(monkeypatch):
    monkeypatch.setattr(
        step4,
        "execute_step4_decisions",
        lambda *_args, **_kwargs: ([SimpleNamespace(action="HOLD")], 1.0),
    )
    monkeypatch.setattr(
        step4,
        "_send_and_persist_step4_results",
        lambda **_kwargs: (False, "persistence_failed"),
    )
    options = SimpleNamespace(order_config=SimpleNamespace())
    context = SimpleNamespace(
        portfolio=SimpleNamespace(
            positions=[SimpleNamespace(code="600519", name="茅台", stop_loss=1700.0)],
        ),
    )

    ok, status = step4._run_stop_loss_only_fallback(
        options,
        context,
        report_progress=lambda *_a, **_k: None,
        status="all_providers_failed",
    )

    assert ok is False
    assert status == "all_providers_failed_degraded_persistence_failed"
