from __future__ import annotations

import logging
import pathlib
import re
import shlex
from typing import Any

from utils.http_security import (
    BEARER_RE as BEARER_RE,
)
from utils.http_security import (
    COMMON_SECRET_VALUE_RE as COMMON_SECRET_VALUE_RE,
)
from utils.http_security import (
    SECRET_ASSIGNMENT_RE as SECRET_ASSIGNMENT_RE,
)
from utils.http_security import (
    redact_sensitive_text as redact_sensitive_text,
)
from utils.http_security import (
    security_error as security_error,
)
from utils.http_security import (
    validate_public_http_url as validate_public_http_url,
)

logger = logging.getLogger(__name__)

SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|credential|authorization|cookie|session)",
    re.IGNORECASE,
)

_BLOCKED_PATH_PARTS = {
    ".ssh",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".gnupg",
    ".kube",
    ".docker",
    ".npm",
}
_BLOCKED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_ALLOWED_WYCKOFF_SUBDIRS = {"tool-results", "scratchpad", "exports", "reports"}
_BLOCKED_SYSTEM_ROOTS = (
    pathlib.Path("/bin"),
    pathlib.Path("/etc"),
    pathlib.Path("/Library"),
    pathlib.Path("/private/etc"),
    pathlib.Path("/sbin"),
    pathlib.Path("/System"),
    pathlib.Path("/usr"),
    pathlib.Path("/var"),
)
_BLOCKED_COMMANDS = {
    "bash",
    "chflags",
    "chmod",
    "chown",
    "curl",
    "dd",
    "ftp",
    "env",
    "kill",
    "launchctl",
    "mkfs",
    "nc",
    "ncat",
    "osascript",
    "pkill",
    "printenv",
    "rm",
    "rmdir",
    "rsync",
    "scp",
    "sh",
    "shred",
    "ssh",
    "su",
    "sudo",
    "wget",
    "zsh",
}
_INLINE_CODE_COMMANDS = {"python", "python3", "node", "ruby", "perl", "php"}
_READ_ONLY_COMMANDS = {
    "cat",
    "cut",
    "echo",
    "find",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "sort",
    "tail",
    "uniq",
    "wc",
    "python",
    "python3",
    "node",
    "ruby",
    "perl",
    "php",
}
_MUTATING_FLAGS = {"--delete", "--exec", "--execdir", "--in-place", "-delete", "-exec", "-execdir", "-f", "-i"}
_SHELL_META_RE = re.compile(r"[\n\r;&|<>`]|(?<!\\)\$\(")
_SAFE_WRITE_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
}
_SECRET_FILE_HINT = "不要读取密钥文件；如果凭据已由 CLI/.env 加载，直接运行需要凭据的命令即可继承当前环境。"


def redact_sensitive_columns(df: Any) -> Any:
    """Redact columns whose name looks sensitive; never leak the original frame on failure."""
    try:
        out = df.copy()
    except Exception:
        logger.warning("redact_sensitive_columns: failed to copy frame; refusing to return unredacted data")
        raise
    for col in out.columns:
        try:
            if SENSITIVE_KEY_RE.search(str(col)):
                out[col] = "***REDACTED***"
        except Exception:
            logger.warning("redact_sensitive_columns: failed to inspect column %r; redacting defensively", col)
            out[col] = "***REDACTED***"
    return out


def _path_parts_lower(path: pathlib.Path) -> list[str]:
    return [part.lower() for part in path.parts]


def _is_allowed_wyckoff_path(parts: list[str]) -> bool:
    if ".wyckoff" not in parts:
        return False
    idx = parts.index(".wyckoff")
    return len(parts) > idx + 1 and parts[idx + 1] in _ALLOWED_WYCKOFF_SUBDIRS


def _is_under_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_agent_path(path: str, *, for_write: bool = False) -> pathlib.Path | dict:
    if not path or not str(path).strip():
        return security_error("文件路径不能为空")

    try:
        p = pathlib.Path(path).expanduser().resolve()
    except Exception as e:
        return security_error(f"文件路径无效: {e}")

    parts = _path_parts_lower(p)
    joined = "/".join(parts)
    name = p.name.lower()
    allowed_wyckoff_path = _is_allowed_wyckoff_path(parts)

    if any(_is_under_path(p, root) for root in _BLOCKED_SYSTEM_ROOTS):
        return security_error("禁止访问系统目录")
    if _is_under_path(p, pathlib.Path.home().expanduser() / "Library"):
        return security_error("禁止访问用户 Library 配置目录")
    if ".wyckoff" in parts and not allowed_wyckoff_path:
        return security_error("禁止读取或写入 Wyckoff 凭据、会话和配置目录")
    if any(part in parts for part in _BLOCKED_PATH_PARTS) or any(part in joined for part in _BLOCKED_PATH_PARTS):
        return security_error("禁止访问凭据、密钥或云配置目录")
    if name in _BLOCKED_FILE_NAMES or name.startswith(".env"):
        return security_error(f"禁止访问环境变量或密钥文件。{_SECRET_FILE_HINT}")
    hidden_parts = [part for part in parts if part.startswith(".") and part not in {".", "..", ".wyckoff"}]
    if hidden_parts and not allowed_wyckoff_path:
        return security_error("禁止访问隐藏文件或隐藏目录")
    if SENSITIVE_KEY_RE.search(name):
        return security_error("文件名疑似包含凭据或会话数据")
    if for_write and p.suffix.lower() not in _SAFE_WRITE_SUFFIXES:
        allowed = ", ".join(sorted(_SAFE_WRITE_SUFFIXES))
        return security_error(f"只允许写入文本/报告类文件: {allowed}")
    return p


def _check_inline_interpreter_args(executable: str, rest_args: list[str]) -> dict | None:
    """Block interpreters from running arbitrary code, whether inline (-c/-e) or via a script file.

    Interpreters ignore file extensions, so allowing e.g. ``python notes.txt`` would let an
    agent bypass the write_file suffix allowlist by writing code into a "safe" text file and
    then executing it as a script. Only flag-only invocations (e.g. ``python --version``) pass.
    """
    if any(arg in {"-c", "-e"} for arg in rest_args):
        return security_error("禁止通过 Agent 执行内联代码")
    for arg in rest_args:
        if not arg.startswith("-"):
            return security_error(f"禁止通过 Agent 使用 {executable} 执行脚本文件")
    return None


def validate_agent_command(command: str) -> list[str] | dict:
    raw = str(command or "").strip()
    if not raw:
        return security_error("命令不能为空")
    if len(raw) > 500:
        return security_error("命令过长，请拆成更小的只读操作")
    if _SHELL_META_RE.search(raw):
        return security_error("禁止使用 shell 控制符、管道、重定向、命令替换或多条命令")

    try:
        args = shlex.split(raw)
    except ValueError as e:
        return security_error(f"命令解析失败: {e}")
    if not args:
        return security_error("命令不能为空")

    executable = pathlib.Path(args[0]).name.lower()
    if executable in _BLOCKED_COMMANDS:
        return security_error(f"禁止通过 Agent 执行高风险命令: {executable}")
    if executable not in _READ_ONLY_COMMANDS:
        return security_error(f"Agent 只允许执行明确批准的只读命令: {executable}")
    if executable in _INLINE_CODE_COMMANDS:
        inline_error = _check_inline_interpreter_args(executable, args[1:])
        if inline_error is not None:
            return inline_error
    if any(arg.split("=", 1)[0].lower() in _MUTATING_FLAGS for arg in args[1:]):
        return security_error("禁止通过 Agent 使用可能修改文件的命令参数")
    for arg in args[1:]:
        lowered = arg.lower()
        touches_wyckoff_config = ".wyckoff" in lowered and not any(
            f".wyckoff/{subdir}" in lowered for subdir in _ALLOWED_WYCKOFF_SUBDIRS
        )
        if SENSITIVE_KEY_RE.search(arg) or ".ssh" in lowered or ".env" in lowered or touches_wyckoff_config:
            return security_error(f"命令参数疑似访问凭据、会话或密钥。{_SECRET_FILE_HINT}")
    return args
