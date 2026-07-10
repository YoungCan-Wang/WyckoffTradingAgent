from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYER_IMPORT_RULES = (
    ("core", {"agents", "cli", "integrations", "tools", "web", "workflows"}),
    ("integrations", {"agents", "cli", "tools", "web", "workflows"}),
    ("tools", {"agents", "cli", "web", "workflows"}),
    ("workflows", {"agents", "cli", "web"}),
    ("agents", {"cli", "web"}),
    ("cli", {"web"}),
)
LIBRARY_LAYERS = ("agents", "core", "integrations", "tools")
RUNTIME_LAYERS = ("agents", "cli", "core", "integrations", "tools", "workflows")
RUNTIME_IMPORT_ROOTS = ("cli", "core", "integrations", "tools", "workflows")
CHANNEL_SENDERS = {"send_to_telegram", "send_wecom_notification", "send_dingtalk_notification"}
PRIVATE_MODULE_ALLOWLIST = {"cli.workflows._shared", "integrations._llm_types"}


def _python_files(*locations: Path) -> list[Path]:
    paths: list[Path] = []
    for location in locations:
        candidates = [location] if location.is_file() else location.rglob("*.py")
        paths.extend(path for path in candidates if "__pycache__" not in path.parts)
    return sorted(paths)


def _display_path(path: Path, relative_to: Path = ROOT) -> str:
    try:
        return str(path.relative_to(relative_to))
    except ValueError:
        return path.name


def _imports(path: Path, *, top_level_only: bool = False) -> list[tuple[int, str, set[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = tree.body if top_level_only else ast.walk(tree)
    imports: list[tuple[int, str, set[str]]] = []
    for node in nodes:
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name, set()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module, {alias.name for alias in node.names}))
    return imports


def _scan_import_boundaries(
    paths: list[Path],
    forbidden_roots: set[str],
    *,
    relative_to: Path = ROOT,
    top_level_only: bool = False,
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for lineno, name, _members in _imports(path, top_level_only=top_level_only):
            if name.split(".", 1)[0] in forbidden_roots:
                violations.append(f"{path.relative_to(relative_to)}:{lineno} -> {name}")
    return violations


def _qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _scan_calls(paths: list[Path], forbidden: set[str]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (name := _qualified_name(node.func)) in forbidden:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {name}")
    return violations


def _scan_env_accesses(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        os_names = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "os"
        }
        env_names = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "os"
            for alias in node.names
            if alias.name in {"getenv", "environ"}
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in env_names:
                violations.append(f"{_display_path(path)}:{node.lineno} -> os.{node.func.id}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in os_names and node.attr in {"getenv", "environ"}:
                    violations.append(f"{_display_path(path)}:{node.lineno} -> os.{node.attr}")
            if (
                isinstance(node, ast.Name)
                and node.id == "environ"
                and node.id in env_names
                and node.ctx.__class__ is ast.Load
            ):
                violations.append(f"{_display_path(path)}:{node.lineno} -> os.{node.id}")
    return sorted(violations)


def _scan_cli_entrypoints(paths: list[Path]) -> list[str]:
    violations = _scan_import_boundaries(paths, {"argparse"})
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{path.relative_to(ROOT)}:{node.lineno} -> __main__"
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(isinstance(part, ast.Constant) and part.value == "__main__" for part in ast.walk(node.test))
        )
    return sorted(violations)


def _scan_private_imports(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for lineno, module, members in _imports(path):
            if module.split(".", 1)[0] not in RUNTIME_IMPORT_ROOTS:
                continue
            private_module = next((part for part in module.split(".")[1:] if part.startswith("_")), "")
            if private_module and module not in PRIVATE_MODULE_ALLOWLIST:
                violations.append(f"{_display_path(path)}:{lineno} -> {module}")
            violations.extend(
                f"{_display_path(path)}:{lineno} -> {module}.{member}"
                for member in sorted(members)
                if member.startswith("_")
            )
    return violations


def _scan_notify_aggregator(paths: list[Path], forbidden: set[str]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for lineno, module, _members in _imports(path):
            if module == "utils.notify":
                violations.append(f"{_display_path(path)}:{lineno} -> {module}")
    return violations


def test_import_boundary_scanner_reports_forbidden_import(tmp_path: Path):
    path = tmp_path / "bad.py"
    path.write_text("from workflows.step4_pipeline import run_step4_pipeline\n", encoding="utf-8")

    assert _scan_import_boundaries([path], {"workflows"}, relative_to=tmp_path) == [
        "bad.py:1 -> workflows.step4_pipeline"
    ]


def test_boundary_scanners_cover_aliases_and_private_modules(tmp_path: Path):
    path = tmp_path / "bad.py"
    path.write_text(
        "import os as runtime_os\nfrom os import getenv as read_env\nfrom os import environ\nfrom core import _private\nimport core._math\nruntime_os.getenv('X')\nread_env('X')\nenviron['X']\n",
        encoding="utf-8",
    )

    assert _scan_env_accesses([path]) == ["bad.py:6 -> os.getenv", "bad.py:7 -> os.read_env", "bad.py:8 -> os.environ"]
    assert _scan_private_imports([path]) == ["bad.py:4 -> core._private", "bad.py:5 -> core._math"]


@pytest.mark.parametrize(("layer", "forbidden"), LAYER_IMPORT_RULES)
def test_package_imports_follow_layer_direction(layer: str, forbidden: set[str]):
    assert _scan_import_boundaries(_python_files(ROOT / layer), forbidden) == []


def test_public_mcp_entrypoint_does_not_depend_on_cli():
    paths = _python_files(
        ROOT / "mcp_server.py", *(ROOT / layer for layer in ("agents", "core", "integrations", "tools", "workflows"))
    )

    assert _scan_import_boundaries(paths, {"cli"}) == []


def test_runtime_layers_do_not_depend_on_script_entrypoints():
    paths = _python_files(ROOT / "mcp_server.py", *(ROOT / layer for layer in RUNTIME_LAYERS))

    assert _scan_import_boundaries(paths, {"scripts"}) == []


def test_agents_only_reach_workflows_at_call_time():
    paths = _python_files(ROOT / "agents")

    assert _scan_import_boundaries(paths, {"workflows"}, top_level_only=True) == []


def test_core_uses_explicit_runtime_configuration():
    assert _scan_env_accesses(_python_files(ROOT / "core")) == []


def test_library_layers_do_not_write_console_output_directly():
    paths = _python_files(*(ROOT / layer for layer in LIBRARY_LAYERS))

    assert _scan_calls(paths, {"print"}) == []


def test_integrations_do_not_define_cli_entrypoints():
    assert _scan_cli_entrypoints(_python_files(ROOT / "integrations")) == []


def test_runtime_layers_import_public_runtime_members_only():
    paths = _python_files(
        ROOT / "mcp_server.py",
        *(ROOT / layer for layer in ("agents", "cli", "integrations", "scripts", "workflows")),
    )

    assert _scan_private_imports(paths) == []


def test_notification_callers_use_channel_specific_modules():
    paths = _python_files(*(ROOT / layer for layer in ("agents", "scripts", "workflows")))

    assert _scan_notify_aggregator(paths, CHANNEL_SENDERS) == []


def test_step4_namespace_is_telegram_only():
    paths = [path for path in _python_files(ROOT / "scripts", ROOT / "workflows") if "step4" in path.stem.lower()]
    paths.append(ROOT / "agents" / "strategy_tools.py")
    forbidden_modules = {"utils.feishu", "utils.wecom", "utils.dingtalk"}
    forbidden_tokens = ("feishu", "wecom", "dingtalk", "飞书", "企业微信", "钉钉")
    violations = [
        f"{path.relative_to(ROOT)}:{lineno} -> {module}"
        for path in paths
        for lineno, module, _members in _imports(path)
        if module in forbidden_modules
    ]
    violations.extend(_scan_notify_aggregator(paths, CHANNEL_SENDERS | {"notify_all"}))
    action_paths = sorted((ROOT / ".github" / "workflows").glob("*step4*.yml"))
    violations.extend(
        f"{path.relative_to(ROOT)} -> non-Telegram notification channel"
        for path in [*paths, *action_paths]
        if any(token in path.read_text(encoding="utf-8").lower() for token in forbidden_tokens)
    )

    assert violations == []
