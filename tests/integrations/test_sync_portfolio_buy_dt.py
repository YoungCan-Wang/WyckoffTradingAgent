"""sync_portfolio must preserve buy_dt when caching Supabase → local SQLite."""

from __future__ import annotations


def test_sync_portfolio_preserves_buy_dt(monkeypatch, tmp_path):
    from integrations import local_db, sync

    monkeypatch.setenv("WYCKOFF_DB_PATH", str(tmp_path / "sync.db"))
    local_db.init_db()

    state = {
        "portfolio_id": "USER_LIVE:u1",
        "free_cash": 10000.0,
        "positions": [
            {
                "code": "000001",
                "name": "平安银行",
                "shares": 1000,
                "cost": 10.5,
                "buy_dt": "2026-09-10",
                "stop_loss": 9.5,
            }
        ],
    }

    monkeypatch.setattr(
        "integrations.supabase_portfolio.load_portfolio_state",
        lambda *_a, **_k: state,
    )

    written = sync.sync_portfolio("USER_LIVE:u1", client=object())
    assert written > 0
    cached = local_db.load_portfolio("USER_LIVE:u1")
    assert cached is not None
    assert cached["positions"][0]["buy_dt"] == "2026-09-10"
    assert cached["positions"][0]["stop_loss"] == 9.5
