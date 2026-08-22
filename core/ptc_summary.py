"""Programmatic tool calling: run a tight sandbox and return a summary only."""

from __future__ import annotations

import ast
import statistics
from typing import Any

_MAX_SOURCE_CHARS = 2000
_FORBIDDEN = (
    ast.Import,
    ast.ImportFrom,
    ast.Attribute,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def run_ptc(source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    text = str(source or "").strip()
    if not text:
        return {"ok": False, "error": "empty_source", "summary": ""}
    if len(text) > _MAX_SOURCE_CHARS:
        return {"ok": False, "error": "source_too_long", "summary": ""}
    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"syntax:{exc.msg}", "summary": ""}
    if _contains_forbidden(tree):
        return {"ok": False, "error": "forbidden_ast", "summary": ""}
    scope: dict[str, Any] = {
        "data": dict(payload or {}),
        "mean": statistics.mean,
        "median": statistics.median,
        "pstdev": statistics.pstdev,
        "result": None,
    }
    try:
        exec(compile(tree, "<ptc>", "exec"), {"__builtins__": {}}, scope)  # noqa: S102
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "summary": ""}
    return {"ok": True, "error": "", "summary": _summarize(scope.get("result"))}


def _contains_forbidden(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN):
            return True
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            return True
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return True
    return False


def _summarize(result: Any) -> str:
    if result is None:
        return "result=None"
    if isinstance(result, dict):
        keys = ",".join(sorted(str(key) for key in result)[:8])
        return f"dict keys={keys} n={len(result)}"
    if isinstance(result, (list, tuple)):
        return f"{type(result).__name__} n={len(result)}"
    if isinstance(result, (int, float, bool)):
        return f"{type(result).__name__}={result}"
    text = str(result).replace("\n", " ")
    return text[:240]
