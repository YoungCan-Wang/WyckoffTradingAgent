from __future__ import annotations

from integrations.supabase_portfolio import compute_portfolio_state_signature, upsert_position
from workflows.holding_diagnosis_core import _normalize_effective_positions


def test_signature_includes_hk_and_us_codes() -> None:
    sig_cn = compute_portfolio_state_signature(1000, [{"code": "600519", "shares": 100, "cost_price": 10}])
    sig_mixed = compute_portfolio_state_signature(
        1000,
        [
            {"code": "600519", "shares": 100, "cost_price": 10},
            {"code": "06881.HK", "shares": 1000, "cost_price": 7.68},
            {"code": "AAPL.US", "shares": 10, "cost_price": 200},
        ],
    )
    assert sig_cn
    assert sig_mixed
    assert sig_cn != sig_mixed


def test_upsert_position_accepts_hk_code(monkeypatch) -> None:
    captured: dict = {}

    class FakeTable:
        def upsert(self, row, on_conflict=None):
            captured["row"] = row
            captured["on_conflict"] = on_conflict

            class Result:
                def execute(self_inner):
                    return self_inner

            return Result()

    class FakeClient:
        def table(self, name):
            captured["table"] = name
            return FakeTable()

    monkeypatch.setattr(
        "integrations.supabase_portfolio._resolve_write_client",
        lambda client, operation: FakeClient(),
    )
    monkeypatch.setattr("integrations.supabase_portfolio._ensure_portfolio_exists", lambda *a, **k: None)

    ok, msg = upsert_position(
        "USER_LIVE:u1",
        {"code": "6881.HK", "name": "中国银河", "shares": 1000, "cost_price": 7.68, "buy_dt": "20260807"},
        client=object(),  # type: ignore[arg-type]
    )
    assert ok is True
    assert captured["row"]["code"] == "06881.HK"
    assert "06881.HK" in msg


def test_upsert_position_omits_empty_buy_dt(monkeypatch) -> None:
    captured: dict = {}

    class FakeTable:
        def upsert(self, row, on_conflict=None):
            captured["row"] = row

            class Result:
                def execute(self_inner):
                    return self_inner

            return Result()

    class FakeClient:
        def table(self, name):
            return FakeTable()

    monkeypatch.setattr(
        "integrations.supabase_portfolio._resolve_write_client",
        lambda client, operation: FakeClient(),
    )
    monkeypatch.setattr("integrations.supabase_portfolio._ensure_portfolio_exists", lambda *a, **k: None)

    ok, _msg = upsert_position(
        "USER_LIVE:u1",
        {"code": "000001", "name": "平安银行", "shares": 200, "cost_price": 10.5, "buy_dt": ""},
        client=object(),  # type: ignore[arg-type]
    )
    assert ok is True
    assert "buy_dt" not in captured["row"]
    assert captured["row"]["shares"] == 200
    assert captured["row"]["cost_price"] == 10.5


def test_upsert_position_rejects_bare_short_digits(monkeypatch) -> None:
    ok, msg = upsert_position("USER_LIVE:u1", {"code": "6881", "shares": 1, "cost_price": 1}, client=object())  # type: ignore[arg-type]
    assert ok is False
    assert "无效" in msg


def test_holding_diagnosis_keeps_hk_us_codes() -> None:
    positions, stats = _normalize_effective_positions(
        [
            {"code": "06881.HK", "name": "中国银河", "shares": 1000, "cost": 7.68},
            {"code": "AAPL.US", "name": "Apple", "shares": 10, "cost": 200},
            {"code": "600519", "name": "贵州茅台", "shares": 100, "cost": 1500},
            {"code": "6881", "shares": 1, "cost": 1},
        ]
    )
    assert [p["code"] for p in positions] == ["06881.HK", "AAPL.US", "600519"]
    assert stats["invalid_code"] == 1
    assert stats["active"] == 3
