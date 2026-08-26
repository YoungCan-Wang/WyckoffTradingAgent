"""Decide whether changed paths affect a deployable product."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

PYPI_PREFIXES = (
    "agents/",
    "cli/",
    "core/",
    "integrations/",
    "llmdoc/",
    "scripts/",
    "tools/",
    "utils/",
    "workflows/",
    "config/profiles/",
    "data/market_universes/",
)
PYPI_FILES = {
    "CHANGELOG.md",
    "README.md",
    "data/stock_list_cache.json",
    "mcp_server.py",
    "pyproject.toml",
}
WORKER_PREFIXES = ("web/apps/api/", "web/packages/shared/")
WORKER_FILES = {
    ".github/workflows/worker_deploy.yml",
    "web/package.json",
    "web/pnpm-lock.yaml",
    "web/pnpm-workspace.yaml",
}


def affects_scope(scope: str, paths: Iterable[str]) -> bool:
    """Return whether at least one normalized repository path affects a scope."""
    clean = {path.strip().lstrip("./") for path in paths if path.strip()}
    if scope == "pypi":
        return any(path in PYPI_FILES or path.startswith(PYPI_PREFIXES) for path in clean)
    if scope == "worker":
        return any(path in WORKER_FILES or path.startswith(WORKER_PREFIXES) for path in clean)
    raise ValueError(f"unknown release scope: {scope}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scope", choices=("pypi", "worker"))
    parser.add_argument("paths_file")
    args = parser.parse_args()
    with open(args.paths_file, encoding="utf-8") as handle:
        print("true" if affects_scope(args.scope, handle) else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
