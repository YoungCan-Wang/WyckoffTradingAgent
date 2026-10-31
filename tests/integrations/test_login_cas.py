from __future__ import annotations

import pytest

from integrations import local_auth


def _point_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(local_auth, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(local_auth, "CONFIG_FILE", tmp_path / "wyckoff.json")
    monkeypatch.setattr(local_auth, "_OLD_CONFIG_FILE", tmp_path / "old-config.json")


def test_login_refuses_to_resurrect_after_logout_during_signin(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)

    def fake_sign_in(email: str, password: str):
        local_auth.logout()
        return {
            "user_id": "alice",
            "email": email,
            "access_token": "alice-at",
            "refresh_token": "alice-rt",
        }

    monkeypatch.setattr(local_auth, "_sign_in_with_password", fake_sign_in)

    with pytest.raises(RuntimeError, match="退出或另一次登录"):
        local_auth.login("alice@example.test", "alice-secret")

    assert local_auth.load_session() is None
    cfg = local_auth.load_config()
    assert not cfg.get("email")
    assert not cfg.get("password")
    assert not cfg.get(local_auth._LOGIN_ATTEMPT_ID_KEY)


def test_login_refuses_to_clobber_newer_login_during_signin(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    depth = {"n": 0}

    def fake_sign_in(email: str, password: str):
        depth["n"] += 1
        # First (Alice) sign-in: a second auth_login (Bob) wins mid-flight.
        if depth["n"] == 1:
            local_auth.login("bob@example.test", "bob-secret")
        return {
            "user_id": "bob" if email.startswith("bob") else "alice",
            "email": email,
            "access_token": f"{email}-at",
            "refresh_token": f"{email}-rt",
        }

    monkeypatch.setattr(local_auth, "_sign_in_with_password", fake_sign_in)

    with pytest.raises(RuntimeError, match="退出或另一次登录"):
        local_auth.login("alice@example.test", "alice-secret")

    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"].startswith("bob@")
    cfg = local_auth.load_config()
    assert cfg.get("email") == "bob@example.test"
    assert cfg.get("password") == "bob-secret"


def test_login_persists_when_attempt_unchanged(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_auth,
        "_sign_in_with_password",
        lambda email, password: {
            "user_id": "alice",
            "email": email,
            "access_token": "alice-at",
            "refresh_token": "alice-rt",
        },
    )

    result = local_auth.login("alice@example.test", "alice-secret")

    assert result["access_token"] == "alice-at"
    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "alice"
    cfg = local_auth.load_config()
    assert cfg.get("email") == "alice@example.test"
    assert cfg.get("password") == "alice-secret"
    assert not cfg.get(local_auth._LOGIN_ATTEMPT_ID_KEY)
