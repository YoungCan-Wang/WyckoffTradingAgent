from __future__ import annotations

import json
from pathlib import Path

from scripts.check_dependency_hygiene import check_project, main


def _write_minimal_project(root: Path, *, npm_spec: str = "^1.0.0", requirement: str = "requests>=2.31.0") -> None:
    (root / "web").mkdir(parents=True)
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "demo"
version = "0.1.0"
dependencies = ["{requirement}"]
""",
        encoding="utf-8",
    )
    (root / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (root / "web" / "package.json").write_text(
        json.dumps({"dependencies": {"left-pad": npm_spec}}, indent=2),
        encoding="utf-8",
    )


def test_dependency_hygiene_warns_on_ranges_without_errors(tmp_path: Path):
    _write_minimal_project(tmp_path)

    issues = check_project(tmp_path)

    assert any(issue.level == "warning" and "left-pad" in issue.message for issue in issues)
    assert any(issue.level == "warning" and "requests" in issue.message for issue in issues)
    assert not any(issue.level == "error" for issue in issues)
    assert main(["--root", str(tmp_path)]) == 0
    assert main(["--root", str(tmp_path), "--strict"]) == 1


def test_dependency_hygiene_errors_on_missing_lockfiles(tmp_path: Path):
    _write_minimal_project(tmp_path, npm_spec="1.0.0", requirement="requests==2.31.0")
    (tmp_path / "uv.lock").unlink()
    (tmp_path / "web" / "pnpm-lock.yaml").unlink()

    issues = check_project(tmp_path)

    assert sum(1 for issue in issues if issue.level == "error") == 2
    assert main(["--root", str(tmp_path)]) == 1
