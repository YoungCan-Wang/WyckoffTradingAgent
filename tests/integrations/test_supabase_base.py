"""integrations/supabase_base.py 冒烟测试。"""

from __future__ import annotations

import pytest

from integrations.supabase_base import create_admin_client, is_admin_configured, require_server_write_context


class TestIsAdminConfigured:
    def test_not_configured_when_env_empty(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        assert is_admin_configured() is False

    def test_not_configured_with_anon_key_only(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.setenv("SUPABASE_KEY", "anon-key")
        assert is_admin_configured() is False

    def test_configured_when_env_set(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key-123")
        assert is_admin_configured() is True


def test_create_admin_client_requires_service_role(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_KEY", "anon-key")

    with pytest.raises(ValueError, match="SUPABASE_SERVICE_ROLE_KEY"):
        create_admin_client()


def test_require_server_write_context(monkeypatch):
    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)
    with pytest.raises(PermissionError, match="server_job"):
        require_server_write_context("upsert signal_observations")

    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    require_server_write_context("upsert signal_observations")
