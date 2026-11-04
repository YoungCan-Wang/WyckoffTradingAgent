"""
共享 pytest fixtures — mock 外部服务，让 core/ 单测可离线运行。
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """防止测试通过标准库或 HTTP 客户端发出真实网络请求。"""

    def _guard(*args, **kwargs):
        raise RuntimeError("Tests must not make real network calls")

    monkeypatch.setattr(socket, "create_connection", _guard)
    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard)


@pytest.fixture(autouse=True)
def _no_background_sync(monkeypatch):
    """不让测试起 `wyckoff-sync` 后台线程。

    `init_local_services()`（IPC 会话 start、CLI、headless 都会调）起的是一个不被 join
    的守护线程，其中 `restore_session()` 读的是**当时**被 monkeypatch 的 `SESSION_FILE`，
    线程会活过起它的测试，落到后面测试的临时目录上。supabase-auth 2.34 起 `set_session`
    先在本地校验 JWT 结构，测试里的假 token 立刻抛「Invalid JWT structure」，被当成会话
    失效，`_clear_session_if_same_user` 就删掉了后一个测试刚写好的同名会话 ——
    test_auth_and_cloud_config 在 CI 上间歇报 StopIteration 就是这么来的。它还会在共享
    连接上跑 init_db（见 test_background_results 记的 CI segfault）。
    """
    import integrations.sync as sync

    monkeypatch.setattr(sync, "sync_all_background", lambda: None)


@pytest.fixture()
def mock_supabase():
    """返回一个 MagicMock supabase Client，用于 integrations 层测试。"""
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value.data = []
    return client
