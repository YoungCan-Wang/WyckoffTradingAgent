from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

from workflows import holding_diagnosis_core as hdc
from workflows.holding_diagnosis_core import (
    _finite_number,
    _normalize_effective_positions,
    holding_no_position_meta,
    resolve_holding_portfolio_context,
)


def test_finite_number_rejects_none_bad_nan_and_inf() -> None:
    assert _finite_number(None) is None
    assert _finite_number("bad") is None
    assert _finite_number(float("nan")) is None
    assert _finite_number(float("inf")) is None
    assert _finite_number(-float("inf")) is None


def test_finite_number_accepts_valid_numbers() -> None:
    assert _finite_number(100) == 100.0
    assert _finite_number("10.5") == 10.5
    assert _finite_number(0) == 0.0


def test_normalize_effective_positions_skips_invalid_numbers_without_crash() -> None:
    rows = [
        {"code": "000001", "shares": None, "cost": 10.0},
        {"code": "000002", "shares": "bad", "cost": 10.0},
        {"code": "000003", "shares": 100, "cost": "bad"},
        {"code": "000004", "shares": float("nan"), "cost": 10.0},
        {"code": "000005", "shares": float("inf"), "cost": 10.0},
        {"code": "000006", "shares": 100, "cost": 10.0},
    ]

    positions, stats = _normalize_effective_positions(rows)

    assert [p["code"] for p in positions] == ["000006"]
    assert stats["invalid_number"] == 5
    assert stats["active"] == 1
    assert stats["raw"] == 6


def test_normalize_effective_positions_keeps_zero_shares_stat_distinct_from_invalid_number() -> None:
    rows = [{"code": "000001", "shares": 0, "cost": 10.0}]

    positions, stats = _normalize_effective_positions(rows)

    assert positions == []
    assert stats["zero_shares"] == 1
    assert stats["invalid_number"] == 0


def test_resolve_holding_portfolio_context_fails_closed_without_cross_user_fallback(monkeypatch) -> None:
    """未配置用户身份时只能拿到空的 USER_LIVE 占位符，不能跨用户挑选其它账户兜底。"""
    import workflows.holding_diagnosis_core as mod

    def fake_load_portfolio_state(portfolio_id: str):
        if portfolio_id == "USER_LIVE":
            return None
        # 模拟数据库里存在别的用户仓位；正确实现绝不能触达这里。
        return {"positions": [{"code": "600000", "shares": 100, "cost": 10.0}]}

    monkeypatch.setattr(mod, "load_portfolio_state", fake_load_portfolio_state)

    context = resolve_holding_portfolio_context("USER_LIVE")

    assert context.positions == []
    assert context.resolved_portfolio_id == "USER_LIVE"
    assert "USER_LIVE" in holding_no_position_meta(context)


def test_resolve_holding_portfolio_context_uses_requested_portfolio_directly(monkeypatch) -> None:
    import workflows.holding_diagnosis_core as mod

    monkeypatch.setattr(
        mod,
        "load_portfolio_state",
        lambda portfolio_id: (
            {"positions": [{"code": "600000", "shares": 100, "cost": 10.0}]}
            if portfolio_id == "USER_LIVE:abc"
            else None
        ),
    )

    context = resolve_holding_portfolio_context("USER_LIVE:abc")

    assert context.resolved_portfolio_id == "USER_LIVE:abc"
    assert len(context.positions) == 1
    assert not math.isnan(context.positions[0]["cost"])


def test_fetch_helpers_pass_end_calendar_day_to_trading_window(monkeypatch) -> None:
    """两个取数入口都曾漏传必填的 end_calendar_day，而调用方 catch-all 把 TypeError 吞成静默降级。"""
    seen: list[dict] = []

    def fake_window(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(start_trade_date=date(2026, 1, 2), end_trade_date=date(2026, 7, 28))

    monkeypatch.setattr(hdc, "resolve_trading_window", fake_window)
    monkeypatch.setattr(hdc, "fetch_hist", lambda *a, **k: None)
    monkeypatch.setattr(hdc, "fetch_index_hist", lambda *a, **k: None)

    assert hdc.fetch_holding_daily_frame("600378") is None
    assert hdc.fetch_holding_benchmark() is None

    assert len(seen) == 2
    for kwargs in seen:
        assert isinstance(kwargs.get("end_calendar_day"), date)
        assert kwargs.get("trading_days") == hdc.TRADING_DAYS
