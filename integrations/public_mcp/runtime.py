"""Validate and authorize calls before loading optional domain dependencies."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from integrations.public_mcp.contracts import TOOL_BY_NAME, ToolSpec

logger = logging.getLogger(__name__)
MAX_INPUT_BYTES = 65_536
MAX_RESULT_BYTES = 1_048_576
_SECRET_FIELDS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "proxy_authorization",
        "password",
        "secret",
        "client_secret",
        "cookie",
        "set_cookie",
    }
)


@dataclass(frozen=True)
class Outcome:
    data: dict[str, Any]
    is_error: bool = False


def failure(code: str, message: str, *, retryable: bool = False) -> Outcome:
    return Outcome({"status": "error", "code": code, "error": message, "retryable": retryable}, True)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower().replace("-", "_") in _SECRET_FIELDS else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def normalize(result: Any) -> Outcome:
    payload = sanitize(result if isinstance(result, dict) else {"result": result})
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return failure("INVALID_RESULT", "The tool returned a non-JSON result; no result was published.")
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        return failure("RESULT_TOO_LARGE", "Result exceeds 1 MiB. Narrow the query; data was not silently truncated.")
    data = json.loads(text)
    is_error = bool(data.get("error")) or data.get("status") in ("error", "failed") or data.get("success") is False
    return Outcome(data, is_error)


def needs_write_permission(spec: ToolSpec, arguments: dict[str, Any]) -> bool:
    return spec.writes or (spec.name == "research_hypothesis" and arguments.get("action") not in ("list", "detail"))


class Runtime:
    """One local principal per server. No domain import, session load or DB write in __init__."""

    def __init__(self, backend: Callable[[ToolSpec, dict[str, Any]], Any] | None = None) -> None:
        self._backend = backend
        self._lock = threading.Lock()

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Outcome:
        spec = TOOL_BY_NAME.get(name)
        if spec is None:
            return failure("UNKNOWN_TOOL", "Unknown public MCP tool.")
        supplied = {} if arguments is None else arguments
        invalid = self._validate(spec, supplied)
        if invalid:
            return invalid
        args = spec.arguments(supplied)
        if needs_write_permission(spec, args) and os.getenv("WYCKOFF_MCP_ALLOW_WRITES", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            return failure(
                "WRITE_DENIED", "MCP 没有审批环节。请使用 CLI/桌面端，或显式设置 WYCKOFF_MCP_ALLOW_WRITES=1。"
            )
        # The local domain shares SQLite and user configuration. Do not run overlapping calls
        # or abandon a write in a background thread and report it as safely cancelled.
        with self._lock:
            try:
                if self._backend is None:
                    from integrations.public_mcp.backend import DomainBackend

                    self._backend = DomainBackend()
                return normalize(self._backend(spec, deepcopy(args)))
            except ImportError:
                return failure(
                    "MISSING_DEPENDENCY",
                    "A domain dependency is unavailable. Install the full project with its mcp extra.",
                )
            except Exception as exc:
                # Exception strings can contain provider keys, URLs or user records.
                logger.error("public MCP tool %s failed (%s)", name, type(exc).__name__)
                return failure("TOOL_FAILED", "The domain call failed. Check local backend configuration and logs.")

    @staticmethod
    def _validate(spec: ToolSpec, arguments: Any) -> Outcome | None:
        from jsonschema import Draft202012Validator

        try:
            if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_INPUT_BYTES:
                return failure("INPUT_TOO_LARGE", "Arguments exceed 64 KiB.")
        except (TypeError, ValueError, RecursionError):
            return failure("INVALID_ARGUMENTS", "Arguments must be a JSON object with finite numeric values.")
        errors = Draft202012Validator(spec.input_schema()).iter_errors(arguments)
        first = next(errors, None)
        if first is not None:
            # Report the contract location, not the rejected value (which may be a secret).
            location = ".".join(str(part) for part in first.absolute_schema_path)
            return failure("INVALID_ARGUMENTS", f"Arguments do not satisfy inputSchema ({location}).")
        return None
