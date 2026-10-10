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
from cli.service.tool_cache import ToolResultCache
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

    def _start(provider: LLMProvider, **state_kwargs: Any) -> tuple[str, int, ServiceState]:
        state = ServiceState(TOKEN, StubToolRegistry(), provider_factory=lambda llm: (provider, None), **state_kwargs)
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


def test_turn_deadline_cancels_a_stalled_turn_and_frees_the_gate(serve):
    gated = _GatedProvider()
    _, port, state = serve(gated, max_turn_seconds=0.6)
    events = _events(_post(port, {"text": "a"}))
    gated.release.set()
    assert events[-1]["type"] == "turn_cancelled"
    assert _wait_for(lambda: state.active_turns == 0)


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
    while state.active_turns and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _wait_for(lambda: state.active_turns == 0)


def test_cloud_registry_exposes_only_listed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CloudToolRegistry(frozenset()).schemas() == []
    only = CloudToolRegistry(frozenset({"get_market_overview"}))
    assert [schema["name"] for schema in only.schemas()] == ["get_market_overview"]


def test_cloud_registry_refuses_tools_outside_the_allowlist(tmp_path, monkeypatch):
    """schemas 过滤不够：Runtime 用 has_tool，未覆盖时 read_file/exec_command 仍可执行。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = CloudToolRegistry(frozenset({"market_regime"}))

    assert registry.has_tool("market_regime") is True
    assert registry.has_tool("read_file") is False
    assert registry.has_tool("exec_command") is False

    prepared = registry.prepare("read_file", {"path": "/etc/passwd"})
    assert prepared.action == "reject" and prepared.code == "tool_not_found"
    assert registry.execute("read_file", {"path": "/etc/passwd"}) == {"error": "未知工具: read_file"}
    assert registry.execute("exec_command", {"command": "id"}) == {"error": "未知工具: exec_command"}


def test_echo_provider_is_opt_in_and_its_delay_is_capped(monkeypatch):
    monkeypatch.delenv("WYCKOFF_SERVICE_ECHO", raising=False)
    assert default_provider_factory({"provider_name": "echo"})[0] is None

    monkeypatch.setenv("WYCKOFF_SERVICE_ECHO", "1")
    provider, error = default_provider_factory({"provider_name": "echo", "delay_ms": 999_999})
    assert error is None
    assert provider.delay_s == 1.0


def _post_ui(port: int, messages: list[dict[str, Any]]) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Wyckoff-User": "u1", "Content-Type": "application/json"}
    body = {"id": "chat-1", "trigger": "submit-message", "messages": messages, "llm": {"provider_name": "echo"}}
    conn.request("POST", "/v1/ui-turns", json.dumps(body), headers)
    return conn.getresponse()


def _chunks(resp: http.client.HTTPResponse) -> list[Any]:
    frames = [line.removeprefix("data: ") for line in resp.read().decode().split("\n\n") if line]
    return [frame if frame == "[DONE]" else json.loads(frame) for frame in frames]


def _user(text: str) -> dict[str, Any]:
    return {"id": "u", "role": "user", "parts": [{"type": "text", "text": text}]}


def test_ui_turn_speaks_the_ai_sdk_stream_protocol(serve):
    _, port, _ = serve(EchoProvider())
    resp = _post_ui(port, [_user("威科夫")])

    assert resp.status == 200
    assert resp.getheader("x-vercel-ai-ui-message-stream") == "v1"
    chunks = _chunks(resp)
    assert chunks[-1] == "[DONE]"
    assert [c["type"] for c in chunks[:-1]][:2] == ["start", "start-step"]
    assert "".join(c["delta"] for c in chunks[:-1] if c["type"] == "text-delta") == "威科夫"
    assert chunks[-2] == {"type": "finish", "finishReason": "stop"}


def test_ui_turn_is_stateless_and_takes_history_from_the_request(serve):
    recorder = _Recorder()
    _, port, state = serve(recorder)
    history = [
        _user("第一问"),
        {"id": "a", "role": "assistant", "parts": [{"type": "text", "text": "第一答"}]},
        _user("第二问"),
    ]

    _chunks(_post_ui(port, history))

    assert [m["content"] for m in recorder.seen[0]] == ["第一问", "第一答", "第二问"]
    assert state.history("u1", "default") == []


def test_ui_turn_rejects_a_conversation_that_does_not_end_with_a_user_message(serve):
    _, port, _ = serve(EchoProvider())
    assistant = {"id": "a", "role": "assistant", "parts": [{"type": "text", "text": "hi"}]}

    assert _post_ui(port, [assistant]).status == 400


class _FailingProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "failing"

    def chat(self, messages, tools, system_prompt=""):
        raise NotImplementedError

    def chat_stream(self, messages, tools, system_prompt="") -> Generator[dict[str, Any], None, None]:
        raise RuntimeError("Error code: 401 - Authentication Fails, your api key is invalid")
        yield {}  # pragma: no cover - 让它成为生成器


def test_ui_turn_surfaces_the_real_provider_failure_to_the_user(serve):
    """用真实 runtime 触发失败，而不是手造事件：turn_failed 把原因放在 message 里，读错字段只会剩一句「agent error」。"""
    _, port, state = serve(_FailingProvider())

    chunks = _chunks(_post_ui(port, [_user("你好")]))

    error = next(c for c in chunks if isinstance(c, dict) and c["type"] == "error")
    assert "401" in error["errorText"] and "api key is invalid" in error["errorText"]
    assert [c["type"] for c in chunks[-3:-1]] == ["finish-step", "finish"]
    assert chunks[-1] == "[DONE]"
    assert _wait_for(lambda: state.active_turns == 0)


# ---- 共用模式 ----------------------------------------------------------------


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def _drain(port: int, body: dict[str, Any], user: str) -> int:
    """读完整个流再返回状态码：连接一断，服务端会（正确地）判定客户端走了并结束这一轮。"""
    response = _post(port, body, user=user)
    status = response.status
    response.read()
    return status


def _run_in_thread(target):
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=target()))
    thread.start()
    return thread, box


def test_shared_mode_serves_different_users_at_the_same_time(serve):
    gated = _GatedProvider()
    _, port, state = serve(gated, shared=True)
    a, box_a = _run_in_thread(lambda: _drain(port, {"text": "a"}, "alice"))
    assert gated.entered.wait(5)
    b, box_b = _run_in_thread(lambda: _drain(port, {"text": "b"}, "bob"))

    assert _wait_for(lambda: state.active_turns == 2)
    gated.release.set()
    a.join(5)
    b.join(5)

    assert (box_a["result"], box_b["result"]) == (200, 200)
    assert _wait_for(lambda: state.active_turns == 0)


def test_shared_mode_still_allows_one_turn_per_user(serve):
    gated = _GatedProvider()
    _, port, _ = serve(gated, shared=True)
    first, _box = _run_in_thread(lambda: _drain(port, {"text": "a"}, "alice"))
    assert gated.entered.wait(5)

    assert _post(port, {"text": "again"}, user="alice").status == 429

    gated.release.set()
    first.join(5)


def test_shared_mode_has_a_global_concurrency_cap(serve):
    gated = _GatedProvider()
    _, port, _ = serve(gated, shared=True, max_concurrent_turns=1)
    first, _box = _run_in_thread(lambda: _drain(port, {"text": "a"}, "alice"))
    assert gated.entered.wait(5)

    response = _post(port, {"text": "b"}, user="bob")

    assert response.status == 429
    assert "capacity" in response.read().decode()
    gated.release.set()
    first.join(5)


def test_single_user_mode_is_unchanged_other_users_are_still_rejected(serve):
    _, port, _ = serve(EchoProvider())
    assert _post(port, {"text": "hi"}, user="alice").status == 200
    assert _post(port, {"text": "hi"}, user="bob").status == 409


def test_shared_mode_keeps_sessions_apart_per_user(serve):
    recorder = _Recorder()
    _, port, _ = serve(recorder, shared=True)

    _events(_post(port, {"text": "alice-secret", "session_id": "s"}, user="alice"))
    _events(_post(port, {"text": "bob-hello", "session_id": "s"}, user="bob"))

    assert [m["content"] for m in recorder.seen[1] if m["role"] == "user"] == ["bob-hello"]


def test_each_request_gets_its_own_tools_with_only_its_own_credentials(serve):
    built: list[tuple[str, dict[str, str]]] = []

    def factory(user: str, credentials: dict[str, str]):
        built.append((user, credentials))
        return StubToolRegistry()

    _, port, _ = serve(EchoProvider(), shared=True, tools_factory=factory)

    _events(_post(port, {"text": "a", "credentials": {"tushare_token": "alice-token"}}, user="alice"))
    _events(_post(port, {"text": "b", "credentials": {"tushare_token": "bob-token"}}, user="bob"))
    _events(_post(port, {"text": "c"}, user="carol"))

    assert built == [
        ("alice", {"tushare_token": "alice-token"}),
        ("bob", {"tushare_token": "bob-token"}),
        ("carol", {}),
    ]


def test_credentials_are_never_echoed_back_to_the_client(serve):
    _, port, _ = serve(EchoProvider(), shared=True, tools_factory=lambda user, creds: StubToolRegistry())

    stream = _post(port, {"text": "hi", "credentials": {"tushare_token": "super-secret-token"}}, user="alice").read()

    assert b"super-secret-token" not in stream


@pytest.mark.parametrize(
    "credentials",
    ["token", ["a"], {"k": 1}, {"k": None}, {f"k{i}": "v" for i in range(21)}, {"k": "x" * 4097}],
)
def test_malformed_credentials_are_rejected(serve, credentials):
    _, port, _ = serve(EchoProvider(), shared=True)

    assert _post(port, {"text": "hi", "credentials": credentials}, user="alice").status == 400


def test_context_variables_written_during_a_turn_do_not_leak_into_the_next_one():
    import contextvars

    from cli.service.server import in_fresh_context

    token = contextvars.ContextVar("token", default="")
    seen: list[str] = []

    def turn(value: str):
        token.set(value)
        yield {"step": 1}
        seen.append(token.get())  # 同一轮的后续步骤仍然看得到自己写的值
        yield {"step": 2}

    list(in_fresh_context(turn("alice")))

    assert seen == ["alice"]
    assert token.get() == ""
    list(in_fresh_context(turn("bob")))
    assert seen == ["alice", "bob"]


def test_shared_mode_refuses_tools_that_cannot_run_in_a_shared_process():
    from cli.service.server import validate_shared_tools

    validate_shared_tools(frozenset({"get_market_overview", "search_stock_by_name"}))
    with pytest.raises(ValueError, match="exec_command"):
        validate_shared_tools(frozenset({"get_market_overview", "exec_command"}))


def test_the_forbidden_list_only_names_tools_that_exist():
    """名单写错一个字，等于那个工具没被拦住。"""
    from cli.service.server import SHARED_FORBIDDEN_TOOLS
    from cli.tools import TOOL_SCHEMAS

    assert SHARED_FORBIDDEN_TOOLS <= {schema["name"] for schema in TOOL_SCHEMAS}


def test_shared_state_is_built_from_the_environment(monkeypatch):
    from cli.service.server import build_state

    monkeypatch.setenv("WYCKOFF_SERVICE_SHARED", "1")
    monkeypatch.setenv("WYCKOFF_SERVICE_MAX_TURNS", "7")
    monkeypatch.setenv("WYCKOFF_SERVICE_TOOLS", "exec_command")
    with pytest.raises(ValueError, match="exec_command"):
        build_state("t")

    monkeypatch.setenv("WYCKOFF_SERVICE_TOOLS", "")
    state = build_state("t")
    assert state.shared and state.max_concurrent_turns == 7 and state.tools is None


def test_the_real_registry_carries_the_request_state_and_never_shares_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alice = CloudToolRegistry(frozenset(), {"user_id": "alice", "credentials": {"tushare_token": "a"}})
    bob = CloudToolRegistry(frozenset(), {"user_id": "bob", "credentials": {}})

    alice.state["credentials"]["extra"] = "x"

    assert alice.tool_context.state["user_id"] == "alice"
    assert bob.tool_context.state["credentials"] == {}
    assert alice.tool_context is not bob.tool_context


# ---- 工具后端 ---------------------------------------------------------------


def _post_tool(port: int, name: str, body: dict[str, Any], *, user: str = "u1", token: str = TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}", "X-Wyckoff-User": user, "Content-Type": "application/json"}
    conn.request("POST", f"/v1/tools/{name}", json.dumps(body), headers)
    response = conn.getresponse()
    return response.status, json.loads(response.read() or b"{}")


def _tool_server(serve, results=None, **state_kwargs):
    registry = StubToolRegistry(tool_results=results or {})
    kwargs = {"shared": True, "tools_factory": lambda user, creds: registry, **state_kwargs}
    kwargs.setdefault("tool_names", frozenset(results or {}))
    _, port, state = serve(EchoProvider(), **kwargs)
    return port, state, registry


def test_tool_endpoint_runs_an_allowlisted_tool_and_returns_its_result(serve):
    port, _, registry = _tool_server(serve, {"market_regime": {"regime": "NEUTRAL", "score": 0.4}})

    status, body = _post_tool(port, "market_regime", {"args": {"x": 1}})

    assert (status, body) == (200, {"result": {"regime": "NEUTRAL", "score": 0.4}, "cached": False})
    assert registry.calls == [{"name": "market_regime", "args": {"x": 1}}]


def test_tool_endpoint_refuses_names_outside_the_allowlist(serve):
    port, _, registry = _tool_server(serve, {"market_regime": {"ok": True}})

    status, _ = _post_tool(port, "exec_command", {"args": {"command": "id"}})

    assert status == 404
    assert registry.calls == []


def test_tool_endpoint_needs_the_gateway_token_and_a_user(serve):
    port, _, _ = _tool_server(serve, {"market_regime": {"ok": True}})

    assert _post_tool(port, "market_regime", {}, token="wrong")[0] == 401
    assert _post_tool(port, "market_regime", {}, user=" ")[0] == 400


@pytest.mark.parametrize("body", [{"args": "x"}, {"args": ["a"]}, {"credentials": "tok"}, {"credentials": {"k": 1}}])
def test_tool_endpoint_rejects_malformed_requests(serve, body):
    port, _, _ = _tool_server(serve, {"market_regime": {"ok": True}})

    assert _post_tool(port, "market_regime", body)[0] == 400


def test_each_tool_call_builds_tools_with_that_requests_credentials(serve):
    built: list[tuple[str, dict[str, str]]] = []

    def factory(user, creds):
        built.append((user, creds))
        return StubToolRegistry(tool_results={"intraday_rescue_check": {"ok": True}})

    _, port, _ = serve(
        EchoProvider(), shared=True, tools_factory=factory, tool_names=frozenset({"intraday_rescue_check"})
    )

    _post_tool(port, "intraday_rescue_check", {"credentials": {"tickflow_api_key": "alice-key"}}, user="alice")
    _post_tool(port, "intraday_rescue_check", {"credentials": {"tickflow_api_key": "bob-key"}}, user="bob")

    assert built == [("alice", {"tickflow_api_key": "alice-key"}), ("bob", {"tickflow_api_key": "bob-key"})]


def test_tool_results_are_strict_json(serve):
    port, _, _ = _tool_server(serve, {"market_regime": {"ratio": float("nan"), "rows": [1.0, float("inf")]}})

    status, body = _post_tool(port, "market_regime", {})

    assert status == 200
    assert body == {"result": {"ratio": None, "rows": [1.0, None]}, "cached": False}


def test_a_failing_tool_is_a_500_and_frees_its_slot(serve):
    def boom(name, args):
        raise RuntimeError("boom")

    port, state, _ = _tool_server(serve, {"market_regime": boom}, max_concurrent_tool_calls=1)

    assert _post_tool(port, "market_regime", {})[0] == 500
    assert _post_tool(port, "market_regime", {})[0] == 500  # 名额释放了，第二次不是 429


def test_tool_calls_have_a_global_concurrency_cap(serve):
    started, release = threading.Event(), threading.Event()

    def slow(name, args):
        started.set()
        release.wait(5)
        return {"ok": True}

    # 关掉缓存：这个用例要的是「两个不同的人同时算」，不是「等同一个结果」。
    port, _, _ = _tool_server(
        serve, {"market_regime": slow}, max_concurrent_tool_calls=1, tool_cache=ToolResultCache({})
    )
    first, box = _run_in_thread(lambda: _post_tool(port, "market_regime", {}, user="alice")[0])
    assert started.wait(5)

    status, _ = _post_tool(port, "market_regime", {}, user="bob")

    assert status == 429
    release.set()
    first.join(5)
    assert box["result"] == 200


def test_a_tool_call_does_not_take_the_users_chat_turn_slot(serve):
    gated = _GatedProvider()
    registry = StubToolRegistry(tool_results={"market_regime": {"ok": True}})
    _, port, _ = serve(gated, shared=True, tools_factory=lambda u, c: registry, tool_names=frozenset({"market_regime"}))
    turn, _box = _run_in_thread(lambda: _drain(port, {"text": "hi"}, "alice"))
    assert gated.entered.wait(5)

    status, _ = _post_tool(port, "market_regime", {}, user="alice")

    assert status == 200
    gated.release.set()
    turn.join(5)


def test_a_market_wide_tool_is_computed_once_and_shared_between_users(serve):
    port, _, registry = _tool_server(serve, {"market_regime": {"regime": "RISK_OFF"}})

    first = _post_tool(port, "market_regime", {"args": {}}, user="alice")
    second = _post_tool(port, "market_regime", {"args": {}}, user="bob")

    assert first == (200, {"result": {"regime": "RISK_OFF"}, "cached": False})
    assert second == (200, {"result": {"regime": "RISK_OFF"}, "cached": True})
    assert len(registry.calls) == 1


def test_a_tool_that_uses_the_users_own_key_is_never_shared(serve):
    port, _, registry = _tool_server(serve, {"intraday_rescue_check": {"ok": True}})

    _post_tool(port, "intraday_rescue_check", {"args": {"code": "600519"}}, user="alice")
    _post_tool(port, "intraday_rescue_check", {"args": {"code": "600519"}}, user="bob")

    assert len(registry.calls) == 2


def test_cache_hits_do_not_take_a_tool_slot(serve):
    started, release = threading.Event(), threading.Event()

    def slow(name, args):
        if args.get("code") == "slow":
            started.set()
            release.wait(5)
        return {"code": args.get("code")}

    port, _, _ = _tool_server(serve, {"wyckoff_diagnose": slow}, max_concurrent_tool_calls=1)
    assert _post_tool(port, "wyckoff_diagnose", {"args": {"code": "600519"}})[0] == 200  # 先写进缓存
    first, box = _run_in_thread(lambda: _post_tool(port, "wyckoff_diagnose", {"args": {"code": "slow"}})[0])
    assert started.wait(5)

    cached = _post_tool(port, "wyckoff_diagnose", {"args": {"code": "600519"}}, user="bob")
    other = _post_tool(port, "wyckoff_diagnose", {"args": {"code": "000001"}}, user="carol")

    assert cached[0] == 200 and cached[1]["cached"] is True
    assert other[0] == 429  # 名额被那个慢的占着，新的计算才会被挡
    release.set()
    first.join(5)
    assert box["result"] == 200


def test_identical_concurrent_requests_share_one_computation(serve):
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def slow(name, args):
        calls.append(name)
        started.set()
        release.wait(5)
        return {"regime": "RISK_OFF"}

    port, _, _ = _tool_server(serve, {"market_regime": slow})
    first, box_a = _run_in_thread(lambda: _post_tool(port, "market_regime", {}, user="alice"))
    assert started.wait(5)
    second, box_b = _run_in_thread(lambda: _post_tool(port, "market_regime", {}, user="bob"))
    time.sleep(0.2)
    release.set()
    first.join(5)
    second.join(5)

    assert len(calls) == 1
    assert box_a["result"][0] == box_b["result"][0] == 200
    assert {box_a["result"][1]["cached"], box_b["result"][1]["cached"]} == {False, True}
