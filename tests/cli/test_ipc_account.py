"""account / sign_out / settings 的 IPC 方法：重点是不泄露凭据。"""

from __future__ import annotations

from typing import Any

import pytest

from cli.ipc.methods import MethodError, dispatch


def _result(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for event in dispatch(method, params or {}):
        if event.get("type") == "result":
            return event
    raise AssertionError(f"{method} 没有产生 result 事件")


class TestAccount:
    def test_signed_in_reports_email_without_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "integrations.local_auth.load_session",
            lambda: {
                "email": "a@b.com",
                "user_id": "u-1",
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
            },
        )
        result = _result("account")
        assert result["signed_in"] is True
        assert result["email"] == "a@b.com"
        # token 绝不能出现在任何字段里
        assert "secret-access" not in str(result)
        assert "secret-refresh" not in str(result)
        assert "access_token" not in result
        assert "refresh_token" not in result

    def test_no_session_reports_signed_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("integrations.local_auth.load_session", lambda: None)
        result = _result("account")
        assert result["signed_in"] is False
        assert result["email"] == ""

    def test_session_without_token_is_not_signed_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"email": "a@b.com"})
        assert _result("account")["signed_in"] is False


class TestSignOut:
    def test_sign_out_clears_stored_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只删 session 不够：auto_relogin 会用 config 里的 email/password 登回去。"""
        saved: dict[str, Any] = {}
        called: list[str] = []
        monkeypatch.setattr("integrations.local_auth.logout", lambda: called.append("logout"))
        monkeypatch.setattr(
            "integrations.local_auth.load_config",
            lambda: {"email": "a@b.com", "password": "pw", "theme": "light"},
        )
        monkeypatch.setattr(
            "integrations.local_auth.save_config_key",
            lambda key, value: saved.__setitem__(key, value),
        )

        assert _result("sign_out")["signed_out"] is True
        assert called == ["logout"]
        assert saved == {"email": "", "password": ""}
        assert "theme" not in saved


class TestSettings:
    def test_settings_get_hides_api_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "integrations.local_auth.load_model_configs",
            lambda: [{"id": "m1", "model": "x", "provider_name": "p", "api_key": "sk-secret"}],
        )
        monkeypatch.setattr("integrations.local_auth.load_config", lambda: {"tickflow_api_key": "tk-secret"})
        result = _result("settings_get")
        assert "sk-secret" not in str(result)
        assert "tk-secret" not in str(result)
        assert result["models"][0]["has_key"] is True
        assert result["has_tickflow_key"] is True

    def test_settings_set_rejects_unlisted_key(self) -> None:
        with pytest.raises(MethodError) as excinfo:
            list(dispatch("settings_set", {"key": "password", "value": "pw"}))
        assert excinfo.value.code == "invalid_key"

    def test_settings_set_allows_theme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saved: dict[str, Any] = {}
        monkeypatch.setattr(
            "integrations.local_auth.save_config_key",
            lambda key, value: saved.__setitem__(key, value),
        )
        assert _result("settings_set", {"key": "theme", "value": "dark"})["saved"] is True
        assert saved == {"theme": "dark"}

    def test_settings_set_routes_default_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        picked: list[str] = []
        monkeypatch.setattr("integrations.local_auth.set_default_model", lambda mid: picked.append(mid))
        _result("settings_set", {"key": "default_model", "value": "gemini"})
        assert picked == ["gemini"]
