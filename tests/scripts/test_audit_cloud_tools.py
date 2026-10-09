"""云端工具静态审计：能沿调用链追到间接效应，且不会在环里死循环。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.audit_cloud_tools import Auditor, Project, classify


def _project(tmp_path: Path, files: dict[str, str]) -> Project:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return Project(tmp_path, ("agents", "integrations"))


def _effects(project: Project, module: str, func: str) -> dict[str, list[str]]:
    return Auditor(project).reach((module, func)).effects


def test_direct_effects_are_detected(tmp_path):
    project = _project(
        tmp_path,
        {
            "agents/t.py": (
                "import os, subprocess\n"
                "from pathlib import Path\n"
                "def tool():\n"
                "    os.getenv('A'); os.environ['B'] = '1'; subprocess.run(['ls'])\n"
                "    Path('x').write_text('y'); Path.home()\n"
            )
        },
    )

    assert set(_effects(project, "agents.t", "tool")) == {
        "ENV_READ",
        "ENV_WRITE",
        "SUBPROCESS",
        "FS_LOCAL",
        "HOME_PATH",
    }


def test_effects_are_found_through_helpers_in_other_modules_with_the_chain(tmp_path):
    project = _project(
        tmp_path,
        {
            "agents/t.py": "from integrations.h import helper\ndef tool():\n    return helper()\n",
            "integrations/h.py": "import os\ndef helper():\n    os.environ['TOKEN'] = 'x'\n",
        },
    )

    chain = _effects(project, "agents.t", "tool")["ENV_WRITE"]

    assert chain[0] == "agents.t:tool"
    assert chain[-1].startswith("integrations.h:helper")


def test_function_local_imports_and_module_aliases_are_followed(tmp_path):
    project = _project(
        tmp_path,
        {
            "agents/t.py": "def tool():\n    from integrations import local_db\n    return local_db.load_portfolio('p')\n",
            "integrations/local_db.py": "import sqlite3\ndef load_portfolio(pid):\n    return sqlite3.connect('x')\n",
        },
    )

    assert "SQLITE_LOCAL" in _effects(project, "agents.t", "tool")


def test_call_cycles_terminate(tmp_path):
    project = _project(
        tmp_path,
        {"agents/t.py": "import os\ndef a():\n    return b()\ndef b():\n    os.getenv('X')\n    return a()\n"},
    )

    assert "ENV_READ" in _effects(project, "agents.t", "a")


def test_unresolvable_method_calls_are_counted_not_guessed(tmp_path):
    project = _project(tmp_path, {"agents/t.py": "def tool(obj):\n    obj.go()\n    obj.stop()\n"})

    reach = Auditor(project).reach(("agents.t", "tool"))

    assert reach.effects == {} and reach.unresolved == 2


def test_only_modules_on_the_call_chain_are_parsed(tmp_path):
    project = _project(
        tmp_path,
        {"agents/t.py": "def tool():\n    return 1\n", "agents/broken.py": "def (:\n"},
    )

    assert _effects(project, "agents.t", "tool") == {}


@pytest.mark.parametrize(
    ("effects", "expected"),
    [
        (set(), "candidate"),
        ({"NETWORK", "USER_CONTEXT"}, "candidate"),
        ({"ENV_READ"}, "review"),
        ({"FS_LOCAL"}, "review"),
        ({"SQLITE_LOCAL"}, "needs-fix"),
        ({"ENV_WRITE", "NETWORK"}, "needs-fix"),
        ({"SUBPROCESS", "ENV_READ"}, "sandbox-only"),
        ({"BROWSER"}, "sandbox-only"),
    ],
)
def test_classification(effects, expected):
    assert classify(effects) == expected


def test_the_real_shell_tool_is_sandbox_only():
    """审计对真实代码仍然有用：能跑命令的工具必须被判进沙箱。"""
    effects = set(Auditor(Project()).reach(("agents.local_tools", "exec_command")).effects)

    assert classify(effects) == "sandbox-only"
