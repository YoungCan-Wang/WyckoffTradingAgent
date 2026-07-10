from __future__ import annotations


def test_persist_theme_radar_snapshot_uses_supabase_first(monkeypatch):
    from integrations import theme_radar_storage as mod

    calls = []
    monkeypatch.setattr(
        "integrations.supabase_theme_radar.upsert_theme_radar_snapshot", lambda snapshot: calls.append(snapshot) or 1
    )

    result = mod.persist_theme_radar_snapshot({"trade_date": "2026-05-27"})

    assert result == {"supabase": 1, "sqlite": 0}
    assert calls == [{"trade_date": "2026-05-27"}]


def test_persist_theme_radar_snapshot_can_disable_local_fallback(monkeypatch):
    from integrations import theme_radar_storage as mod

    monkeypatch.setattr("integrations.supabase_theme_radar.upsert_theme_radar_snapshot", lambda _snapshot: 0)

    result = mod.persist_theme_radar_snapshot({"trade_date": "2026-05-27"}, local_fallback=False)

    assert result == {"supabase": 0, "sqlite": 0}


def test_load_latest_theme_radar_snapshot_uses_supabase(monkeypatch):
    from integrations import theme_radar_storage as mod

    monkeypatch.setattr(
        "integrations.supabase_theme_radar.load_latest_theme_radar_snapshot_from_supabase",
        lambda: {"trade_date": "2026-05-27"},
    )

    assert mod.load_latest_theme_radar_snapshot() == {"trade_date": "2026-05-27"}


def test_load_latest_theme_radar_snapshot_falls_back_to_sqlite(monkeypatch):
    from integrations import theme_radar_storage as mod

    calls = []
    monkeypatch.setattr(
        "integrations.supabase_theme_radar.load_latest_theme_radar_snapshot_from_supabase", lambda: None
    )
    monkeypatch.setattr("integrations.local_db.init_db", lambda: calls.append("init"))
    monkeypatch.setattr(
        "integrations.local_db.load_latest_theme_radar_snapshot",
        lambda: {"trade_date": "2026-05-26"},
    )

    assert mod.load_latest_theme_radar_snapshot() == {"trade_date": "2026-05-26"}
    assert calls == ["init"]
