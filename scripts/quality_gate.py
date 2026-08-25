#!/usr/bin/env python3
"""Quality gate: function length check + LOC trend tracking.

Usage:
  .venv/bin/python scripts/quality_gate.py --snapshot       Generate baseline (LOC + function whitelist)
  .venv/bin/python scripts/quality_gate.py --check-functions [--verbose] Check function length against whitelist
  .venv/bin/python scripts/quality_gate.py --ci [--verbose] Full CI check (functions + LOC trend)
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = ROOT / ".metrics"
LOC_FILE = METRICS_DIR / "loc.json"
WHITELIST_FILE = METRICS_DIR / "func_whitelist.json"
SOFT_FUNC_TARGET_LINES = 50
DEFAULT_FUNC_LIMIT = 70
FUNC_LIMIT_BY_PREFIX = (
    ("web/packages/shared/src/", 70),
    ("web/apps/web/src/routes/", 120),
    ("web/apps/web/src/components/", 90),
    ("web/apps/", 90),
    ("scripts/", 100),
    ("cli/", 100),
)
LOC_GROWTH_WARN_PCT = 5
STALE_WHITELIST_WARN_COUNT = 10
REPORT_DETAIL_LIMIT = 12

PY_DIRS = ["agents", "app", "cli", "core", "integrations", "pages", "scripts", "tools", "utils", "workflows"]
TS_DIRS = ["web/apps/web/src", "web/apps/api/src", "web/packages/shared/src"]


# -- function scanning --


def _count_func_lines(node: ast.AST) -> int:
    lines = set()
    for child in ast.walk(node):
        if hasattr(child, "lineno") and hasattr(child, "end_lineno"):
            lines.update(range(child.lineno, child.end_lineno + 1))
    return len(lines)


def function_line_limit(filepath: str) -> int:
    normalized = filepath.replace("\\", "/")
    for prefix, limit in FUNC_LIMIT_BY_PREFIX:
        if normalized.startswith(prefix):
            return limit
    return DEFAULT_FUNC_LIMIT


def _limit_summary() -> str:
    parts = [
        f"default={DEFAULT_FUNC_LIMIT}",
        *(f"{prefix.rstrip('/')}={limit}" for prefix, limit in FUNC_LIMIT_BY_PREFIX),
    ]
    return ", ".join(parts)


def scan_py_functions(dirs: list[str]) -> list[tuple[str, str, int, int]]:
    results = []
    for d in dirs:
        for path in sorted((ROOT / d).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    n = _count_func_lines(node)
                    rel = str(path.relative_to(ROOT))
                    limit = function_line_limit(rel)
                    if n > limit:
                        results.append((rel, node.name, n, limit))
    return results


TS_FUNC_RE = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",
    re.MULTILINE,
)


def scan_ts_functions(dirs: list[str]) -> list[tuple[str, str, int, int]]:
    results = []
    for d in dirs:
        dp = ROOT / d
        if not dp.exists():
            continue
        for path in sorted(dp.rglob("*.ts")) + sorted(dp.rglob("*.tsx")):
            lines = path.read_text(encoding="utf-8").splitlines()
            i = 0
            while i < len(lines):
                m = TS_FUNC_RE.search(lines[i])
                if m:
                    name = m.group(1) or m.group(2) or "anonymous"
                    depth, start = 0, i
                    found_open = False
                    for j in range(i, len(lines)):
                        depth += lines[j].count("{") - lines[j].count("}")
                        if "{" in lines[j]:
                            found_open = True
                        if found_open and depth <= 0:
                            length = j - start + 1
                            rel = str(path.relative_to(ROOT))
                            limit = function_line_limit(rel)
                            if length > limit:
                                results.append((rel, name, length, limit))
                            i = j
                            break
                    else:
                        break
                i += 1
    return results


# -- LOC counting --


def count_loc(dirs: list[str], extensions: list[str]) -> dict[str, int]:
    result = {}
    for d in dirs:
        dp = ROOT / d
        if not dp.exists():
            continue
        total = 0
        for ext in extensions:
            for path in dp.rglob(f"*{ext}"):
                total += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        result[d] = total
    return result


def generate_loc_metrics() -> dict:
    py = count_loc(PY_DIRS, [".py"])
    ts = count_loc(TS_DIRS, [".ts", ".tsx"])
    return {
        "total_python_loc": sum(py.values()),
        "total_ts_loc": sum(ts.values()),
        "by_module": {**py, **ts},
        "generated_at": __import__("datetime").date.today().isoformat(),
    }


# -- whitelist management --


def _make_key(filepath: str, funcname: str) -> str:
    return f"{filepath}::{funcname}"


def _print_limited(rows: list[tuple], *, verbose: bool) -> None:
    displayed = rows if verbose else rows[:REPORT_DETAIL_LIMIT]
    for row in displayed:
        print(f"  {'  '.join(str(part) for part in row)}")
    hidden = len(rows) - len(displayed)
    if hidden:
        print(f"  ... {hidden} more; rerun with --verbose for the full list")


def load_whitelist() -> dict[str, int]:
    if not WHITELIST_FILE.exists():
        return {}
    return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))


def save_whitelist(wl: dict[str, int]) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    WHITELIST_FILE.write_text(json.dumps(wl, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# -- commands --


def cmd_snapshot() -> int:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    metrics = generate_loc_metrics()
    LOC_FILE.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    all_violations = scan_py_functions(PY_DIRS) + scan_ts_functions(TS_DIRS)
    current = {_make_key(f, fn): n for f, fn, n, _limit in all_violations}

    old_wl = load_whitelist()
    new_wl: dict[str, int] = {}
    for key, val in current.items():
        if key in old_wl:
            new_wl[key] = min(old_wl[key], val)
        else:
            new_wl[key] = val
    save_whitelist(new_wl)

    removed = set(old_wl) - set(current)
    improved = sum(1 for k in new_wl if k in old_wl and new_wl[k] < old_wl[k])

    print(f"LOC baseline: Python={metrics['total_python_loc']}  TS={metrics['total_ts_loc']}")
    print(f"Function whitelist: {len(new_wl)} functions tracked")
    if removed:
        print(f"  Removed {len(removed)} entries (functions no longer over limit)")
    if improved:
        print(f"  Ratcheted down {improved} entries (functions got shorter)")
    return 0


def cmd_check_no_sql_files(*, verbose: bool = False) -> int:
    """本项目不保留 .sql 文件——schema 以 Python 常量版本化，由脚本打印后人工执行。

    这条约束此前只是口头共识、无任何自动检查，结果 2026-08-22 引入了
    docs/sql/factor_ic_daily.sql 才被发现。改成 CI 拦截。
    """
    import subprocess

    out = subprocess.run(["git", "ls-files", "*.sql"], capture_output=True, text=True, check=False).stdout
    hits = [line for line in out.splitlines() if line.strip()]
    if hits:
        print("FAIL: 仓库不应包含 .sql 文件（schema 请用 Python 常量 + 打印脚本）：")
        for path in hits:
            print(f"  {path}")
        return 1
    if verbose:
        print("OK: 无 .sql 文件")
    return 0


def cmd_check_functions(*, verbose: bool = False) -> int:
    wl = load_whitelist()
    all_v = scan_py_functions(PY_DIRS) + scan_ts_functions(TS_DIRS)
    active_keys = {_make_key(filepath, funcname) for filepath, funcname, _lines, _limit in all_v}

    new_violations = []
    worsened = []
    unchanged = []
    for filepath, funcname, lines, limit in all_v:
        key = _make_key(filepath, funcname)
        if key not in wl:
            new_violations.append((filepath, funcname, lines, limit))
        elif lines > wl[key]:
            worsened.append((filepath, funcname, wl[key], lines, limit))
        else:
            unchanged.append((filepath, funcname, lines, limit))

    has_error = False

    if new_violations:
        has_error = True
        print(f"ERROR: {len(new_violations)} NEW functions exceed layer-specific line limits:")
        _print_limited(
            [(f, f"{fn}()", f"{n} lines > {limit}") for f, fn, n, limit in new_violations],
            verbose=verbose,
        )

    if worsened:
        has_error = True
        print(f"ERROR: {len(worsened)} whitelisted functions got longer:")
        _print_limited(
            [(f, f"{fn}()", f"{old} -> {new} lines (limit {limit})") for f, fn, old, new, limit in worsened],
            verbose=verbose,
        )

    if unchanged:
        print(f"WARNING: {len(unchanged)} legacy functions still over limit (whitelisted):")
        _print_limited(
            [(f, f"{fn}()", f"{n} lines (limit {limit})") for f, fn, n, limit in unchanged],
            verbose=verbose,
        )

    if has_error:
        print(
            "\nFAILED: Split long functions to their layer limit, or at least keep whitelisted legacy functions "
            "from growing, before committing. "
            f"Soft target is ≤ {SOFT_FUNC_TARGET_LINES}; hard limits: {_limit_summary()}."
        )
        return 1

    active_legacy = sum(1 for key in wl if key in active_keys)
    stale_legacy = len(wl) - active_legacy
    stale_note = f", {stale_legacy} stale whitelist entries" if stale_legacy else ""
    print(f"OK: No new violations. ({active_legacy} active legacy functions over limit{stale_note})")
    if stale_legacy >= STALE_WHITELIST_WARN_COUNT:
        print(
            f"WARNING: stale whitelist entries reached {stale_legacy}; "
            "run --snapshot after large refactors to keep debt visible."
        )
    return 0


def cmd_ci(*, verbose: bool = False) -> int:
    exit_code = cmd_check_functions(verbose=verbose)

    metrics = generate_loc_metrics()
    print(f"\nLOC: Python={metrics['total_python_loc']}  TS={metrics['total_ts_loc']}")
    if LOC_FILE.exists():
        baseline = json.loads(LOC_FILE.read_text(encoding="utf-8"))
        for key in ("total_python_loc", "total_ts_loc"):
            base_val = baseline.get(key, 0)
            if base_val == 0:
                continue
            growth = (metrics[key] - base_val) / base_val * 100
            if growth > LOC_GROWTH_WARN_PCT:
                print(f"  WARNING: {key} grew {growth:.1f}% ({base_val} -> {metrics[key]})")

    return exit_code


def main() -> int:
    args = set(sys.argv[1:])
    verbose = "--verbose" in args or "-v" in args
    if "--snapshot" in args:
        return cmd_snapshot()
    if "--ci" in args:
        return cmd_ci(verbose=verbose)
    if "--check-functions" in args:
        rc = cmd_check_functions(verbose=verbose)
        return rc or cmd_check_no_sql_files(verbose=verbose)
    if "--check-no-sql" in args:
        return cmd_check_no_sql_files(verbose=True)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
