from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# 从测试文件位置推导仓库根，不要写死绝对路径：worktree 一删测试就全挂。
REPO = str(Path(__file__).resolve().parents[2])


def _run_ipc(stdin_text: str, *, extra_setup: str = "", timeout: int = 240):
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {REPO!r})",
            extra_setup,
            "from cli.ipc.stdio import serve",
            "serve()",
        ]
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO,
    )


def _protocol_lines(stdout: str) -> list[dict]:
    lines = []
    for raw in stdout.strip().splitlines():
        if not raw.strip():
            continue
        lines.append(json.loads(raw))
    return lines


class TestProtocol:
    def test_ready_then_result_then_end(self):
        result = _run_ipc('{"id":1,"method":"health"}\n__shutdown__\n')
        events = _protocol_lines(result.stdout)
        assert events[0] == {"type": "ready", "protocol": 1}
        assert events[1]["id"] == 1
        assert events[1]["type"] == "result"
        assert events[1]["ready"] is True
        assert events[-1] == {"id": 1, "type": "end"}

    def test_request_ids_preserved(self):
        result = _run_ipc('{"id":"a","method":"health"}\n{"id":"b","method":"health"}\n__shutdown__\n')
        ids = [e.get("id") for e in _protocol_lines(result.stdout) if "id" in e]
        assert set(ids) == {"a", "b"}

    def test_business_event_cannot_override_stream_id(self):
        setup = "\n".join(
            [
                "from cli.ipc import methods",
                "def event_with_id(_params):",
                '    yield {"type": "approval_pending", "id": "approval-1", "approval_id": "approval-1"}',
                'methods.METHODS["event_with_id"] = event_with_id',
            ]
        )
        result = _run_ipc(
            '{"id":"stream-7","method":"event_with_id"}\n__shutdown__\n',
            extra_setup=setup,
        )
        event = next(e for e in _protocol_lines(result.stdout) if e.get("type") == "approval_pending")
        assert event["id"] == "stream-7"
        assert event["approval_id"] == "approval-1"

    def test_unknown_method_is_error_not_crash(self):
        result = _run_ipc('{"id":1,"method":"nope"}\n__shutdown__\n')
        events = _protocol_lines(result.stdout)
        err = next(e for e in events if e.get("type") == "error")
        assert err["code"] == "unknown_method"
        assert any(e.get("type") == "end" for e in events)

    def test_malformed_json_line_survives(self):
        result = _run_ipc('not json at all\n{"id":1,"method":"health"}\n__shutdown__\n')
        events = _protocol_lines(result.stdout)
        assert any(e.get("code") == "parse_error" for e in events)
        assert any(e.get("type") == "result" for e in events)

    def test_blank_lines_ignored(self):
        result = _run_ipc('\n\n{"id":1,"method":"health"}\n__shutdown__\n')
        assert any(e.get("type") == "result" for e in _protocol_lines(result.stdout))

    def test_eof_exits_cleanly(self):
        result = _run_ipc('{"id":1,"method":"health"}\n')
        assert result.returncode == 0

    def test_non_object_message_rejected(self):
        result = _run_ipc("[1,2,3]\n__shutdown__\n")
        assert any(e.get("code") == "parse_error" for e in _protocol_lines(result.stdout))

    def test_queries_are_not_blocked_by_a_long_request(self):
        setup = "\n".join(
            [
                "import time",
                "from cli.ipc import methods",
                "def slow(_params):",
                "    time.sleep(0.25)",
                '    yield {"type": "result", "slow": True}',
                'methods.METHODS["slow"] = slow',
            ]
        )
        payload = '{"id":"slow","method":"slow"}\n{"id":"health","method":"health"}\n__shutdown__\n'
        events = _protocol_lines(_run_ipc(payload, extra_setup=setup).stdout)
        health_result = next(
            i for i, event in enumerate(events) if event.get("id") == "health" and event["type"] == "result"
        )
        slow_result = next(
            i for i, event in enumerate(events) if event.get("id") == "slow" and event["type"] == "result"
        )
        assert health_result < slow_result


class TestStdoutGuard:
    """stdout 是协议通道。AGENTS.md 允许 cli/ 用 print()，
    任何漏掉的 print 都会让前端解析失败——必须在传输层挡住。"""

    SETUP = "\n".join(
        [
            "from cli.ipc import methods",
            "def noisy(_params):",
            '    print("STRAY_PRINT_MUST_NOT_REACH_STDOUT")',
            '    sys.stdout.write("DIRECT_WRITE_MUST_NOT_REACH_STDOUT\\n")',
            '    yield {"type": "result", "ok": True}',
            'methods.METHODS["noisy"] = noisy',
        ]
    )

    def test_stray_print_does_not_corrupt_protocol(self):
        result = _run_ipc('{"id":1,"method":"noisy"}\n__shutdown__\n', extra_setup=self.SETUP)
        # 每一行 stdout 都必须是合法 JSON，否则前端 readline 解析会炸
        events = _protocol_lines(result.stdout)
        assert any(e.get("ok") is True for e in events)
        assert "STRAY_PRINT" not in result.stdout
        assert "DIRECT_WRITE" not in result.stdout

    def test_stray_print_redirected_to_stderr(self):
        result = _run_ipc('{"id":1,"method":"noisy"}\n__shutdown__\n', extra_setup=self.SETUP)
        assert "STRAY_PRINT_MUST_NOT_REACH_STDOUT" in result.stderr


class TestMethodErrors:
    def test_chat_without_text_is_invalid_params(self):
        result = _run_ipc('{"id":1,"method":"chat","params":{}}\n__shutdown__\n')
        err = next(e for e in _protocol_lines(result.stdout) if e.get("type") == "error")
        assert err["code"] == "invalid_params"

    def test_approve_decide_without_id(self):
        result = _run_ipc('{"id":1,"method":"approve_decide","params":{}}\n__shutdown__\n')
        err = next(e for e in _protocol_lines(result.stdout) if e.get("type") == "error")
        assert err["code"] == "invalid_params"

    def test_approve_decide_unknown_id(self):
        payload = '{"id":1,"method":"approve_decide","params":{"id":"nosuch","approved":true}}'
        result = _run_ipc(payload + "\n__shutdown__\n")
        err = next(e for e in _protocol_lines(result.stdout) if e.get("type") == "error")
        assert err["code"] == "not_actionable"

    def test_params_not_dict_is_tolerated(self):
        result = _run_ipc('{"id":1,"method":"health","params":5}\n__shutdown__\n')
        assert any(e.get("type") == "result" for e in _protocol_lines(result.stdout))


class TestApprovalOwnership:
    def test_approval_nav_uses_session_tool_registry(self, tmp_path, monkeypatch):
        from cli import approval_queue as aq
        from cli.ipc.session import DesktopSession

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        registry = object()
        seen: list[object] = []
        monkeypatch.setattr(
            "cli.headless.current_nav",
            lambda tools: seen.append(tools) or 1_000.0,
        )
        session = DesktopSession()
        session._tools = registry
        session._confirm(
            "update_portfolio",
            {"action": "add", "code": "600519", "shares": 10, "cost_price": 10},
        )

        record = aq.list_pending()[0]
        assert seen == [registry]
        assert record.risk == "confirm"
        assert record.nav_ratio == pytest.approx(0.1)

    def test_desktop_approval_records_the_current_user(self, tmp_path, monkeypatch):
        from cli import approval_queue as aq
        from cli.ipc.session import DesktopSession

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        session = DesktopSession()
        session._user_id = "user-a"
        session._confirm("update_portfolio", {"code": "600519", "action": "buy"})

        record = aq.list_pending()[0]
        assert record.user_id == "user-a"

    def test_each_queued_tool_keeps_its_own_pending_event(self, tmp_path, monkeypatch):
        from cli import approval_queue as aq
        from cli.ipc.session import DesktopSession

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        session = DesktopSession()
        session._confirm("update_portfolio", {"code": "600519", "action": "buy"})
        session._confirm("set_stop_loss", {"code": "600519", "price": 1400})

        assert [item["tool_name"] for item in session._pending_confirms] == [
            "update_portfolio",
            "set_stop_loss",
        ]
        assert all(item["approval_id"] for item in session._pending_confirms)
        assert session._pending_confirms[0]["args"] == {"code": "600519", "action": "buy"}

    def test_mismatched_account_cannot_approve(self, tmp_path, monkeypatch):
        from cli import approval_queue as aq
        from cli import auth
        from cli.ipc import methods

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        approval_id = aq.enqueue(
            "update_portfolio",
            {"code": "600519", "action": "buy"},
            risk="review",
            source="desktop",
            user_id="user-a",
        )
        monkeypatch.setattr(auth, "load_session", lambda: {"user_id": "user-b"})

        with pytest.raises(methods.MethodError, match="所属账户") as error:
            list(methods.approve_decide({"id": approval_id, "approved": True}))

        assert error.value.code == "account_mismatch"
        assert aq.get(approval_id).status == aq.PENDING

    def test_list_only_returns_current_account(self, tmp_path, monkeypatch):
        from cli import approval_queue as aq
        from cli import auth
        from cli.ipc import methods

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        aq.enqueue("update_portfolio", {}, risk="review", source="desktop", user_id="user-a")
        own = aq.enqueue("update_portfolio", {}, risk="review", source="desktop", user_id="user-b")
        monkeypatch.setattr(auth, "load_session", lambda: {"user_id": "user-b"})

        result = list(methods.approve_list({}))[0]

        assert [item["id"] for item in result["items"]] == [own]

    def test_list_exposes_the_risk_reason(self, tmp_path, monkeypatch):
        """界面要显示「为什么需要确认」，这两个字段必须过 IPC 边界。"""
        from cli import approval_queue as aq
        from cli import auth
        from cli.ipc import methods

        monkeypatch.setattr(aq, "DB_PATH", tmp_path / "approvals.db")
        aq.enqueue(
            "update_portfolio",
            {"action": "add", "shares": 100, "cost_price": 1452.0},
            risk="confirm",
            source="desktop",
            user_id="user-a",
            risk_reason="reason.over_nav",
            nav_ratio=0.145,
        )
        monkeypatch.setattr(auth, "load_session", lambda: {"user_id": "user-a"})

        item = list(methods.approve_list({}))[0]["items"][0]

        assert item["risk_reason"] == "reason.over_nav"
        assert item["nav_ratio"] == pytest.approx(0.145)


class TestEventProjection:
    def test_only_whitelisted_fields_pass(self):
        from cli.ipc.session import _project

        event = {"type": "text_delta", "text": "hi", "secret_internal": "leak"}
        assert _project(event) == {"type": "text_delta", "text": "hi"}

    def test_unknown_event_type_reduced_to_type(self):
        from cli.ipc.session import _project

        assert _project({"type": "compaction", "detail": "x"}) == {"type": "compaction"}

    def test_non_dict_event(self):
        from cli.ipc.session import _project

        assert _project("junk") == {"type": "unknown"}

    def test_done_carries_text(self):
        from cli.ipc.session import _project

        out = _project({"type": "done", "text": "done", "rounds": 2, "streamed": True})
        assert out == {"type": "done", "text": "done", "rounds": 2}

    def test_thinking_text_does_not_cross_the_desktop_boundary(self):
        from cli.ipc.session import _project

        assert _project({"type": "thinking_delta", "text": "private chain of thought"}) == {"type": "thinking_delta"}


class TestMethodTable:
    def test_all_methods_are_generators(self):
        import inspect

        from cli.ipc.methods import METHODS

        for name, fn in METHODS.items():
            assert inspect.isgeneratorfunction(fn), f"{name} must be a generator"

    def test_expected_methods_present(self):
        from cli.ipc.methods import METHODS

        assert {
            "health",
            "chat",
            "chat_reset",
            "approve_list",
            "approve_decide",
            "portfolio",
            "schedules",
        } <= set(METHODS)

    def test_dispatch_rejects_unknown(self):
        from cli.ipc.methods import MethodError, dispatch

        with pytest.raises(MethodError):
            list(dispatch("nope", {}))


class TestScheduleRun:
    """手动重跑失败的定时任务。"""

    @pytest.fixture
    def sched(self, monkeypatch):
        from cli import scheduler
        from cli.ipc import methods

        items = [
            scheduler.Schedule(id="s1", name="收盘复盘", cron="30 15 * * 1-5", action="复盘今天"),
            scheduler.Schedule(id="s2", name="空动作", cron="0 9 * * *", action="  "),
        ]
        monkeypatch.setattr(methods, "_rerunning", set())
        monkeypatch.setattr("cli.scheduler.load_schedules", lambda: items)
        return items

    def _run(self, monkeypatch, result):
        from cli.ipc import methods

        calls: list[tuple[str, str, str]] = []

        def fake_run_once(action, *, source="cli", schedule_id="", db_path=None):
            calls.append((action, source, schedule_id))
            return result

        monkeypatch.setattr("cli.headless.run_once", fake_run_once)
        events = list(methods.schedule_run({"id": "s1"}))
        return events, calls

    def test_runs_the_configured_action(self, sched, monkeypatch):
        from cli.headless import HeadlessResult

        events, calls = self._run(monkeypatch, HeadlessResult(ok=True, text="done"))

        assert calls == [("复盘今天", "manual", "s1")]
        assert events[-1]["ok"] is True

    def test_marks_source_manual_not_daemon(self, sched, monkeypatch):
        """人点的重跑要能和无人值守跑出来的区分开。"""
        from cli.headless import HeadlessResult

        _events, calls = self._run(monkeypatch, HeadlessResult(ok=True))

        assert calls[0][1] == "manual"

    def test_surfaces_queued_approvals(self, sched, monkeypatch):
        from cli.headless import HeadlessResult

        events, _calls = self._run(monkeypatch, HeadlessResult(ok=True, queued=["a1", "a2"]))

        assert events[-1]["queued"] == ["a1", "a2"]

    def test_failure_is_reported_not_raised(self, sched, monkeypatch):
        """跑完但失败要带回错误文本，不能当成成功。"""
        from cli.headless import HeadlessResult

        events, _calls = self._run(monkeypatch, HeadlessResult(ok=False, error="数据源超时"))

        assert events[-1]["ok"] is False
        assert "数据源超时" in events[-1]["error"]

    def test_does_not_touch_last_status(self, sched, monkeypatch):
        """last_status 记录排程自动执行的结果，手动重跑不该覆盖它。"""
        from cli.headless import HeadlessResult

        sched[0].last_status = "failed"
        sched[0].last_error = "数据源超时"
        self._run(monkeypatch, HeadlessResult(ok=True))

        assert sched[0].last_status == "failed"
        assert sched[0].last_error == "数据源超时"

    def test_unknown_id(self, sched):
        from cli.ipc.methods import MethodError, schedule_run

        with pytest.raises(MethodError) as error:
            list(schedule_run({"id": "nope"}))
        assert error.value.code == "not_found"

    def test_missing_id(self, sched):
        from cli.ipc.methods import MethodError, schedule_run

        with pytest.raises(MethodError) as error:
            list(schedule_run({}))
        assert error.value.code == "invalid_params"

    def test_empty_action_is_rejected_before_running(self, sched):
        from cli.ipc.methods import MethodError, schedule_run

        with pytest.raises(MethodError) as error:
            list(schedule_run({"id": "s2"}))
        assert error.value.code == "invalid_params"

    def test_concurrent_rerun_is_refused(self, sched, monkeypatch):
        """同一个任务并行跑两轮会写重复的审批和记录。"""
        from cli.ipc import methods

        monkeypatch.setattr(methods, "_rerunning", {"s1"})

        with pytest.raises(methods.MethodError) as error:
            list(methods.schedule_run({"id": "s1"}))
        assert error.value.code == "already_running"

    def test_lock_is_released_after_a_failed_run(self, sched, monkeypatch):
        """run_once 抛异常也要放锁，否则这个任务再也点不动。"""
        from cli.ipc import methods

        def boom(*_args, **_kwargs):
            raise RuntimeError("provider down")

        monkeypatch.setattr("cli.headless.run_once", boom)
        with pytest.raises(RuntimeError):
            list(methods.schedule_run({"id": "s1"}))

        assert "s1" not in methods._rerunning


class TestStdoutGuardAtFdLevel:
    """协议通道必须在 fd 层隔离，不只是换 sys.stdout。

    原生扩展（pandas/numpy/lxml）会直接 write(1, ...)，子进程也会继承 fd 1。
    这两种写入绕过 sys.stdout，落进协议流会让前端 JSON.parse 当场失败 ——
    而且是概率性的，打包分发后基本无法排查。
    """

    def _run(self, body: str, tmp_path) -> tuple[str, str]:
        """在子进程里装好守卫再执行 body，分别返回 stdout / stderr。

        走脚本文件而不是 -c：body 里有多行代码，也更贴近真实启动方式。
        sys.path 要显式插 REPO —— venv 装的是主检出，worktree 的 cli.ipc
        不在默认搜索路径里。
        """
        script = tmp_path / "probe.py"
        script.write_text(
            "import os, sys\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "from cli.ipc.stdio import _install_stdout_guard, _emit\n"
            "_install_stdout_guard()\n" + body + "\n_emit({'type': 'result', 'ok': True})\n",
            encoding="utf-8",
        )
        out = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO,
        )
        return out.stdout, out.stderr

    def test_raw_fd_write_cannot_reach_the_protocol(self, tmp_path) -> None:
        stdout, stderr = self._run("os.write(1, b'RAW-FD-LEAK\\n')", tmp_path)
        assert "RAW-FD-LEAK" not in stdout
        assert "RAW-FD-LEAK" in stderr
        # 协议本身仍要通。
        assert '"ok": true' in stdout

    def test_subprocess_output_cannot_reach_the_protocol(self, tmp_path) -> None:
        """子进程继承 fd 1；dup2 之后它继承到的是 stderr。"""
        stdout, _stderr = self._run("os.system('echo FROM-SUBPROCESS')", tmp_path)
        assert "FROM-SUBPROCESS" not in stdout
        assert '"ok": true' in stdout

    def test_python_print_still_goes_to_stderr(self, tmp_path) -> None:
        stdout, stderr = self._run("print('business output')", tmp_path)
        assert "business output" not in stdout
        assert "business output" in stderr

    def test_every_protocol_line_is_valid_json(self, tmp_path) -> None:
        """前端逐行 parse，混进一行非 JSON 整条连接就废了。"""
        stdout, _stderr = self._run("os.write(1, b'noise\\n'); print('more noise'); os.system('echo shell')", tmp_path)
        for line in stdout.splitlines():
            if line.strip():
                json.loads(line)

    def test_guard_is_idempotent(self, tmp_path) -> None:
        """serve() 可能被重复调用；第二次不该再 dup 一层。"""
        stdout, _stderr = self._run(
            "from cli.ipc.stdio import _install_stdout_guard as g\n"
            "a = g(); b = g()\n"
            "assert a is b, 'guard must return the same channel'",
            tmp_path,
        )
        assert '"ok": true' in stdout


class TestAskUserWithoutTty:
    """没有终端时 ask_user_question 不能去读 stdin。

    IPC 下 stdin 是协议输入流：input() 会吞掉一帧然后永久阻塞工作线程，
    表现为界面卡死且无从排查。
    """

    def test_refuses_instead_of_blocking(self, monkeypatch) -> None:
        import io

        from cli import tools

        monkeypatch.setattr(tools.sys, "stdin", io.StringIO('{"id":"1","method":"health"}\n'))

        def boom(*_a, **_k):
            raise AssertionError("input() must not be called without a tty")

        monkeypatch.setattr("builtins.input", boom)
        result = tools.ask_user_question("要不要买入？")

        assert "无法向用户提问" in result["error"]
        assert "hint" in result

    def test_protocol_stdin_is_not_consumed(self, monkeypatch) -> None:
        """拒绝之后协议输入必须原封不动。"""
        import io

        from cli import tools

        stream = io.StringIO('{"id":"1","method":"health"}\n')
        monkeypatch.setattr(tools.sys, "stdin", stream)
        tools.ask_user_question("问题")

        assert stream.read() == '{"id":"1","method":"health"}\n'
