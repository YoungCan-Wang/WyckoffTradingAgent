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


@pytest.mark.parametrize("original", [None, "", "unknown", "0"])
def test_read_only_scope_preserves_reads_blocks_workers_and_restores_environment(monkeypatch, original):
    import os
    from concurrent.futures import ThreadPoolExecutor

    from integrations import supabase_base as sb

    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    if original is None:
        monkeypatch.delenv("WYCKOFF_SHARED_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("WYCKOFF_SHARED_READ_ONLY", original)
    monkeypatch.setattr(sb, "is_admin_configured", lambda: True)
    monkeypatch.setattr(sb, "create_admin_client", lambda: "shared-read-client")
    with pytest.raises(RuntimeError, match="replay failure"), sb.read_only_write_context():
        assert sb.create_read_client() == "shared-read-client"
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(sb.is_server_write_context).result() is False
            assert pool.submit(sb.create_read_client).result() == "shared-read-client"
        os.environ["WYCKOFF_SHARED_READ_ONLY"] = "0"
        os.environ["WYCKOFF_WRITE_CONTEXT"] = "server_job"
        with pytest.raises(PermissionError):
            sb.require_server_write_context("attempted shared write")
        raise RuntimeError("replay failure")
    assert os.environ.get("WYCKOFF_SHARED_READ_ONLY") == original
    assert sb.is_server_write_context()


def test_nested_read_only_scope_does_not_restore_write_permission_early(monkeypatch):
    from integrations import supabase_base as sb

    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.delenv("WYCKOFF_SHARED_READ_ONLY", raising=False)
    with sb.read_only_write_context():
        with sb.read_only_write_context():
            assert not sb.is_server_write_context()
        assert not sb.is_server_write_context()
    assert sb.is_server_write_context()


class TestNetworkTimeout:
    """所有客户端都必须带网络超时。

    实测踩到的：桌面端切到持仓页永久卡在「读取中…」。量下来 account 25ms 返回，
    portfolio 超过 15s 从未结束 —— 它卡在 `client.auth.set_session` 的 TLS 握手上。

    supabase-py 的默认值救不了这个场景：postgrest 默认 120s（界面挂两分钟），
    而 auth 那个 gotrue client **根本没有超时选项**。唯一的口子是注入自己的
    httpx client，它同时被 auth 和 postgrest 采用。
    """

    def test_auth_and_postgrest_share_our_timeout_client(self, monkeypatch) -> None:
        from integrations import supabase_base as sb

        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "anon-key")
        client = sb.create_anon_client()

        auth_http = getattr(client.auth, "_http_client", None)
        assert auth_http is not None, "拿不到 auth 的 http client —— 超时无从验证"
        # auth 是当初卡住的那一条链路，必须有超时
        assert auth_http.timeout.connect == sb.CONNECT_TIMEOUT_SECONDS
        assert auth_http.timeout.read == sb.READ_TIMEOUT_SECONDS

        session = getattr(client.postgrest, "session", None)
        assert session is not None
        assert session.timeout.read == sb.READ_TIMEOUT_SECONDS

    def test_timeout_is_well_under_the_120s_default(self) -> None:
        """守住量级：默认 120s 等于界面挂两分钟，交互路径不能接受。"""
        from integrations import supabase_base as sb

        assert sb.CONNECT_TIMEOUT_SECONDS <= 10
        assert sb.READ_TIMEOUT_SECONDS <= 15
