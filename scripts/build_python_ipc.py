"""Build and smoke-test the bundled Python IPC runtime on macOS or Windows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

PYINSTALLER_VERSION = "6.22.2"
UV_VERSION = "0.11.32"


def _python(venv_dir: Path) -> Path:
    name = "python.exe" if os.name == "nt" else "python"
    folder = "Scripts" if os.name == "nt" else "bin"
    return venv_dir / folder / name


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=False)


def _smoke(binary: Path, root: Path) -> None:
    health = subprocess.run(
        [str(binary)],
        cwd=root,
        input='{"id":"smoke","method":"health","params":{}}\n__shutdown__\n',
        text=True,
        capture_output=True,
        check=True,
    )
    events = []
    for line in health.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not any(event.get("type") == "ready" for event in events):
        raise RuntimeError(f"IPC health smoke failed: {health.stdout[-500:]}")
    daemon = subprocess.run([str(binary), "--daemon", "--status"], cwd=root, text=True, capture_output=True, check=True)
    if not daemon.stdout.startswith("daemon:"):
        raise RuntimeError(f"daemon smoke failed: {daemon.stdout[-500:]}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    venv_dir = Path(os.environ.get("WYCKOFF_BUILD_VENV", root / ".build-venv"))
    out_dir = root / "dist" / "python"
    work_dir = root / "dist" / "pywork"
    builder = _python(venv_dir)
    if not builder.exists():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    _run([str(builder), "-m", "pip", "install", f"uv=={UV_VERSION}"], cwd=root)
    sync_env = {**os.environ, "VIRTUAL_ENV": str(venv_dir)}
    subprocess.run(
        [str(builder), "-m", "uv", "sync", "--active", "--frozen", "--no-dev"],
        cwd=root,
        env=sync_env,
        check=True,
    )
    _run(
        [str(builder), "-m", "pip", "install", f"pyinstaller=={PYINSTALLER_VERSION}"],
        cwd=root,
    )
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    _run(
        [
            str(builder),
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(out_dir),
            "--workpath",
            str(work_dir),
            str(root / "packaging" / "wyckoff-ipc.spec"),
        ],
        cwd=root,
    )
    binary = out_dir / "wyckoff-ipc" / ("wyckoff-ipc.exe" if os.name == "nt" else "wyckoff-ipc")
    if not binary.is_file():
        raise FileNotFoundError(f"bundled IPC missing: {binary}")
    _smoke(binary, root)
    size = sum(path.stat().st_size for path in binary.parent.rglob("*") if path.is_file())
    print(f"bundled IPC ready: {binary} ({size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()
