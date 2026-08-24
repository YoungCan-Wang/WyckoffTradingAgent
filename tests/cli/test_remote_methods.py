"""遥控的 IPC 方法：开关、配对、设备管理。

传输层本身在 test_ipc_remote.py。这里测的是「桌面端点开关时发生什么」。
"""

from __future__ import annotations

import logging

import pytest

from cli.ipc.methods import MethodError, dispatch


def _result(method: str, params: dict | None = None):
    events = list(dispatch(method, params or {}))
    return next((e for e in events if e.get("type") == "result"), None)


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def signed_out(monkeypatch):
    monkeypatch.setattr("cli.ipc.methods._remote_credentials", lambda: ("", ""))


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr("cli.ipc.methods._remote_credentials", lambda: ("tok-abc", "user-1"))


class FakeHttp:
    """替掉 _remote_http，记录调用。真调用会打网络。"""

    def __init__(self, reply: dict | None = None, error: MethodError | None = None) -> None:
        self.reply = reply or {}
        self.error = error
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        self.calls.append((path, method, payload))
        if self.error is not None:
            raise self.error
        return self.reply


def test_status_reports_signed_out(signed_out):
    out = _result("remote_status")
    assert out["signed_in"] is False
    assert out["running"] is False


def test_enable_requires_login(signed_out):
    with pytest.raises(MethodError) as exc:
        list(dispatch("remote_enable", {}))
    assert exc.value.code == "not_signed_in"
    # 要说明为什么，而不是一句 unauthorized
    assert "同一个账号" in exc.value.message


def test_enable_starts_the_bridge(signed_in, monkeypatch):
    started: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "cli.ipc.remote.start_bridge",
        lambda url, token, label="": started.append((url, token, label)),
    )
    out = _result("remote_enable")
    assert out["enabled"] is True
    assert started and started[0][1] == "tok-abc"
    assert started[0][0].startswith("wss://")


def test_disable_also_revokes_paired_phones(signed_in, monkeypatch):
    """只断电脑这端不够：配对码还没过期时手机能再连回来。"""
    monkeypatch.setattr("cli.ipc.remote.stop_bridge", lambda: None)
    http = FakeHttp({"revoked": 2})
    monkeypatch.setattr("cli.ipc.methods._remote_http", http)
    out = _result("remote_disable")
    assert out["enabled"] is False
    assert http.calls == [("revoke", "POST", {"conn_id": "*"})]


def test_disable_still_stops_locally_when_the_relay_is_down(signed_in, monkeypatch):
    """用户点了关闭就该关闭 —— 云端不可达不能让本地继续连着。"""
    stopped: list[bool] = []
    monkeypatch.setattr("cli.ipc.remote.stop_bridge", lambda: stopped.append(True))
    monkeypatch.setattr(
        "cli.ipc.methods._remote_http",
        FakeHttp(error=MethodError("relay_unreachable", "连不上")),
    )
    out = _result("remote_disable")
    assert out["enabled"] is False
    assert stopped == [True]


def test_pair_returns_a_scannable_url(signed_in, monkeypatch):
    monkeypatch.setattr(
        "cli.ipc.methods._remote_http",
        FakeHttp({"code": "abc1234567", "expires_in_ms": 180000}),
    )
    out = _result("remote_pair")
    assert out["code"] == "abc1234567"
    # 手机扫到的是一个能打开的地址，不是裸 code
    assert out["url"].startswith("https://")
    assert "abc1234567" in out["url"]
    assert out["expires_in_ms"] == 180000


def test_pair_fails_loudly_when_the_relay_returns_nothing(signed_in, monkeypatch):
    monkeypatch.setattr("cli.ipc.methods._remote_http", FakeHttp({}))
    with pytest.raises(MethodError) as exc:
        list(dispatch("remote_pair", {}))
    assert exc.value.code == "relay_error"


def test_devices_are_passed_through(signed_in, monkeypatch):
    devices = [{"conn_id": "p1", "role": "remote", "label": "iPhone", "since": 1}]
    monkeypatch.setattr("cli.ipc.methods._remote_http", FakeHttp({"devices": devices}))
    assert _result("remote_devices")["devices"] == devices


def test_revoke_needs_a_target(signed_in):
    with pytest.raises(MethodError) as exc:
        list(dispatch("remote_revoke", {}))
    assert exc.value.code == "invalid_params"


def test_revoke_forwards_the_connection_id(signed_in, monkeypatch):
    http = FakeHttp({"revoked": 1})
    monkeypatch.setattr("cli.ipc.methods._remote_http", http)
    _result("remote_revoke", {"conn_id": "phone-a"})
    assert http.calls == [("revoke", "POST", {"conn_id": "phone-a"})]


def test_http_helper_requires_login(signed_out):
    from cli.ipc.methods import _remote_http

    with pytest.raises(MethodError) as exc:
        _remote_http("devices")
    assert exc.value.code == "not_signed_in"


def test_ws_url_derives_from_the_api_base(monkeypatch):
    import cli.ipc.methods as M

    monkeypatch.setattr(M, "REMOTE_API_BASE", "https://api.example.com")
    assert M._remote_ws_url() == "wss://api.example.com/api/remote/ws"
    # 本地 wrangler dev 是 http，要退回 ws 而不是硬写 wss
    monkeypatch.setattr(M, "REMOTE_API_BASE", "http://127.0.0.1:8787")
    assert M._remote_ws_url() == "ws://127.0.0.1:8787/api/remote/ws"


def test_all_remote_methods_are_registered():
    from cli.ipc.methods import METHODS

    for name in ("remote_status", "remote_enable", "remote_disable", "remote_pair", "remote_devices", "remote_revoke"):
        assert name in METHODS
