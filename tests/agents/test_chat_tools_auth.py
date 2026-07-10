from __future__ import annotations

from agents import tool_context


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
