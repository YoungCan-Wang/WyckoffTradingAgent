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
# cli/service 这个 Agent Runtime HTTP 服务打进 Cloud Run 镜像（见 packaging/agent-service/
# Dockerfile 的 COPY 清单）：比 PyPI 范围更宽（整个 config/、data/ 都带，而非只要 profiles/
# market_universes 子目录），还依赖 uv.lock（PyPI 发布不看锁文件，容器要用它精确复现依赖）。
AGENT_SERVICE_PREFIXES = (
    "agents/",
    "cli/",
    "core/",
    "integrations/",
    "tools/",
    "utils/",
    "workflows/",
    "llmdoc/",
    "config/",
    "data/",
    "packaging/agent-service/",
)
AGENT_SERVICE_FILES = {
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".github/workflows/agent_service_deploy.yml",
}


def _normalize(path: str) -> str:
    # str.lstrip("./") strips a *character set*, not a prefix: on ".github/..." it eats the
    # leading dot too (-> "github/..."), which silently breaks exact-match entries like
    # ".github/workflows/worker_deploy.yml". Strip only a literal "./" prefix instead.
    stripped = path.strip()
    return stripped[2:] if stripped.startswith("./") else stripped


def affects_scope(scope: str, paths: Iterable[str]) -> bool:
    """Return whether at least one normalized repository path affects a scope."""
    clean = {_normalize(path) for path in paths if path.strip()}
    if scope == "pypi":
        return any(path in PYPI_FILES or path.startswith(PYPI_PREFIXES) for path in clean)
    if scope == "worker":
        return any(path in WORKER_FILES or path.startswith(WORKER_PREFIXES) for path in clean)
    if scope == "agent_service":
        return any(path in AGENT_SERVICE_FILES or path.startswith(AGENT_SERVICE_PREFIXES) for path in clean)
    raise ValueError(f"unknown release scope: {scope}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scope", choices=("pypi", "worker", "agent_service"))
    parser.add_argument("paths_file")
    args = parser.parse_args()
    with open(args.paths_file, encoding="utf-8") as handle:
        print("true" if affects_scope(args.scope, handle) else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
