from __future__ import annotations

import pytest

from integrations.radar_supabase import (
    RadarCredentialsMissing,
    radar_configured,
    radar_env_help,
    resolve_radar_credentials,
)


def test_missing_radar_creds_fail_closed(monkeypatch) -> None:
    for name in (
        "RADAR_SUPABASE_URL",
        "RADAR_SUPABASE_SERVICE_ROLE_KEY",
        "RADAR_SUPABASE_SERVICE_KEY",
        "MAINLINE_RADAR_SUPABASE_URL",
        "MAINLINE_RADAR_SUPABASE_SERVICE_ROLE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert radar_configured() is False
    with pytest.raises(RadarCredentialsMissing, match="RADAR_SUPABASE_URL"):
        resolve_radar_credentials()
    assert "RADAR_SUPABASE_SERVICE_ROLE_KEY" in radar_env_help()


def test_radar_creds_do_not_reuse_wyckoff_supabase(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://yfyivczvmorpqdyehfmn.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "wyckoff-service-key")
    monkeypatch.delenv("RADAR_SUPABASE_URL", raising=False)
    monkeypatch.delenv("RADAR_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("RADAR_SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("MAINLINE_RADAR_SUPABASE_URL", raising=False)
    monkeypatch.delenv("MAINLINE_RADAR_SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert radar_configured() is False
    with pytest.raises(RadarCredentialsMissing):
        resolve_radar_credentials()


def test_radar_service_key_alias(monkeypatch) -> None:
    monkeypatch.setenv("RADAR_SUPABASE_URL", "https://rnqbgmxvqlygymrjmmwv.supabase.co")
    monkeypatch.setenv("RADAR_SUPABASE_SERVICE_KEY", "radar-key")
    monkeypatch.delenv("RADAR_SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert resolve_radar_credentials() == ("https://rnqbgmxvqlygymrjmmwv.supabase.co", "radar-key")
