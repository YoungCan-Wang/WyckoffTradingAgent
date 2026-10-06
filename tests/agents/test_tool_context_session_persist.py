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
