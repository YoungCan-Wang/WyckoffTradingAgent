from __future__ import annotations

from agents import tool_context
from integrations.supabase_portfolio import FillWriteResult


class DummyToolContext:
    def __init__(self, state: dict[str, str]):
        self.state = state


def test_user_client_cache_key_uses_token_digest(monkeypatch):
    tool_context._user_client_cache.clear()
    created: list[tuple[str, str]] = []

    class Client:
        pass

    def fake_create_user_client(access_token: str, refresh_token: str):
        created.append((access_token, refresh_token))
        return Client()

    monkeypatch.setattr("integrations.supabase_base.create_user_client", fake_create_user_client)
    monkeypatch.setattr("integrations.supabase_base.get_session_tokens", lambda _client: ("", ""))

    ctx = DummyToolContext(
        {
            "user_id": "user-1",
            "access_token": "same-jwt-prefix-token-a",
            "refresh_token": "refresh-a",
        }
    )
    first = tool_context.get_user_client(ctx)
    ctx.state["access_token"] = "same-jwt-prefix-token-b"
    second = tool_context.get_user_client(ctx)

    assert first is not second
    assert created == [
        ("same-jwt-prefix-token-a", "refresh-a"),
        ("same-jwt-prefix-token-b", "refresh-a"),
    ]


def test_cached_user_client_refreshes_expired_access_token(monkeypatch):
    """Cache hit must call get_session(); otherwise JWT expires after ~1h (#653 side effect)."""
    tool_context.close_cached_clients()
    created: list[tuple[str, str]] = []
    session_tokens = {"access": "at-1", "refresh": "rt-1"}
    persisted: list[tuple[str, str]] = []

    class Client:
        pass

    def fake_create_user_client(access_token: str, refresh_token: str):
        created.append((access_token, refresh_token))
        return Client()

    def fake_get_session_tokens(_client):
        return session_tokens["access"], session_tokens["refresh"]

    monkeypatch.setattr("integrations.supabase_base.create_user_client", fake_create_user_client)
    monkeypatch.setattr("integrations.supabase_base.get_session_tokens", fake_get_session_tokens)
    monkeypatch.setattr(
        tool_context,
        "_persist_tool_session",
        lambda ctx, expected_refresh_token="": persisted.append(
            (ctx.state.get("access_token", ""), ctx.state.get("refresh_token", ""))
        ),
    )

    ctx = DummyToolContext(
        {
            "user_id": "user-1",
            "access_token": "at-1",
            "refresh_token": "rt-1",
        }
    )
    first = tool_context.get_user_client(ctx)
    assert first is not None
    assert created == [("at-1", "rt-1")]

    # Access JWT expires; GoTrue get_session() rotates AT/RT in memory.
    session_tokens["access"] = "at-2"
    session_tokens["refresh"] = "rt-2"
    second = tool_context.get_user_client(ctx)

    assert second is first
    assert created == [("at-1", "rt-1")]  # no recreate
    assert ctx.state["access_token"] == "at-2"
    assert ctx.state["refresh_token"] == "rt-2"
    assert persisted == [("at-2", "rt-2")]
    # Re-keyed under new AT so the next lookup still hits the same client.
    assert tool_context.get_user_client(ctx) is first
    tool_context.close_cached_clients()


def test_cached_user_client_recreates_when_session_unusable(monkeypatch):
    tool_context.close_cached_clients()
    created: list[tuple[str, str]] = []
    session_tokens_by_call: list[tuple[str, str]] = [
        ("at-1", "rt-1"),  # create path
        ("", ""),  # cache hit: refresh failed
        ("at-2", "rt-2"),  # recreate path
    ]

    class Client:
        pass

    def fake_create_user_client(access_token: str, refresh_token: str):
        created.append((access_token, refresh_token))
        return Client()

    def fake_get_session_tokens(_client):
        return session_tokens_by_call.pop(0)

    monkeypatch.setattr("integrations.supabase_base.create_user_client", fake_create_user_client)
    monkeypatch.setattr("integrations.supabase_base.get_session_tokens", fake_get_session_tokens)
    monkeypatch.setattr("integrations.supabase_base.close_client", lambda _c: None)
    monkeypatch.setattr(tool_context, "_persist_tool_session", lambda *a, **k: None)

    ctx = DummyToolContext(
        {
            "user_id": "user-1",
            "access_token": "at-1",
            "refresh_token": "rt-1",
        }
    )
    first = tool_context.get_user_client(ctx)
    second = tool_context.get_user_client(ctx)

    assert first is not None
    assert second is not None
    assert first is not second
    assert created == [("at-1", "rt-1"), ("at-1", "rt-1")]
    assert ctx.state["access_token"] == "at-2"
    tool_context.close_cached_clients()


def test_with_auth_retry_retries_tuple_auth_failure(monkeypatch):
    new_client = object()
    calls: list[object] = []

    def fake_relogin(_tool_context):
        return new_client, "new-access", "new-refresh"

    def fake_update(*, client):
        calls.append(client)
        if len(calls) == 1:
            return False, "{'message': 'JWT expired', 'code': 'PGRST303'}"
        return True, "ok"

    monkeypatch.setattr(tool_context, "close_cached_clients", lambda: None)
    monkeypatch.setattr(tool_context, "_relogin_and_create_client", fake_relogin)

    ctx = DummyToolContext({"user_id": "user-1", "access_token": "old-access", "refresh_token": "old-refresh"})

    assert tool_context.with_auth_retry(ctx, fake_update, client=object()) == (True, "ok")
    assert calls[-1] is new_client


def test_auth_failure_result_supports_structured_fill_result():
    assert tool_context.is_auth_failure_result(FillWriteResult(False, "JWT expired")) is True
    assert (
        tool_context.is_auth_failure_result(FillWriteResult(False, "持仓已更新但现金写入失败", position_committed=True))
        is False
    )
