"""trade_orders 查询不得因单页 limit 静默丢掉未取消工单。"""

from __future__ import annotations

from types import SimpleNamespace

import integrations.supabase_portfolio as portfolio


class _PagedTable:
    """Minimal PostgREST table stub that pages via range() and records cancel ids."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.cancelled_ids: list = []
        self._mode = "select"
        self._range: tuple[int, int] | None = None
        self.not_ = self

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def update(self, *_a, **_k):
        self._mode = "update"
        return self

    def eq(self, *_a, **_k):
        return self

    def neq(self, *_a, **_k):
        return self

    def in_(self, column: str, values):
        if self._mode == "update" and column == "id":
            self.cancelled_ids.extend(list(values))
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start: int, end: int):
        self._range = (start, end)
        return self

    def execute(self):
        if self._mode == "update":
            return SimpleNamespace(data=[])
        start, _end = self._range or (0, portfolio._TRADE_ORDER_PAGE - 1)
        page_idx = start // portfolio._TRADE_ORDER_PAGE
        batch = self.pages[page_idx] if page_idx < len(self.pages) else []
        return SimpleNamespace(data=batch)


def test_cancel_trade_orders_pages_past_500_and_cancels_all_active(monkeypatch):
    # Same-day degraded HOLDs fill page 1; prior full-OMS BUY/EXIT sit on page 2.
    # Old limit(500) without pagination would cancel only page 1 and leave live tickets.
    page1 = [{"id": f"deg-{i}", "status": "APPROVED", "run_id": "degraded"} for i in range(portfolio._TRADE_ORDER_PAGE)]
    page2 = [
        {"id": "live-buy", "status": "APPROVED", "run_id": "prev"},
        {"id": "live-exit", "status": "APPROVED", "run_id": "prev"},
    ]
    table = _PagedTable([page1, page2])

    monkeypatch.setattr(portfolio, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio, "require_server_write_context", lambda _op: None)
    monkeypatch.setattr(portfolio, "_get_supabase_admin_client", lambda: SimpleNamespace(table=lambda _n: table))

    cancelled = portfolio.cancel_trade_orders(
        portfolio_id="P1",
        trade_date="2026-10-14",
        exclude_run_id="new-run",
        raise_on_error=True,
    )

    assert cancelled == portfolio._TRADE_ORDER_PAGE + 2
    assert {"live-buy", "live-exit"}.issubset(set(table.cancelled_ids))
    assert len(table.cancelled_ids) == portfolio._TRADE_ORDER_PAGE + 2


def test_check_daily_run_exists_finds_full_oms_beyond_first_page(monkeypatch):
    # Newest page is all degraded protection tickets; full OMS sits on page 2.
    page1 = [
        {
            "run_id": f"deg-{i}",
            "status": "APPROVED",
            "created_at": "2026-10-14T12:00:00+00:00",
            "model": "degraded:llm_failed",
        }
        for i in range(portfolio._TRADE_ORDER_PAGE)
    ]
    page2 = [
        {
            "run_id": "20261014_090000_abcd1234_sigabcdef01234567",
            "status": "APPROVED",
            "created_at": "2026-10-14T01:00:00+00:00",
            "model": "gemini:flash",
        }
    ]
    table = _PagedTable([page1, page2])
    monkeypatch.setattr(portfolio, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio, "_get_supabase_admin_client", lambda: SimpleNamespace(table=lambda _n: table))

    assert portfolio.check_daily_run_exists("P1", "2026-10-14", state_signature="abcdef01234567") is True


def test_load_recent_trade_orders_keeps_prior_day_exits_after_same_day_flood(monkeypatch):
    today = [
        {"code": "000001", "name": "A", "action": "HOLD", "status": "APPROVED", "trade_date": "2026-10-14"}
        for _ in range(portfolio._TRADE_ORDER_PAGE)
    ]
    yesterday_exit = [
        {
            "code": "600519",
            "name": "茅台",
            "action": "EXIT",
            "status": "APPROVED",
            "trade_date": "2026-10-13",
        }
    ]
    table = _PagedTable([today, yesterday_exit])
    monkeypatch.setattr(portfolio, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(portfolio, "_get_supabase_admin_client", lambda: SimpleNamespace(table=lambda _n: table))

    rows = portfolio.load_recent_trade_orders("P1", limit=200, min_run_dates=2)

    assert any(r.get("code") == "600519" and r.get("action") == "EXIT" for r in rows)
    assert {str(r.get("trade_date"))[:10] for r in rows} >= {"2026-10-14", "2026-10-13"}
