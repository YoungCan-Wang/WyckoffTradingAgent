"""外部 MCP server 配置 — 接入第三方工具。

配置文件即信任边界：接一个 server 等于允许在本机 spawn 它的命令。
所以配置只由用户手写，模型不能新增 server，新增后默认 disabled。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".wyckoff" / "mcp_servers.json"

TOOL_PREFIX = "mcp__"
DEFAULT_TIMEOUT_SECONDS = 30

# 自建 server 的识别特征。它已经内置在原生工具里，再接一遍只会让模型
# 看到两份同名工具，且 MCP 那条路径不过审批闸门。
_BUILTIN_MARKERS = ("mcp_server.py",)


@dataclass
class Server:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def tool_prefix(self) -> str:
        return f"{TOOL_PREFIX}{self.name}__"


def is_builtin_duplicate(server: Server, repo_root: Path | None = None) -> bool:
    """判断这条配置是否指向自建 wyckoff server。"""
    haystack = " ".join([server.command, *server.args])
    if any(marker in haystack for marker in _BUILTIN_MARKERS):
        return True
    root = (repo_root or Path(__file__).resolve().parent.parent).resolve()
    # command 和 args 都要看：自建 server 的启动形式是「仓库里的解释器 + 仓库里的脚本」，
    # 脚本路径出现在 args 里。
    return any(_points_into(token, root) for token in [server.command, *server.args])


def _points_into(token: str, root: Path) -> bool:
    text = str(token).strip()
    # 只看真正像路径的 token。裸命令（npx）按 cwd 展开会把任何命令算成仓库内路径；
    # 包名（@scope/pkg）含斜杠但不是本地路径，展开后同样会误判。
    if not (text.startswith(("/", "~", "./", "../")) or text.endswith(".py")):
        return False
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == root or root in candidate.parents


def load_servers(path: Path | None = None) -> list[Server]:
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("failed to read %s, treating as empty", config_path)
        return []
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return []
    return [parsed for name, entry in servers.items() if (parsed := _parse_server(name, entry))]


def save_servers(servers: list[Server], path: Path | None = None) -> None:
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": {s.name: _server_body(s) for s in servers}}
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        logger.warning("failed to restrict permissions on %s", config_path)


def enabled_servers(path: Path | None = None) -> list[Server]:
    """只返回可以真正连接的 server：已启用且不是自建重复项。"""
    return [s for s in load_servers(path) if s.enabled and not is_builtin_duplicate(s)]


def find_server(name: str, path: Path | None = None) -> Server | None:
    return next((s for s in load_servers(path) if s.name == name), None)


def upsert_server(server: Server, path: Path | None = None) -> None:
    servers = [s for s in load_servers(path) if s.name != server.name]
    servers.append(server)
    save_servers(sorted(servers, key=lambda s: s.name), path)


def remove_server(name: str, path: Path | None = None) -> bool:
    servers = load_servers(path)
    remaining = [s for s in servers if s.name != name]
    if len(remaining) == len(servers):
        return False
    save_servers(remaining, path)
    return True


def set_enabled(name: str, enabled: bool, path: Path | None = None) -> Server | None:
    server = find_server(name, path)
    if server is None:
        return None
    server.enabled = enabled
    upsert_server(server, path)
    return server


def _parse_server(name: str, entry: Any) -> Server | None:
    if not isinstance(entry, dict):
        return None
    command = str(entry.get("command") or "").strip()
    if not command:
        logger.warning("mcp server %s has no command, skipped", name)
        return None
    return Server(
        name=str(name).strip(),
        command=command,
        args=[str(a) for a in entry.get("args") or []],
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        enabled=bool(entry.get("enabled", False)),
        timeout_seconds=_positive_int(entry.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
    )


def _server_body(server: Server) -> dict[str, Any]:
    body = asdict(server)
    body.pop("name", None)
    return body


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
