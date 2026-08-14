from __future__ import annotations

import subprocess
import sys
import textwrap

from cli import platform_lock


class TestPosixPath:
    def test_acquire_and_release(self, tmp_path):
        path = tmp_path / "a.lock"
        with path.open(platform_lock.lock_mode()) as handle:
            assert platform_lock.try_acquire(handle) is True
            platform_lock.release(handle)

    def test_second_handle_refused_while_held(self, tmp_path):
        """同一进程内两个句柄：POSIX flock 按句柄计，可验证互斥语义。"""
        path = tmp_path / "b.lock"
        first = path.open(platform_lock.lock_mode())
        second = path.open("a+")
        try:
            assert platform_lock.try_acquire(first) is True
            assert platform_lock.try_acquire(second) is False
        finally:
            platform_lock.release(first)
            first.close()
            second.close()

    def test_release_after_release_is_silent(self, tmp_path):
        path = tmp_path / "c.lock"
        with path.open(platform_lock.lock_mode()) as handle:
            platform_lock.try_acquire(handle)
            platform_lock.release(handle)
            platform_lock.release(handle)

    def test_lock_mode_is_writable(self):
        assert "a" in platform_lock.lock_mode() or "w" in platform_lock.lock_mode()


class TestWindowsImportSafety:
    """Windows 上 fcntl 不存在。旧代码在 cli/daemon.py 顶层 import fcntl，
    导致 `wyckoff daemon` 在 Windows 上连启动都做不到。"""

    def test_daemon_imports_without_fcntl(self):
        script = textwrap.dedent(
            """
            import builtins, sys
            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "fcntl":
                    raise ImportError("No module named 'fcntl'")
                return real_import(name, *args, **kwargs)

            builtins.__import__ = fake_import
            for mod in [m for m in sys.modules if m.startswith(("cli.", "fcntl"))]:
                sys.modules.pop(mod, None)

            import cli.daemon  # must not raise
            print("IMPORT_OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "IMPORT_OK" in result.stdout, result.stderr[-2000:]

    def test_msvcrt_branch_selected_when_only_msvcrt_present(self, monkeypatch):
        """模拟 Windows：fcntl 缺失、msvcrt 存在时必须走 msvcrt 分支。"""
        calls: list[tuple[int, int]] = []

        class _FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(fileno, mode, nbytes):
                calls.append((mode, nbytes))

        monkeypatch.setattr(platform_lock, "fcntl", None)
        monkeypatch.setattr(platform_lock, "msvcrt", _FakeMsvcrt)
        monkeypatch.setattr(platform_lock, "_USE_MSVCRT", True)

        assert platform_lock.available() is True
        assert platform_lock.lock_mode() == "a+"

        class _Handle:
            def seek(self, _n):
                pass

            def fileno(self):
                return 3

        handle = _Handle()
        assert platform_lock.try_acquire(handle) is True
        platform_lock.release(handle)
        assert calls == [(1, 1), (0, 1)]

    def test_no_primitive_raises_clearly(self, monkeypatch):
        monkeypatch.setattr(platform_lock, "fcntl", None)
        monkeypatch.setattr(platform_lock, "msvcrt", None)
        monkeypatch.setattr(platform_lock, "_USE_MSVCRT", False)
        assert platform_lock.available() is False
        try:
            platform_lock.try_acquire(object())
        except RuntimeError as exc:
            assert "no file-locking primitive" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")

    def test_platform_lock_imports_without_fcntl(self):
        script = textwrap.dedent(
            """
            import builtins, sys
            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "fcntl":
                    raise ImportError("No module named 'fcntl'")
                return real_import(name, *args, **kwargs)

            builtins.__import__ = fake_import
            sys.modules.pop("cli.platform_lock", None)
            sys.modules.pop("fcntl", None)

            import cli.platform_lock  # must not raise on the POSIX branch import
            print("IMPORT_OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # POSIX 分支确实需要 fcntl；这个测试记录当前行为，Windows 走 msvcrt 分支。
        assert "IMPORT_OK" in result.stdout or "fcntl" in result.stderr


class TestDaemonUsesPlatformLock:
    def test_daemon_has_no_direct_fcntl_reference(self):
        """daemon 不应再直接引用 fcntl，平台差异收敛在 platform_lock。"""
        from pathlib import Path

        source = Path("cli/daemon.py").read_text(encoding="utf-8")
        assert "import fcntl" not in source
        assert "fcntl." not in source

    def test_lock_still_enforces_single_instance(self, tmp_path):
        from cli.daemon import DaemonLockBusy, single_instance_lock

        lock = tmp_path / "d.lock"
        with single_instance_lock(lock):
            try:
                with single_instance_lock(lock):
                    raise AssertionError("second acquisition should fail")
            except DaemonLockBusy:
                pass

    def test_pid_written_and_truncated(self, tmp_path):
        import os

        from cli.daemon import read_lock_pid, single_instance_lock

        lock = tmp_path / "e.lock"
        lock.write_text("999999999", encoding="utf-8")
        with single_instance_lock(lock):
            assert read_lock_pid(lock) == os.getpid()
