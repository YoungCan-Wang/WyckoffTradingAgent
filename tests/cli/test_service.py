"""Agent runtime 服务宿主：鉴权、用户绑定、SSE 流、单轮并发与断开回收。"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest

from cli.providers.base import LLMProvider
from cli.service.echo_provider import EchoProvider
from cli.service.server import CloudToolRegistry, ServiceState, default_provider_factory, make_server
from tests.helpers.agent_loop_harness import StubToolRegistry

TOKEN = "gateway-secret"


class _GatedProvider(LLMProvider):
    """先吐一个分片再卡在门上，用来把一轮钉在「进行中」。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def name(self) -> str:
        return "gated"

    def chat(self, messages, tools, system_prompt=""):
        raise NotImplementedError

    def chat_stream(self, messages, tools, system_prompt="") -> Generator[dict[str, Any], None, None]:
        yield {"type": "text_delta", "text": "hi"}
        self.entered.set()
        self.release.wait(5)
        yield {"type": "finish", "reason": "stop"}


class _EndlessProvider(LLMProvider):
    """一直吐分片，直到被关闭；finally 记录是否真的被收尾。"""

    def __init__(self) -> None:
        self.closed = threading.Event()

    @property
    def name(self) -> str:
        return "endless"

    def chat(self, messages, tools, system_prompt=""):
        raise NotImplementedError

    def chat_stream(self, messages, tools, system_prompt="") -> Generator[dict[str, Any], None, None]:
        try:
            while True:
                yield {"type": "text_delta", "text": "x" * 512}
                time.sleep(0.005)
        finally:
            self.closed.set()


class _Recorder(EchoProvider):
    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    def chat_stream(self, messages, tools, system_prompt=""):
        self.seen.append([dict(m) for m in messages])
        yield from super().chat_stream(messages, tools, system_prompt)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """覆盖 conftest 的同名守卫：分块 SSE 必须走真套接字验证，所以只放行回环，其余外联照旧禁止。"""
    real_connect = socket.socket.connect

    def _loopback_only(sock, address):
        if address[0] != "127.0.0.1":
            raise RuntimeError("Tests must not make real network calls")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _loopback_only)


@pytest.fixture
def serve():
    servers = []

    def _start(provider: LLMProvider) -> tuple[str, int, ServiceState]:
        state = ServiceState(TOKEN, StubToolRegistry(), provider_factory=lambda llm: (provider, None))
        server = make_server("127.0.0.1", 0, state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return "127.0.0.1", server.server_address[1], state

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _open(
    port: int, body: dict[str, Any], *, user: str = "u1", token: str = TOKEN
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}", "X-Wyckoff-User": user, "Content-Type": "application/json"}
    conn.request("POST", "/v1/turns", json.dumps(body), headers)
    return conn, conn.getresponse()


def _post(port: int, body: dict[str, Any], *, user: str = "u1", token: str = TOKEN) -> http.client.HTTPResponse:
    return _open(port, body, user=user, token=token)[1]


def _events(resp: http.client.HTTPResponse) -> list[dict[str, Any]]:
    frames = [line for line in resp.read().decode().split("\n\n") if line.startswith("data: ")]
    return [json.loads(frame[len("data: ") :]) for frame in frames]


def test_healthz_needs_no_auth(serve):
    _, port, _ = serve(EchoProvider())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    assert resp.status == 200
    assert json.loads(resp.read()) == {"ok": True}


@pytest.mark.parametrize("token", ["", "wrong"])
def test_turn_rejects_missing_or_wrong_token(serve, token):
    _, port, _ = serve(EchoProvider())
    assert _post(port, {"text": "hi"}, token=token).status == 401


def test_turn_streams_runtime_events_and_ends_with_done(serve):
    _, port, _ = serve(EchoProvider())
    resp = _post(port, {"text": "你好，威科夫"})
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    events = _events(resp)
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "你好，威科夫"
    assert any(event["type"] == "text_delta" for event in events)


def test_instance_is_bound_to_the_first_user(serve):
    _, port, _ = serve(EchoProvider())
    assert _post(port, {"text": "hi"}, user="alice").status == 200
    assert _post(port, {"text": "hi"}, user="mallory").status == 409


def test_missing_user_header_is_rejected(serve):
    _, port, _ = serve(EchoProvider())
    assert _post(port, {"text": "hi"}, user=" ").status == 400


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": 7}])
def test_invalid_text_is_rejected(serve, body):
    _, port, _ = serve(EchoProvider())
    assert _post(port, body).status == 400


def test_provider_error_is_a_400_without_leaking_the_key():
    state = ServiceState(TOKEN, StubToolRegistry(), provider_factory=lambda llm: (None, "boom"))
    server = make_server("127.0.0.1", 0, state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = _post(server.server_address[1], {"text": "hi", "llm": {"api_key": "sk-secret"}})
        payload = resp.read().decode()
        assert resp.status == 400
        assert "sk-secret" not in payload
    finally:
        server.shutdown()
        server.server_close()


def test_second_concurrent_turn_gets_429(serve):
    gated = _GatedProvider()
    _, port, _ = serve(gated)
    first: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: first.update(events=_events(_post(port, {"text": "a"}))))
    thread.start()
    assert gated.entered.wait(5)
    assert _post(port, {"text": "b"}).status == 429
    gated.release.set()
    thread.join(5)
    assert first["events"][-1]["type"] == "done"


def test_history_carries_across_turns_of_one_session(serve):
    recorder = _Recorder()
    _, port, _ = serve(recorder)
    _events(_post(port, {"text": "first", "session_id": "s1"}))
    _events(_post(port, {"text": "second", "session_id": "s1"}))
    _events(_post(port, {"text": "other", "session_id": "s2"}))
    assert [m["content"] for m in recorder.seen[1] if m["role"] == "user"] == ["first", "second"]
    assert [m["content"] for m in recorder.seen[2] if m["role"] == "user"] == ["other"]


def test_client_disconnect_closes_the_turn_and_frees_the_gate(serve):
    endless = _EndlessProvider()
    _, port, state = serve(endless)
    conn, resp = _open(port, {"text": "go"})
    resp.fp.readline()
    conn.close()
    assert endless.closed.wait(5), "断开后模型流应被关闭"
    deadline = time.monotonic() + 5
    while state.turn_gate.locked() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not state.turn_gate.locked()


def test_cloud_registry_exposes_only_listed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CloudToolRegistry(frozenset()).schemas() == []
    only = CloudToolRegistry(frozenset({"get_market_overview"}))
    assert [schema["name"] for schema in only.schemas()] == ["get_market_overview"]


def test_echo_provider_is_opt_in_and_its_delay_is_capped(monkeypatch):
    monkeypatch.delenv("WYCKOFF_SERVICE_ECHO", raising=False)
    assert default_provider_factory({"provider_name": "echo"})[0] is None

    monkeypatch.setenv("WYCKOFF_SERVICE_ECHO", "1")
    provider, error = default_provider_factory({"provider_name": "echo", "delay_ms": 999_999})
    assert error is None
    assert provider.delay_s == 1.0
