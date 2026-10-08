from __future__ import annotations

from integrations import local_auth


def _point_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(local_auth, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(local_auth, "CONFIG_FILE", tmp_path / "wyckoff.json")
    monkeypatch.setattr(local_auth, "_OLD_CONFIG_FILE", tmp_path / "old-config.json")


def test_auto_relogin_refuses_to_resurrect_after_logout_during_signin(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    local_auth.save_config_key("email", "alice@example.test")
    local_auth.save_config_key("password", "alice-secret")
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )

    def fake_sign_in(email: str, password: str):
        # Desktop auth_logout runs while network sign-in is in flight.
        local_auth.logout()
        return {
            "user_id": "alice",
            "email": email,
            "access_token": "alice-new-at",
            "refresh_token": "alice-new-rt",
        }

    monkeypatch.setattr(local_auth, "_sign_in_with_password", fake_sign_in)

    assert local_auth.auto_relogin() is None
    assert local_auth.load_session() is None
    cfg = local_auth.load_config()
    assert not cfg.get("email")
    assert not cfg.get("password")


def test_auto_relogin_refuses_to_clobber_switched_account_during_signin(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    local_auth.save_config_key("email", "alice@example.test")
    local_auth.save_config_key("password", "alice-secret")
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )

    def fake_sign_in(email: str, password: str):
        local_auth.save_session(
            {
                "user_id": "bob",
                "email": "bob@example.test",
                "access_token": "bob-at",
                "refresh_token": "bob-rt",
            }
        )
        local_auth.save_config_key("email", "bob@example.test")
        local_auth.save_config_key("password", "bob-secret")
        return {
            "user_id": "alice",
            "email": email,
            "access_token": "alice-new-at",
            "refresh_token": "alice-new-rt",
        }

    monkeypatch.setattr(local_auth, "_sign_in_with_password", fake_sign_in)

    assert local_auth.auto_relogin() is None
    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"
    cfg = local_auth.load_config()
    assert cfg.get("email") == "bob@example.test"
    assert cfg.get("password") == "bob-secret"


def test_auto_relogin_persists_when_credentials_unchanged(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    local_auth.save_config_key("email", "alice@example.test")
    local_auth.save_config_key("password", "alice-secret")
    # Token refresh path: session already cleared, credentials still valid.
    monkeypatch.setattr(
        local_auth,
        "_sign_in_with_password",
        lambda email, password: {
            "user_id": "alice",
            "email": email,
            "access_token": "alice-new-at",
            "refresh_token": "alice-new-rt",
        },
    )

    result = local_auth.auto_relogin()

    assert result is not None
    assert result["access_token"] == "alice-new-at"
    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "alice"
    assert disk["refresh_token"] == "alice-new-rt"


def test_logout_clears_credentials_before_session(tmp_path, monkeypatch):
    _point_auth(tmp_path, monkeypatch)
    local_auth.save_config_key("email", "alice@example.test")
    local_auth.save_config_key("password", "alice-secret")
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "at",
            "refresh_token": "rt",
        }
    )
    order: list[str] = []
    real_save_config = local_auth.save_config_key
    real_clear = local_auth.clear_session

    def tracking_save(key, value):
        if key in ("email", "password"):
            order.append(f"cred:{key}")
        return real_save_config(key, value)

    def tracking_clear():
        order.append("session")
        return real_clear()

    monkeypatch.setattr(local_auth, "save_config_key", tracking_save)
    monkeypatch.setattr(local_auth, "clear_session", tracking_clear)

    local_auth.logout()

    assert order.index("cred:email") < order.index("session")
    assert order.index("cred:password") < order.index("session")
