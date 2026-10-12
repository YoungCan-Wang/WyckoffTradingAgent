"""降级保护工单不得挡住同日完整 OMS 补跑。"""

from __future__ import annotations

from types import SimpleNamespace

import integrations.supabase_portfolio as portfolio
import workflows.step4_results as step4_results
from workflows.step4_models import ExecutionTicket


def test_check_daily_run_exists_ignores_degraded_protection_orders(monkeypatch):
    rows = [
        {
            "run_id": "20261011_120000_abcd1234_sigabcdef01234567",
            "status": "APPROVED",
            "created_at": "2026-10-11T12:00:00+00:00",
            "model": "degraded:llm_failed",
        }
    ]

    class Query:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return SimpleNamespace(data=rows)

    monkeypatch.setattr(portfolio, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio, "_get_supabase_admin_client", lambda: SimpleNamespace(table=lambda _n: Query()))

    assert portfolio.check_daily_run_exists("P1", "2026-10-11", state_signature="abcdef01234567") is False


def test_check_daily_run_exists_still_blocks_on_full_oms_with_matching_signature(monkeypatch):
    rows = [
        {
            "run_id": "20261011_120000_abcd1234_sigabcdef01234567",
            "status": "APPROVED",
            "created_at": "2026-10-11T12:00:00+00:00",
            "model": "gemini:flash",
        }
    ]

    class Query:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return SimpleNamespace(data=rows)

    monkeypatch.setattr(portfolio, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio, "_get_supabase_admin_client", lambda: SimpleNamespace(table=lambda _n: Query()))

    assert portfolio.check_daily_run_exists("P1", "2026-10-11", state_signature="abcdef01234567") is True
    assert portfolio.check_daily_run_exists("P1", "2026-10-11", state_signature="deadbeefdeadbeef") is False


def test_degraded_result_record_omits_state_signature_from_run_id():
    ticket = ExecutionTicket(
        code="000001",
        name="测试",
        action="HOLD",
        status="APPROVED",
        shares=100,
        price_hint=10.0,
        amount=1000.0,
        stop_loss=9.0,
        max_loss=0.0,
        drawdown_ratio=0.0,
        reason="degraded",
        tape_condition="",
        invalidate_condition="",
        is_holding=True,
        atr14=0.5,
        original_stop_loss=9.0,
        effective_stop_loss=9.0,
        slippage_bps=0.0,
        audit="",
    )
    bound = step4_results.prepare_step4_result_record(tickets=[ticket], state_signature="abcdef01234567")
    free = step4_results.prepare_step4_result_record(
        tickets=[ticket],
        state_signature="abcdef01234567",
        bind_state_signature=False,
    )

    assert bound.run_id.endswith("_sigabcdef01234567")
    assert "_sig" not in free.run_id
