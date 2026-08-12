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


class TestMethodTable:
    def test_all_methods_are_generators(self):
        import inspect

        from cli.ipc.methods import METHODS

        for name, fn in METHODS.items():
            assert inspect.isgeneratorfunction(fn), f"{name} must be a generator"

    def test_expected_methods_present(self):
        from cli.ipc.methods import METHODS

        assert {"health", "chat", "approve_list", "approve_decide", "portfolio", "schedules"} <= set(METHODS)

    def test_dispatch_rejects_unknown(self):
        from cli.ipc.methods import MethodError, dispatch

        with pytest.raises(MethodError):
            list(dispatch("nope", {}))
