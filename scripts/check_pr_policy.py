#!/usr/bin/env python3
"""Validate pull request body and changed-file policy gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SUMMARY_HEADINGS = {
    "summary",
    "problem",
    "goal",
    "fix summary",
    "change summary",
    "变更摘要",
    "摘要",
    "目标",
    "问题",
}
VALIDATION_HEADINGS = {
    "validation",
    "test plan",
    "tests",
    "results",
    "验证",
    "测试",
    "测试计划",
    "结果",
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bBearer\s+eyJ[A-Za-z0-9_.-]+"),
    re.compile(r"\b(?:ANTHROPIC|OPENAI|OPENROUTER|GEMINI|TUSHARE|SUPABASE)_[A-Z0-9_]*KEY\s*="),
)
DANGEROUS_FILENAMES = {".env", ".env.local", "id_rsa", "id_ed25519"}
DANGEROUS_SUFFIXES = {".db", ".dump", ".key", ".log", ".pem", ".sqlite", ".sqlite3"}
DANGEROUS_PREFIXES = ("logs/", ".traces/", "artifacts/")
DEPENDENCY_FILE_NAMES = {
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "uv.lock",
    "requirements.txt",
}
DEPENDABOT_LOGINS = {"dependabot[bot]", "dependabot-preview[bot]"}


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]


def _heading_names(body: str) -> set[str]:
    headings: set[str] = set()
    for line in body.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            headings.add(match.group(1).strip().lower())
    return headings


def _dangerous_files(changed_files: list[str]) -> list[str]:
    dangerous: list[str] = []
    for raw_path in changed_files:
        path = raw_path.strip()
        if not path:
            continue
        lower_path = path.lower()
        name = Path(path).name.lower()
        suffix = Path(path).suffix.lower()
        if name in DANGEROUS_FILENAMES:
            dangerous.append(path)
        elif suffix in DANGEROUS_SUFFIXES:
            dangerous.append(path)
        elif lower_path.startswith(DANGEROUS_PREFIXES):
            dangerous.append(path)
    return dangerous


def _dependency_only_change(changed_files: list[str]) -> bool:
    if not changed_files:
        return False
    for raw_path in changed_files:
        path = raw_path.strip()
        if not path:
            continue
        if Path(path).name not in DEPENDENCY_FILE_NAMES and not path.endswith(".lock"):
            return False
    return True


def validate_policy(
    body: str,
    changed_files: list[str],
    *,
    automated_dependency_pr: bool = False,
) -> PolicyResult:
    failures: list[str] = []
    warnings: list[str] = []
    body = body.strip()
    if not body:
        return PolicyResult(ok=False, failures=("PR body is empty",), warnings=())

    headings = _heading_names(body)
    if not automated_dependency_pr:
        if not headings.intersection(SUMMARY_HEADINGS):
            failures.append("PR body is missing a Summary/变更摘要 section")
        if not headings.intersection(VALIDATION_HEADINGS):
            failures.append("PR body is missing a Validation/验证 section")

    for pattern in SECRET_PATTERNS:
        if pattern.search(body):
            failures.append("PR body appears to contain a secret or bearer token")
            break

    dangerous = _dangerous_files(changed_files)
    if dangerous:
        failures.append("PR includes local logs, trace artifacts, database dumps, or secret-like files")
        warnings.append("Blocked files: " + ", ".join(dangerous[:8]))

    return PolicyResult(ok=not failures, failures=tuple(failures), warnings=tuple(warnings))


def _load_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        data = json.loads(Path(event_path).read_text(encoding="utf-8"))
        body = data.get("pull_request", {}).get("body")
        if isinstance(body, str):
            return body
    raise SystemExit("error: provide --body, --body-file, or --event-path with pull_request.body")


def _load_event(args: argparse.Namespace) -> dict:
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    return json.loads(Path(event_path).read_text(encoding="utf-8"))


def _is_dependabot_event(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    pull_request = event.get("pull_request", {})
    pr_user = pull_request.get("user", {}) if isinstance(pull_request, dict) else {}
    sender = event.get("sender", {})
    logins = [
        pr_user.get("login") if isinstance(pr_user, dict) else "",
        sender.get("login") if isinstance(sender, dict) else "",
    ]
    return any(str(login or "").lower() in DEPENDABOT_LOGINS for login in logins)


def _load_changed_files(args: argparse.Namespace) -> list[str]:
    if args.changed_file:
        return args.changed_file
    if args.changed_files_file:
        return [
            line.strip()
            for line in Path(args.changed_files_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", help="PR body text")
    parser.add_argument("--body-file", help="File containing the PR body")
    parser.add_argument("--event-path", help="GitHub event JSON path")
    parser.add_argument("--changed-files-file", help="File containing changed paths, one per line")
    parser.add_argument("--changed-file", action="append", default=[], help="Changed path; repeatable")
    args = parser.parse_args(argv)

    changed_files = _load_changed_files(args)
    event = _load_event(args)
    result = validate_policy(
        _load_body(args),
        changed_files,
        automated_dependency_pr=_is_dependabot_event(event) and _dependency_only_change(changed_files),
    )
    if result.ok:
        print("PR Policy: PASS")
        for warning in result.warnings:
            print(f"  WARN {warning}")
        return 0

    print("PR Policy: FAIL")
    for failure in result.failures:
        print(f"  FAIL {failure}")
    for warning in result.warnings:
        print(f"  INFO {warning}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
