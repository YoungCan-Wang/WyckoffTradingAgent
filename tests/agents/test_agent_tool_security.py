from __future__ import annotations

import socket

import pandas as pd
import pytest

from agents.local_tools import exec_command, read_file, web_fetch, write_file
from agents.tool_security import redact_sensitive_columns, validate_public_http_url
from cli.tools import TOOL_SCHEMAS


def test_exec_command_allows_simple_read_only_command():
    result = exec_command("echo hello")

    assert result["returncode"] == 0
    assert "hello" in result["stdout"]


def test_exec_command_runs_in_validated_working_directory(tmp_path):
    result = exec_command("pwd", cwd=str(tmp_path))

    assert result["returncode"] == 0
    assert result["cwd"] == str(tmp_path)
    assert str(tmp_path) in result["stdout"]


def test_exec_command_blocks_sensitive_working_directory(tmp_path):
    hidden = tmp_path / ".secrets"
    hidden.mkdir()

    result = exec_command("pwd", cwd=str(hidden))

    assert result["error"].startswith("安全拦截")
    assert "隐藏" in result["error"]


def test_exec_command_schema_exposes_working_directory():
    schema = next(item for item in TOOL_SCHEMAS if item["name"] == "exec_command")

    assert "继承当前 CLI 进程环境变量" in schema["description"]
    assert "不要读取 .env" in schema["description"]
    cwd = schema["parameters"]["properties"]["cwd"]
    assert cwd["type"] == "string"
    assert "项目根目录" in cwd["description"]
    assert "安全校验" in cwd["description"]


def test_exec_command_blocks_shell_control_operators():
    result = exec_command("echo hello; cat ~/.wyckoff/config.json")

    assert result["error"].startswith("安全拦截")
    assert "shell 控制符" in result["error"]


def test_exec_command_blocks_destructive_command():
    result = exec_command("rm -rf /tmp/wyckoff-agent-security-test")

    assert result["error"].startswith("安全拦截")
    assert "高风险命令" in result["error"]


def test_exec_command_blocks_inline_code():
    result = exec_command("python -c 'print(123)'")

    assert result["error"].startswith("安全拦截")
    assert "内联代码" in result["error"]


def test_exec_command_blocks_interpreter_running_script_file(tmp_path):
    target = tmp_path / "payload.txt"
    target.write_text("print('hi')", encoding="utf-8")

    result = exec_command(f"python {target}")

    assert result["error"].startswith("安全拦截")
    assert "脚本文件" in result["error"]


def test_exec_command_allows_interpreter_flag_only_invocation():
    result = exec_command("python3 --version")

    assert result["returncode"] == 0


def test_exec_command_blocks_environment_dump():
    result = exec_command("printenv")

    assert result["error"].startswith("安全拦截")
    assert "高风险命令" in result["error"]


def test_exec_command_blocks_wyckoff_config_path():
    result = exec_command("ls ~/.wyckoff/config.json")

    assert result["error"].startswith("安全拦截")
    assert "凭据" in result["error"] or "会话" in result["error"]


def test_exec_command_blocks_env_file_with_inherited_env_hint(tmp_path):
    target = tmp_path / ".env"
    target.write_text("PYPI_TOKEN=secret", encoding="utf-8")

    result = exec_command("ls .env", cwd=str(tmp_path))

    assert result["error"].startswith("安全拦截")
    assert "不要读取密钥文件" in result["error"]
    assert "继承当前环境" in result["error"]


def test_read_file_blocks_sensitive_path_name(tmp_path):
    target = tmp_path / "api_key.txt"
    target.write_text("api_key=secret", encoding="utf-8")

    result = read_file(str(target))

    assert result["error"].startswith("安全拦截")
    assert "凭据" in result["error"]


def test_read_file_blocks_env_file_with_inherited_env_hint(tmp_path):
    target = tmp_path / ".env"
    target.write_text("PYPI_TOKEN=secret", encoding="utf-8")

    result = read_file(str(target))

    assert result["error"].startswith("安全拦截")
    assert "不要读取密钥文件" in result["error"]
    assert "继承当前环境" in result["error"]


def test_read_file_redacts_secret_assignments(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("normal=1\napi_key=secret-value\n", encoding="utf-8")

    result = read_file(str(target))

    assert "normal=1" in result["content"]
    assert "secret-value" not in result["content"]
    assert "***REDACTED***" in result["content"]


def test_read_file_blocks_system_file():
    result = read_file("/etc/passwd")

    assert result["error"].startswith("安全拦截")
    assert "系统目录" in result["error"]


def test_read_file_blocks_hidden_directory(tmp_path):
    hidden = tmp_path / ".cache" / "notes.txt"
    hidden.parent.mkdir()
    hidden.write_text("not a secret", encoding="utf-8")

    result = read_file(str(hidden))

    assert result["error"].startswith("安全拦截")
    assert "隐藏" in result["error"]


def test_write_file_allows_report_file(tmp_path):
    target = tmp_path / "report.md"

    result = write_file(str(target), "# report")

    assert result["path"] == str(target)
    assert target.read_text(encoding="utf-8") == "# report"


def test_write_file_blocks_executable_suffix(tmp_path):
    target = tmp_path / "run.py"

    result = write_file(str(target), "print('hi')")

    assert result["error"].startswith("安全拦截")
    assert not target.exists()


def test_web_fetch_blocks_localhost():
    result = web_fetch("http://127.0.0.1/")

    assert result["error"].startswith("安全拦截")
    assert "内网" in result["error"] or "本机" in result["error"]


def test_validate_public_url_allows_proxy_fake_ip_for_domain(monkeypatch):
    url = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.0.98", port))],
    )

    assert validate_public_http_url(url) == url


def test_validate_public_url_blocks_proxy_fake_ip_literal():
    result = validate_public_http_url("https://198.18.0.98/")

    assert result["error"].startswith("安全拦截")
    assert "内网" in result["error"] or "保留地址" in result["error"]


def test_web_fetch_blocks_non_http_scheme():
    result = web_fetch("file:///etc/passwd")

    assert result["error"].startswith("安全拦截")
    assert "http/https" in result["error"]


def test_redact_sensitive_columns_masks_columns_by_name():
    df = pd.DataFrame({"code": ["000001"], "api_key": ["sk-secret"], "password": ["hunter2"]})

    out = redact_sensitive_columns(df)

    assert out["code"].iloc[0] == "000001"
    assert out["api_key"].iloc[0] == "***REDACTED***"
    assert out["password"].iloc[0] == "***REDACTED***"


def test_redact_sensitive_columns_leaves_unrelated_columns_untouched():
    df = pd.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    out = redact_sensitive_columns(df)

    assert out["code"].iloc[0] == "000001"
    assert out["name"].iloc[0] == "平安银行"


def test_redact_sensitive_columns_raises_instead_of_leaking_original_frame_when_copy_fails():
    class ExplodingFrame:
        def copy(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        redact_sensitive_columns(ExplodingFrame())
