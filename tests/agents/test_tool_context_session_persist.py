from __future__ import annotations

from agents import tool_context


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
