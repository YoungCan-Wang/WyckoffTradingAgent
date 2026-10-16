from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="install.sh needs bash")

_PYTHON_STUB = """#!/bin/sh
case "$1" in
  -c) echo "3.13" ;;
  --version) echo "Python 3.13.0" ;;
esac
"""

# 只记录参数并造出 install.sh 需要的文件，不联网。
_UV_STUB = """#!/bin/sh
echo "$*" >> "$UV_LOG"
case "$1" in
  --version) echo "uv 0.0.0-test" ;;
  venv)
    for last; do :; done
    mkdir -p "$last/bin"
    printf '#!/bin/sh\\nexit 0\\n' > "$last/bin/python"
    chmod +x "$last/bin/python"
    ;;
  pip)
    venv_python=$4
    touch "$(dirname "$venv_python")/wyckoff"
    ;;
esac
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_install(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_exe(stub_bin / "python3.13", _PYTHON_STUB)
    _write_exe(stub_bin / "uv", _UV_STUB)
    uv_log = tmp_path / "uv.log"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "UV_LOG": str(uv_log),
        "SHELL": "/bin/bash",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return result, stub_bin, uv_log


def test_broken_venv_is_moved_aside_and_rebuilt_with_absolute_interpreter(tmp_path: Path) -> None:
    venv = tmp_path / "home" / ".wyckoff" / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(tmp_path / "gone" / "python3.11", venv / "bin" / "python")

    result, stub_bin, uv_log = _run_install(tmp_path)

    assert result.returncode == 0, result.stderr
    assert len(list(venv.parent.glob("venv.broken-*"))) == 1
    assert f"venv --python {stub_bin / 'python3.13'} {venv}" in uv_log.read_text().splitlines()
    assert (tmp_path / "home" / ".local" / "bin" / "wyckoff").is_symlink()


def test_healthy_venv_is_upgraded_in_place(tmp_path: Path) -> None:
    venv = tmp_path / "home" / ".wyckoff" / "venv"
    (venv / "bin").mkdir(parents=True)
    _write_exe(venv / "bin" / "python", "#!/bin/sh\nexit 0\n")
    (venv / "bin" / "wyckoff").touch()

    result, _, uv_log = _run_install(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not list(venv.parent.glob("venv.broken-*"))
    assert not any(line.startswith("venv ") for line in uv_log.read_text().splitlines())
