from __future__ import annotations

import socket

import pytest


def test_network_guard_blocks_socket_connections() -> None:
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="must not make real network calls"):
            sock.connect(("127.0.0.1", 9))
        with pytest.raises(RuntimeError, match="must not make real network calls"):
            sock.connect_ex(("127.0.0.1", 9))
    finally:
        sock.close()


def test_network_guard_blocks_create_connection() -> None:
    with pytest.raises(RuntimeError, match="must not make real network calls"):
        socket.create_connection(("127.0.0.1", 9))
