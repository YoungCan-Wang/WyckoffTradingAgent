from __future__ import annotations

import pytest

from agents import tool_context


@pytest.fixture
def no_disk_session(monkeypatch):
    monkeypatch.setattr("integrations.local_auth.load_session", lambda: None)


def _alice_context() -> tool_context.ToolContext:
    return tool_context.ToolContext({"user_id": "alice", "access_token": "at", "refresh_token": "rt"})


def _count_sign_ins(monkeypatch) -> list[str]:
    calls: list[str] = []

    def auto_relogin():
        calls.append("sign-in")

    monkeypatch.setattr("integrations.local_auth.auto_relogin", auto_relogin)
    return calls


def test_frozen_context_does_not_sign_in_while_disk_is_another_account(monkeypatch):
    calls = _count_sign_ins(monkeypatch)
    monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"user_id": "bob"})
    ctx = _alice_context()

    results = [tool_context._relogin_and_create_client(ctx) for _ in range(5)]

    assert calls == []
    assert results == [(None, "", "")] * 5


def test_expired_token_of_same_disk_user_still_signs_in(monkeypatch):
    calls = _count_sign_ins(monkeypatch)
    monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"user_id": "alice"})

    tool_context._relogin_and_create_client(_alice_context())

    assert calls == ["sign-in"]


@pytest.mark.usefixtures("no_disk_session")
def test_missing_disk_session_still_signs_in(monkeypatch):
    calls = _count_sign_ins(monkeypatch)

    tool_context._relogin_and_create_client(_alice_context())

    assert calls == ["sign-in"]


def test_context_without_principal_still_signs_in(monkeypatch):
    calls = _count_sign_ins(monkeypatch)
    monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"user_id": "bob"})

    tool_context._relogin_and_create_client(None)

    assert calls == ["sign-in"]


@pytest.mark.usefixtures("no_disk_session")
def test_relogin_refuses_to_swap_frozen_principal(monkeypatch):
    """Desktop switched to Bob; Alice's frozen MCP context must not become Bob."""

    class Client:
        pass

    monkeypatch.setattr(
        "integrations.local_auth.auto_relogin",
        lambda: {
            "user_id": "bob",
            "email": "bob@example.test",
            "access_token": "bob-at",
            "refresh_token": "bob-rt",
        },
    )
    monkeypatch.setattr("integrations.supabase_base.create_user_client", lambda *_a, **_k: Client())
    monkeypatch.setattr("integrations.supabase_base.get_session_tokens", lambda _c: ("", ""))
    persist_calls: list[object] = []
    monkeypatch.setattr(tool_context, "_persist_tool_session", lambda ctx: persist_calls.append(ctx))

    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-expired-at",
            "refresh_token": "alice-expired-rt",
        }
    )

    client, access, refresh = tool_context._relogin_and_create_client(ctx)

    assert client is None
    assert access == ""
    assert refresh == ""
    assert ctx.state["user_id"] == "alice"
    assert ctx.state["access_token"] == "alice-expired-at"
    assert persist_calls == []


@pytest.mark.usefixtures("no_disk_session")
def test_relogin_keeps_same_user_token_refresh(monkeypatch):
    class Client:
        pass

    monkeypatch.setattr(
        "integrations.local_auth.auto_relogin",
        lambda: {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-new-at",
            "refresh_token": "alice-new-rt",
        },
    )
    monkeypatch.setattr("integrations.supabase_base.create_user_client", lambda *_a, **_k: Client())
    monkeypatch.setattr(
        "integrations.supabase_base.get_session_tokens",
        lambda _c: ("alice-refreshed-at", "alice-refreshed-rt"),
    )
    persist_calls: list[object] = []
    monkeypatch.setattr(tool_context, "_persist_tool_session", lambda ctx: persist_calls.append(ctx))

    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )

    client, access, refresh = tool_context._relogin_and_create_client(ctx)

    assert client is not None
    assert access == "alice-refreshed-at"
    assert refresh == "alice-refreshed-rt"
    assert ctx.state["user_id"] == "alice"
    assert ctx.state["access_token"] == "alice-refreshed-at"
    assert len(persist_calls) == 1


def test_persist_refuses_to_clobber_switched_account(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setattr("integrations.local_auth.SESSION_FILE", session_file)
    from integrations.local_auth import save_session

    save_session(
        {
            "user_id": "bob",
            "email": "bob@example.test",
            "access_token": "bob-at",
            "refresh_token": "bob-rt",
        }
    )
    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-at",
            "refresh_token": "alice-rt",
        }
    )

    tool_context._persist_tool_session(ctx)

    from integrations.local_auth import load_session

    disk = load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"
    assert disk["refresh_token"] == "bob-rt"


def test_persist_refuses_to_resurrect_session_after_logout(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setattr("integrations.local_auth.SESSION_FILE", session_file)
    assert not session_file.exists()
    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "access_token": "alice-at",
            "refresh_token": "alice-rt",
        }
    )

    tool_context._persist_tool_session(ctx)

    assert not session_file.exists()


def test_persist_updates_tokens_for_same_user(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setattr("integrations.local_auth.SESSION_FILE", session_file)
    from integrations.local_auth import load_session, save_session

    save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "old-at",
            "refresh_token": "old-rt",
        }
    )
    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
    )

    tool_context._persist_tool_session(ctx)

    disk = load_session()
    assert disk is not None
    assert disk["user_id"] == "alice"
    assert disk["access_token"] == "new-at"
    assert disk["refresh_token"] == "new-rt"


def test_persist_refuses_without_context_user_id(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setattr("integrations.local_auth.SESSION_FILE", session_file)
    from integrations.local_auth import load_session, save_session

    save_session(
        {
            "user_id": "bob",
            "email": "bob@example.test",
            "access_token": "bob-at",
            "refresh_token": "bob-rt",
        }
    )
    ctx = tool_context.ToolContext(
        {
            "access_token": "env-at",
            "refresh_token": "env-rt",
        }
    )

    tool_context._persist_tool_session(ctx)

    disk = load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"


def test_persist_cannot_overwrite_account_switch_that_waits_on_lock(tmp_path, monkeypatch):
    """Session-lock CAS: auth_login(Bob) waiting on the lock must win after Alice merge.

    The pre-fix check-then-save path released the lock between load and write, so a
    concurrent Bob login could finish first and then be overwritten by Alice's save.
    """
    import threading
    import time

    session_file = tmp_path / "session.json"
    monkeypatch.setattr("integrations.local_auth.SESSION_FILE", session_file)
    from integrations import local_auth

    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )
    real_read = local_auth._read_session_file
    held = threading.Event()

    def slow_read():
        data = real_read()
        held.set()
        time.sleep(0.2)
        return data

    monkeypatch.setattr(local_auth, "_read_session_file", slow_read)
    bob_done = threading.Event()

    def login_bob():
        assert held.wait(timeout=2)
        local_auth.save_session(
            {
                "user_id": "bob",
                "email": "bob@example.test",
                "access_token": "bob-at",
                "refresh_token": "bob-rt",
            }
        )
        bob_done.set()

    thread = threading.Thread(target=login_bob)
    thread.start()
    ctx = tool_context.ToolContext(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-new-at",
            "refresh_token": "alice-new-rt",
        }
    )
    tool_context._persist_tool_session(ctx)
    thread.join(timeout=5)
    assert bob_done.is_set()

    monkeypatch.setattr(local_auth, "_read_session_file", real_read)
    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"
