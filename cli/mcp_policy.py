"""外部 MCP 工具的读写判定。

MCP 不保证 server 声明哪个工具有副作用（annotations 是可选的），
所以拿不准时按写处理：把读当写只多一次确认，把写当读是静默执行了副作用。
"""

from __future__ import annotations

from typing import Any

# 出现在工具名里就认为有副作用。宁可多命中。
_WRITE_VERBS = (
    "create",
    "update",
    "delete",
    "remove",
    "write",
    "send",
    "post",
    "put",
    "patch",
    "set",
    "add",
    "insert",
    "merge",
    "close",
    "publish",
    "deploy",
    "revoke",
    "grant",
    "upload",
    "execute",
    "exec",
    "run",
    "invoke",
    "move",
    "rename",
    "copy",
    "install",
    "restart",
    "kill",
    "cancel",
    "approve",
    "assign",
    "comment",
)

# 明确的只读动词，仅在 server 没给 annotations 时作为次要信号。
_READ_VERBS = ("get", "list", "read", "search", "find", "query", "fetch", "show", "describe", "view")


def is_write_tool(name: str, annotations: Any = None) -> bool:
    """判断外部工具是否有副作用。未知一律按写。"""
    read_only = _hint(annotations, "readOnlyHint")
    if read_only is True:
        return False
    if _hint(annotations, "destructiveHint") is True:
        return True

    bare = _bare_name(name)
    if _matches(bare, _WRITE_VERBS):
        return True
    if _matches(bare, _READ_VERBS):
        return False
    # 既没有 annotations 也认不出动词——按写处理。
    return True


def classify_external(name: str, annotations: Any = None) -> str:
    """映射到 approval_policy 的档位。外部工具永远不会是 auto。"""
    from cli.approval_policy import REVIEW

    return REVIEW if is_write_tool(name, annotations) else "read"


def _bare_name(name: str) -> str:
    """去掉 mcp__<server>__ 前缀，只看工具本名。"""
    from cli.mcp_config import TOOL_PREFIX

    stripped = name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name
    _, _, tail = stripped.partition("__")
    return (tail or stripped).lower()


def _matches(bare: str, verbs: tuple[str, ...]) -> bool:
    parts = {p for p in bare.replace("-", "_").replace(".", "_").split("_") if p}
    if parts & set(verbs):
        return True
    return any(bare.startswith(verb) for verb in verbs)


def _hint(annotations: Any, field: str) -> bool | None:
    if annotations is None:
        return None
    value = annotations.get(field) if isinstance(annotations, dict) else getattr(annotations, field, None)
    return value if isinstance(value, bool) else None
