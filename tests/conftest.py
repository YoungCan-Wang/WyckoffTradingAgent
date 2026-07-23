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


@pytest.fixture()
def mock_supabase():
    """返回一个 MagicMock supabase Client，用于 integrations 层测试。"""
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value.data = []
    return client
