"""Tests for the standalone daily NAV snapshot."""

from __future__ import annotations

import pytest

from workflows import nav_snapshot
from workflows.nav_snapshot import NavSnapshotResult, build_nav_snapshot, persist_nav_snapshot


@pytest.fixture
def _state(monkeypatch):
    """把外部依赖收敛成可控替身：Supabase 客户端、组合状态、行情、TickFlow key。"""

    holder = {
        "positions": [{"code": "600519", "shares": 100}],
        "free_cash": 5_000.0,
        "prices": {"600519": 1_500.0},
        "rates": {"CNY": 1.0},
        "api_key": "k",
    }
    monkeypatch.setattr("integrations.supabase_base.create_admin_client", lambda: object())
    monkeypatch.setattr(
        "integrations.supabase_portfolio.load_portfolio_state",
        lambda pid, client=None: {"positions": holder["positions"], "free_cash": holder["free_cash"]},
    )
    monkeypatch.setattr("integrations.supabase_portfolio.portfolio_tickflow_key", lambda pid, client: holder["api_key"])
    monkeypatch.setattr(
        "integrations.portfolio_market_value.load_portfolio_marks",
        lambda positions, api_key: (holder["prices"], holder["rates"]),
    )
    return holder


class TestBuildSnapshot:
    def test_values_positions_at_market(self, _state):
        result = build_nav_snapshot("USER_LIVE", "2026-08-17")
        assert result.ok is True
        assert result.positions_value == 150_000.0
        assert result.total_equity == 155_000.0
        assert result.free_cash == 5_000.0

    def test_empty_position_still_records(self, _state):
        """空仓也要记：净值曲线不能因为清仓而断档。"""
        _state["positions"] = []
        result = build_nav_snapshot("USER_LIVE", "2026-08-17")
        assert result.ok is True
        assert result.positions_value == 0.0
        assert result.total_equity == _state["free_cash"]

    def test_refuses_partial_valuation(self, _state):
        """行情缺失时不写部分估值——偏低的净值比没有净值更有害。"""
        _state["prices"] = {}
        result = build_nav_snapshot("USER_LIVE", "2026-08-17")
        assert result.ok is False
        assert "估值不完整" in result.message

    def test_requires_trade_date(self, _state):
        assert build_nav_snapshot("USER_LIVE", "").ok is False

    def test_missing_portfolio(self, monkeypatch, _state):
        monkeypatch.setattr("integrations.supabase_portfolio.load_portfolio_state", lambda pid, client=None: None)
        result = build_nav_snapshot("NOPE", "2026-08-17")
        assert result.ok is False
        assert "未找到组合" in result.message

    def test_missing_api_key(self, _state):
        _state["api_key"] = ""
        result = build_nav_snapshot("USER_LIVE", "2026-08-17")
        assert result.ok is False
        assert "TickFlow" in result.message

    def test_ignores_zero_share_rows(self, _state):
        _state["positions"] = [{"code": "600519", "shares": 0}]
        result = build_nav_snapshot("USER_LIVE", "2026-08-17")
        # 全是 0 股等价于空仓，不应因缺该标的报价而失败。
        assert result.ok is True
        assert result.positions_value == 0.0


class TestPersist:
    def test_writes_and_reports(self, monkeypatch):
        captured = {}

        def fake_upsert(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr("integrations.supabase_portfolio.upsert_daily_nav", fake_upsert)
        snapshot = NavSnapshotResult(True, "USER_LIVE", "2026-08-17", 155_000.0, 5_000.0, 150_000.0, "ok")
        result = persist_nav_snapshot(snapshot)
        assert result.written is True
        assert captured["total_equity"] == 155_000.0
        assert captured["trade_date"] == "2026-08-17"

    def test_skips_failed_snapshot(self, monkeypatch):
        called = {"n": 0}

        def fake_upsert(**kwargs):
            called["n"] += 1
            return True

        monkeypatch.setattr("integrations.supabase_portfolio.upsert_daily_nav", fake_upsert)
        snapshot = NavSnapshotResult(False, "USER_LIVE", "2026-08-17", message="估值不完整")
        assert persist_nav_snapshot(snapshot).written is False
        assert called["n"] == 0

    def test_write_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr("integrations.supabase_portfolio.upsert_daily_nav", lambda **kwargs: False)
        snapshot = NavSnapshotResult(True, "USER_LIVE", "2026-08-17", 1.0, 1.0, 0.0, "ok")
        result = persist_nav_snapshot(snapshot)
        assert result.written is False
        assert result.ok is True


class TestMissingDates:
    def test_reports_gaps_only(self, monkeypatch):
        class _Table:
            def select(self, *_a):
                return self

            def eq(self, *_a):
                return self

            def gte(self, *_a):
                return self

            def lte(self, *_a):
                return self

            def execute(self):
                return type("R", (), {"data": [{"trade_date": "2026-08-06"}]})()

        monkeypatch.setattr(
            "integrations.supabase_base.create_admin_client",
            lambda: type("C", (), {"table": lambda self, name: _Table()})(),
        )
        monkeypatch.setattr(
            "integrations.fetch_a_share_csv.cached_trade_dates",
            lambda: ["2026-08-06", "2026-08-07", "2026-08-10"],
        )
        gaps = nav_snapshot.missing_nav_dates("USER_LIVE", "2026-08-06", "2026-08-10")
        assert gaps == ["2026-08-07", "2026-08-10"]
