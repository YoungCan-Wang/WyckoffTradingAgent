from __future__ import annotations

from cli.approval_executor import execute_approved


def test_execute_approved_uses_registry_and_allows_reviewed_call(monkeypatch):
    calls: dict[str, object] = {}

    class Prepared:
        action = "accept"
        args = {"code": "000001", "stop_loss": 9.1}
        message = ""
        code = ""

    class Registry:
        def __init__(self, **kwargs):
            calls["auth"] = kwargs

        def set_confirm_callback(self, callback):
            calls["confirm"] = callback("set_stop_loss", {})

        def prepare(self, name, args):
            calls["prepare"] = (name, args)
            return Prepared()

        def execute(self, name, args):
            calls["execute"] = (name, args)
            return {"updated_count": 1}

    monkeypatch.setattr("cli.auth.load_session", lambda: {"user_id": "u", "access_token": "a", "refresh_token": "r"})
    monkeypatch.setattr("cli.tools.ToolRegistry", Registry)

    result = execute_approved("set_stop_loss", {"code": "000001", "stop_loss": 9.1})

    assert result == {"updated_count": 1}
    assert calls["auth"] == {"user_id": "u", "access_token": "a", "refresh_token": "r"}
    assert calls["confirm"] == {"action": "allow"}


def test_execute_approved_rejects_account_mismatch(monkeypatch):
    monkeypatch.setattr("cli.auth.load_session", lambda: {"user_id": "bob", "access_token": "a"})
    monkeypatch.setattr(
        "cli.tools.ToolRegistry",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not build registry")),
    )

    result = execute_approved(
        "update_portfolio",
        {"code": "000001", "shares": 100},
        expected_user_id="alice",
    )

    assert "不一致" in result["error"]
