#!/usr/bin/env python3
"""静态审计：每个 agent 工具沿调用链最终会碰到什么（本机文件、SQLite、环境变量、子进程……）。

用途：决定哪些工具可以放进共用的云端进程。这是**静态、尽力而为**的分析：
- 只追得到能解析的调用（模块级函数、`from x import y`、`mod.func`），`obj.method()` 这类
  动态分派追不到，每个工具会报告追不到的调用数，数字大说明结论偏乐观；
- 函数内的 `from x import y` 也算（工具大量使用延迟导入）；
- 结果只能用来排除风险和定位要改的地方，不能当作「安全」的证明。

用法：.venv/bin/python scripts/audit_cloud_tools.py [--tool NAME] [--chains]
"""

from __future__ import annotations

import argparse
import ast
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("agents", "cli", "core", "integrations", "tools", "utils", "workflows")
MAX_DEPTH = 10

FS_CALLS = {"open", "write_text", "read_text", "write_bytes", "read_bytes", "mkdir", "unlink", "makedirs", "remove"}
FS_FRAME_CALLS = {"to_csv", "to_parquet", "to_pickle", "savefig"}
SUBPROCESS_NAMES = {"subprocess", "Popen"}
NETWORK_ROOTS = {"requests", "httpx", "urllib", "akshare", "tushare", "efinance", "baostock", "anthropic", "openai"}
BROWSER_MODULES = {"integrations.browser_cdp", "integrations.app_browser", "integrations.chart_annotations"}
LOCAL_DB_MODULE = "integrations.local_db"

# 是否可以放进共用进程的判定：影响「按请求隔离」的才算风险。
SHARED_BLOCKERS = {"SUBPROCESS", "BROWSER", "HOME_PATH", "SQLITE_LOCAL", "ENV_WRITE"}
NEEDS_REQUEST_SCOPE = {"ENV_READ", "FS_LOCAL"}


@dataclass
class ModuleInfo:
    name: str
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    functions: dict[str, ast.AST] = field(default_factory=dict)


FuncKey = tuple[str, str]


class Project:
    """按需解析：只读调用链真正走到的模块，而不是把所有文件先解析一遍。"""

    def __init__(self, root: Path = ROOT, scan_dirs: tuple[str, ...] = SCAN_DIRS) -> None:
        self.root = root
        self.scan_dirs = scan_dirs
        self._modules: dict[str, ModuleInfo | None] = {}

    def module(self, name: str) -> ModuleInfo | None:
        if name not in self._modules:
            self._modules[name] = self._load(name)
        return self._modules[name]

    def _load(self, name: str) -> ModuleInfo | None:
        if name.split(".")[0] not in self.scan_dirs:
            return None
        base = self.root / name.replace(".", "/")
        path = base.with_suffix(".py") if base.with_suffix(".py").exists() else base / "__init__.py"
        if not path.exists():
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"))
        info = ModuleInfo(name)
        _collect_imports(tree, info)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.functions.setdefault(node.name, node)
        return info

    def function(self, key: FuncKey) -> ast.AST | None:
        info = self.module(key[0])
        return info.functions.get(key[1]) if info else None


Index = Project


def _collect_imports(tree: ast.AST, info: ModuleInfo) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                info.imports[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                info.imports[(alias.asname or alias.name).split(".")[0]] = (alias.name, None)


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def direct_effects(func: ast.AST, info: ModuleInfo) -> dict[str, str]:
    """函数体自己直接做的事 → 一条证据。"""
    found: dict[str, str] = {}

    def mark(effect: str, evidence: str) -> None:
        found.setdefault(effect, evidence)

    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            _call_effects(node, info, mark)
        elif isinstance(node, ast.Subscript) and _dotted(node.value) == "os.environ":
            mark("ENV_WRITE" if isinstance(node.ctx, ast.Store) else "ENV_READ", "os.environ[...]")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and ".wyckoff" in node.value:
            mark("HOME_PATH", repr(node.value)[:50])
    return found


def _call_effects(node: ast.Call, info: ModuleInfo, mark: Any) -> None:
    dotted = _dotted(node.func)
    leaf = dotted.rsplit(".", 1)[-1]
    root = dotted.split(".")[0]
    if dotted in {"os.getenv", "os.environ.get"}:
        mark("ENV_READ", dotted)
    elif dotted in {"os.putenv", "os.environ.setdefault", "os.environ.update", "os.environ.pop"}:
        mark("ENV_WRITE", dotted)
    elif dotted in {"Path.home", "os.path.expanduser"} or leaf == "expanduser":
        mark("HOME_PATH", dotted)
    elif leaf in FS_CALLS or leaf in FS_FRAME_CALLS or root == "shutil":
        mark("FS_LOCAL", dotted)
    elif root in SUBPROCESS_NAMES or dotted in {"os.system", "os.popen"}:
        mark("SUBPROCESS", dotted)
    elif root == "sqlite3" or _resolves_to(root, info, LOCAL_DB_MODULE):
        mark("SQLITE_LOCAL", dotted)
    elif root in NETWORK_ROOTS or dotted.startswith("urllib"):
        mark("NETWORK", dotted)
    elif leaf in {"get_user_client", "get_credential"}:
        mark("USER_CONTEXT", dotted)


def _resolves_to(name: str, info: ModuleInfo, module: str) -> bool:
    target = info.imports.get(name)
    return bool(target) and (target[0] == module or f"{target[0]}.{target[1]}" == module)


def resolve_call(node: ast.Call, info: ModuleInfo, project: Project) -> FuncKey | None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in info.functions:
            return info.name, func.id
        target = info.imports.get(func.id)
        if target and target[1] and project.function((target[0], target[1])) is not None:
            return target[0], target[1]
        return None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        target = info.imports.get(func.value.id)
        if not target:
            return None
        module = target[0] if target[1] is None else f"{target[0]}.{target[1]}"
        if project.function((module, func.attr)) is not None:
            return module, func.attr
    return None


@dataclass
class Reach:
    effects: dict[str, list[str]] = field(default_factory=dict)
    unresolved: int = 0


class Auditor:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.memo: dict[FuncKey, Reach] = {}

    def reach(self, key: FuncKey, depth: int = 0, stack: tuple[FuncKey, ...] = ()) -> Reach:
        if key in self.memo:
            return self.memo[key]
        if key in stack or depth > MAX_DEPTH:
            return Reach()
        info = self.project.module(key[0])
        func = self.project.function(key)
        if info is None or func is None:
            return Reach()
        result = Reach({effect: [f"{key[0]}:{key[1]} → {ev}"] for effect, ev in direct_effects(func, info).items()})
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                self._follow(node, info, key, depth, stack, result)
        self.memo[key] = result
        return result

    def _follow(
        self, node: ast.Call, info: ModuleInfo, key: FuncKey, depth: int, stack: tuple[FuncKey, ...], result: Reach
    ) -> None:
        callee = resolve_call(node, info, self.project)
        if callee is None:
            result.unresolved += isinstance(node.func, ast.Attribute)
            return
        if callee[0] in BROWSER_MODULES:
            result.effects.setdefault("BROWSER", [f"{key[0]}:{key[1]} → {callee[0]}"])
        sub = self.reach(callee, depth + 1, (*stack, key))
        for effect, chain in sub.effects.items():
            result.effects.setdefault(effect, [f"{key[0]}:{key[1]}", *chain])
        result.unresolved += sub.unresolved


def load_tools() -> dict[str, FuncKey]:
    sys.path.insert(0, str(ROOT))
    from cli.tools import ToolRegistry

    tools: dict[str, FuncKey] = {}
    for name, func in ToolRegistry()._tools.items():
        target = inspect.unwrap(func)
        module = inspect.getmodule(target)
        tools[name] = (module.__name__ if module else "?", getattr(target, "__name__", name))
    return tools


def classify(effects: set[str]) -> str:
    if effects & {"SUBPROCESS", "BROWSER"}:
        return "sandbox-only"
    if effects & SHARED_BLOCKERS:
        return "needs-fix"
    if effects & NEEDS_REQUEST_SCOPE:
        return "review"
    return "candidate"


def render(rows: list[tuple[str, FuncKey, Reach]], show_chains: bool) -> str:
    out = ["| 工具 | 判定 | 沿调用链碰到 | 追不到的调用 |", "|---|---|---|---:|"]
    for name, _, reach in rows:
        effects = set(reach.effects)
        shown = ", ".join(sorted(effects)) or "—"
        out.append(f"| `{name}` | {classify(effects)} | {shown} | {reach.unresolved} |")
    if show_chains:
        out.append("")
        for name, _, reach in rows:
            for effect in sorted(reach.effects):
                out.append(f"- `{name}` {effect}: {' → '.join(reach.effects[effect])[:300]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tool", help="只审计一个工具")
    parser.add_argument("--chains", action="store_true", help="输出每条结论的调用链证据")
    args = parser.parse_args(argv)
    auditor = Auditor(Project())
    rows = []
    for name, key in sorted(load_tools().items()):
        if args.tool and name != args.tool:
            continue
        if auditor.project.function(key) is None:
            rows.append((name, key, Reach(unresolved=-1)))
            continue
        rows.append((name, key, auditor.reach(key)))
    print(render(rows, args.chains))
    return 0


if __name__ == "__main__":
    sys.exit(main())
