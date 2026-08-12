"""daemon_status / install / uninstall 的 IPC 方法。

定时任务在 GUI 关闭后继续运行，靠的是 launchd 常驻，而不是 GUI 自启。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cli.ipc.methods import MethodError, dispatch


def _result(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for event in dispatch(method, params or {}):
        if event.get("type") == "result":
            return event
    raise AssertionError(f"{method} 没有产生 result 事件")


class TestDaemonStatus:
    def test_reports_not_installed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("cli.ipc.methods._launchd_plist", lambda: tmp_path / "absent.plist")
        monkeypatch.setattr("cli.daemon.is_daemon_running", lambda: False)
        monkeypatch.setattr("sys.platform", "darwin")
        result = _result("daemon_status")
        assert result["installed"] is False
        assert result["loaded"] is False
        assert result["supported"] is True

    def test_installed_and_loaded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        plist = tmp_path / "com.wyckoff.daemon.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr("cli.ipc.methods._launchd_plist", lambda: plist)
        monkeypatch.setattr("cli.daemon.is_daemon_running", lambda: True)
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "ok", ""),
        )
        result = _result("daemon_status")
        assert result["installed"] is True
        assert result["loaded"] is True
        assert result["running"] is True

    def test_non_darwin_reports_unsupported(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("cli.ipc.methods._launchd_plist", lambda: tmp_path / "x.plist")
        monkeypatch.setattr("cli.daemon.is_daemon_running", lambda: False)
        monkeypatch.setattr("sys.platform", "linux")
        assert _result("daemon_status")["supported"] is False


class TestReservedIdKey:
    """传输层用 ``{"id": request_id, **event}`` 打标，方法自带 id 会覆盖请求 ID，
    导致前端永远匹配不上响应。任何方法都不许返回 id 字段。"""

    def test_no_method_yields_reserved_id(self) -> None:
        import inspect

        from cli.ipc import methods as m

        offenders = [
            name
            for name, fn in vars(m).items()
            if callable(fn) and "_ok(id=" in (inspect.getsource(fn) if inspect.isfunction(fn) else "")
        ]
        assert offenders == [], f"这些方法会覆盖请求 ID: {offenders}"


class TestNoInstallMethod:
    def test_install_is_not_exposed(self) -> None:
        """调度进程由桌面应用拉起，不该再提供注册 launchd 的入口。"""
        from cli.ipc.methods import METHODS

        assert "daemon_install" not in METHODS
        assert "daemon_uninstall" in METHODS


class TestDaemonUninstall:
    def test_rejects_non_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(MethodError) as excinfo:
            list(dispatch("daemon_uninstall", {}))
        assert excinfo.value.code == "unsupported"

    def test_removes_plist_even_if_bootout_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """bootout 失败通常只是本来没加载，不该因此留下 plist。"""
        plist = tmp_path / "com.wyckoff.daemon.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("cli.ipc.methods._launchd_plist", lambda: plist)
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "not loaded"),
        )
        assert _result("daemon_uninstall")["installed"] is False
        assert not plist.exists()

    def test_idempotent_when_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("cli.ipc.methods._launchd_plist", lambda: tmp_path / "absent.plist")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        )
        assert _result("daemon_uninstall")["installed"] is False
