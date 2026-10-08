from __future__ import annotations

from types import SimpleNamespace

from integrations import local_auth


class _FakeAuth:
    def __init__(self, *, user=None, session=None, error: Exception | None = None):
        self._user = user
        self._session = session
        self._error = error

    def set_session(self, access_token: str, refresh_token: str) -> None:
        if self._error is not None:
            raise self._error

    def get_user(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(user=self._user)

    def get_session(self):
        return self._session


class _FakeClient:
    def __init__(self, auth: _FakeAuth):
        self.auth = auth


def _point_session(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setattr(local_auth, "SESSION_FILE", session_file)
    return session_file


def test_restore_refuses_to_clobber_switched_account(tmp_path, monkeypatch):
    session_file = _point_session(tmp_path, monkeypatch)
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )
    refreshed = SimpleNamespace(access_token="alice-new-at", refresh_token="alice-new-rt")

    def fake_create():
        # Simulate Bob logging in after Alice's restore already loaded disk.
        local_auth.save_session(
            {
                "user_id": "bob",
                "email": "bob@example.test",
                "access_token": "bob-at",
                "refresh_token": "bob-rt",
            }
        )
        return _FakeClient(_FakeAuth(user=SimpleNamespace(id="alice"), session=refreshed))

    monkeypatch.setattr(local_auth, "_create_client", fake_create)

    result = local_auth.restore_session()

    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"
    assert result is not None
    assert result["user_id"] == "bob"
    assert session_file.exists()


def test_restore_refuses_to_resurrect_session_after_logout(tmp_path, monkeypatch):
    _point_session(tmp_path, monkeypatch)
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )
    refreshed = SimpleNamespace(access_token="alice-new-at", refresh_token="alice-new-rt")

    def fake_create():
        local_auth.clear_session()
        return _FakeClient(_FakeAuth(user=SimpleNamespace(id="alice"), session=refreshed))

    monkeypatch.setattr(local_auth, "_create_client", fake_create)

    result = local_auth.restore_session()

    assert result is None
    assert local_auth.load_session() is None


def test_restore_updates_tokens_for_same_user(tmp_path, monkeypatch):
    _point_session(tmp_path, monkeypatch)
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-old-at",
            "refresh_token": "alice-old-rt",
        }
    )
    refreshed = SimpleNamespace(access_token="alice-new-at", refresh_token="alice-new-rt")
    monkeypatch.setattr(
        local_auth,
        "_create_client",
        lambda: _FakeClient(_FakeAuth(user=SimpleNamespace(id="alice"), session=refreshed)),
    )

    result = local_auth.restore_session()

    disk = local_auth.load_session()
    assert result is not None
    assert result["access_token"] == "alice-new-at"
    assert disk is not None
    assert disk["user_id"] == "alice"
    assert disk["access_token"] == "alice-new-at"
    assert disk["refresh_token"] == "alice-new-rt"


def test_restore_invalid_token_does_not_clear_switched_account(tmp_path, monkeypatch):
    _point_session(tmp_path, monkeypatch)
    local_auth.save_session(
        {
            "user_id": "alice",
            "email": "alice@example.test",
            "access_token": "alice-stale-at",
            "refresh_token": "alice-stale-rt",
        }
    )

    def fake_create():
        local_auth.save_session(
            {
                "user_id": "bob",
                "email": "bob@example.test",
                "access_token": "bob-at",
                "refresh_token": "bob-rt",
            }
        )
        return _FakeClient(_FakeAuth(error=RuntimeError("token expired")))

    monkeypatch.setattr(local_auth, "_create_client", fake_create)
    monkeypatch.setattr(local_auth, "auto_relogin", lambda: {"user_id": "should-not-run"})

    result = local_auth.restore_session()

    disk = local_auth.load_session()
    assert disk is not None
    assert disk["user_id"] == "bob"
    assert disk["access_token"] == "bob-at"
    assert result is not None
    assert result["user_id"] == "bob"
