"""Internal Wyckoff Tool Surface for tool verification, auditing, and execution."""

from __future__ import annotations

import contextvars
import inspect
import json
import logging
import re
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

SUPPORTED_TOOL_SURFACE_SCOPE_DIMENSIONS = frozenset({"stock"})
_SUMMARY_LIMIT = 500
_TIMEOUT_EXEMPT_TOOLS = frozenset(
    {"ask_user_question", "delegate_to_research", "delegate_to_analysis", "delegate_to_trading"}
)

_TOKEN_PATTERN = re.compile(r"(?i)\b(?:sk|pk|ghp|gho|github_pat|xox[baprs]?|bearer)[-_a-z0-9]{12,}\b")
_AUTH_PATTERN = re.compile(
    r"(?i)\b((?:proxy[-_]?authorization|authorization)\s*[:=]\s*)"
    r"[^\s,;\"']+(?:\s+[^\s,;\"']+)?"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE)
_HEADER_SECRET_PATTERN = re.compile(r"(?i)\b(api[-_]?key|token|secret|cookie|set-cookie)\b\s*[:=]\s*[^\s,;]+")
_QUOTED_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(?:authorization|proxy-authorization|api[-_]?key|x-api-key|token|access[-_]?token|"
    r"refresh[-_]?token|secret|client[-_]?secret|password|passwd|cookie|set-cookie)[\"']?\s*[:=]\s*)([\"']).*?(\2)"
)
_HOME_PATH_PATTERN = re.compile(r"(/Users/[^/\s]+|/home/[^/\s]+)(/[^\s,;]*)?")

_SECRET_KEY_NAMES = {
    "authorization",
    "proxy_authorization",
    "api_key",
    "apikey",
    "x_api_key",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "cookie",
    "set_cookie",
}
_SECRET_KEY_MARKERS = ("api_key", "apikey", "token", "secret", "password", "passwd", "cookie")
_JSON_TYPE_TO_PYTHON = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


@dataclass
class ToolParameter:
    name: str
    type: str  # "string" | "number" | "integer" | "boolean" | "array" | "object"
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None
    accepted_types: tuple[type, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ToolPolicy:
    read_only: bool | None = None
    side_effects: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    policy_status: str = "unknown"
    scope_dimensions: list[str] = field(default_factory=list)

    @classmethod
    def unknown(cls) -> ToolPolicy:
        return cls()

    @classmethod
    def declared(
        cls,
        *,
        read_only: bool,
        side_effects: list[str] | None = None,
        permissions: list[str] | None = None,
        scope_dimensions: list[str] | None = None,
    ) -> ToolPolicy:
        return cls(
            read_only=read_only,
            side_effects=list(side_effects or []),
            permissions=list(permissions or []),
            policy_status="declared",
            scope_dimensions=list(scope_dimensions or []),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "side_effects": list(self.side_effects),
            "permissions": list(self.permissions),
            "policy_status": self.policy_status,
        }


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: list[ToolParameter]
    handler: Callable
    category: str = "data"
    policy: ToolPolicy = field(default_factory=ToolPolicy.unknown)

    def _params_json_schema(self) -> dict:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def _descriptor_json_schema(self) -> dict:
        schema = self._params_json_schema()
        schema.setdefault("required", [])
        schema["additionalProperties"] = self.accepts_extra_arguments()
        return schema

    def accepts_extra_arguments(self) -> bool:
        try:
            sig = inspect.signature(self.handler)
        except (TypeError, ValueError):
            return False
        return any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._params_json_schema(),
            },
        }

    def to_public_descriptor(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": self._descriptor_json_schema(),
            "policy": self.policy.to_public_dict(),
            "scope": {
                "scope_dimensions": list(self.policy.scope_dimensions),
                "requires_stock_scope": "stock" in self.policy.scope_dimensions,
            },
        }

    def to_mcp_descriptor(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self._descriptor_json_schema(),
        }


@dataclass
class ToolAccessContext:
    stock_scope: Any = None
    market: str | None = None
    time_range: dict | None = None
    data_sources: list[str] | None = None
    backend: str | None = None
    session_id: str | None = None
    timeout_seconds: float | None = None
    max_result_bytes: int | None = None
    audit_context: dict[str, Any] = field(default_factory=dict)


def serialize_tool_result(result: Any) -> str:
    if result is None:
        return json.dumps({"result": None})
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    if hasattr(result, "__dict__"):
        try:
            d = {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
            return json.dumps(d, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def _normalize_tool_stock_code(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip().upper()
    if not text:
        return text
    try:
        from integrations.tushare_client import normalize_stock_code

        return normalize_stock_code(text)
    except Exception:
        return text


def _normalize_guard_stock_code(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    raw = value if isinstance(value, str) else str(value)
    normalized = _normalize_tool_stock_code(raw)
    return normalized if isinstance(normalized, str) else str(normalized)


def _normalize_secret_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _is_secret_key(key: Any) -> bool:
    normalized = _normalize_secret_key(key)
    if not normalized:
        return False
    if normalized in _SECRET_KEY_NAMES:
        return True
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_structured_secrets(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return "<redacted_depth_limit>"
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_secret_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_structured_secrets(item, _depth=_depth + 1)
        return redacted
    if isinstance(value, list):
        return [_redact_structured_secrets(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_redact_structured_secrets(item, _depth=_depth + 1) for item in value]
    return value


def _redact_json_string_if_possible(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return text
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    try:
        return json.dumps(_redact_structured_secrets(parsed), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return text


def redact_diagnostic_value(value: Any, *, limit: int = _SUMMARY_LIMIT) -> str:
    try:
        if isinstance(value, str):
            text = _redact_json_string_if_possible(value)
        else:
            text = json.dumps(_redact_structured_secrets(value), ensure_ascii=False, default=str)
    except Exception:
        try:
            text = str(value)
        except Exception:
            text = "<unserializable>"

    text = _AUTH_PATTERN.sub(r"\1[REDACTED]", text)
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", text)
    text = _QUOTED_SECRET_FIELD_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(3)}", text)
    text = _HEADER_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = _HOME_PATH_PATTERN.sub(
        lambda m: f"{m.group(1).rsplit('/', 1)[0] if '/' in m.group(1) else m.group(1)}/[REDACTED_PATH]", text
    )
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


def build_tool_audit(
    *,
    tool_name: str,
    arguments: Any,
    result: Any = None,
    error_code: str | None = None,
    duration: float = 0.0,
    context: ToolAccessContext | None = None,
) -> dict[str, Any]:
    ctx = context or ToolAccessContext()
    payload = {
        "tool_name": tool_name,
        "arguments_summary": redact_diagnostic_value(arguments),
        "duration": round(duration, 4),
        "result_summary": redact_diagnostic_value(result),
        "error_code": error_code,
        "backend": ctx.backend,
        "session_id": ctx.session_id,
    }
    if ctx.audit_context:
        payload["audit_context"] = redact_diagnostic_value(ctx.audit_context)
    return payload


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    if max_bytes <= 0:
        return "", True
    marker = "<truncated>"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    prefix = raw[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{marker}", True


def _public_payload_from_result_text(result_text: str) -> Any:
    try:
        return json.loads(result_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return result_text


class ToolSurface:
    """Internal tool schema and execution surface for Wyckoff Analysis."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool_def: ToolDefinition) -> None:
        self._tools[tool_def.name] = tool_def

    def resolve(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self, format: str = "public") -> list[dict]:
        normalized = (format or "public").strip().lower()
        if normalized == "openai":
            return [tool_def.to_openai_tool() for tool_def in self._tools.values()]
        if normalized == "public":
            return [tool_def.to_public_descriptor() for tool_def in self._tools.values()]
        if normalized == "mcp_descriptor":
            return [tool_def.to_mcp_descriptor() for tool_def in self._tools.values()]
        raise ValueError(f"Unsupported tool surface format: {format}")

    def prepare_call(
        self,
        tool_name: str,
        arguments: Any,
        *,
        stock_scope: Any = None,
    ) -> dict[str, Any]:
        """Validate a tool call without executing it.

        Returns ``{"ok": True, "args": ...}`` or ``{"ok": False, "code", "message", ...}``.
        """

        name = tool_name if isinstance(tool_name, str) else str(tool_name)
        tool_def = self.resolve(name)
        if tool_def is None:
            return {
                "ok": False,
                "code": "tool_not_found",
                "message": f"Tool '{name}' not found.",
                "arguments": arguments,
            }
        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "code": "invalid_arguments",
                "message": "Tool arguments must be an object.",
                "arguments": arguments,
            }
        coerced = _coerce_arguments(tool_def, arguments)
        validation_error = _validate_arguments(tool_def, coerced)
        if validation_error is not None:
            return {
                "ok": False,
                "code": "invalid_arguments",
                "message": validation_error,
                "arguments": coerced,
            }
        guard_result = _guard_tool_stock_scope(tool_def, coerced, stock_scope)
        if guard_result is not None:
            return {
                "ok": False,
                "code": "stock_scope_violation",
                "message": "Tool call is outside the allowed stock scope.",
                "details": guard_result,
                "arguments": coerced,
            }
        return {"ok": True, "args": coerced, "tool": name}

    def _validate_tool_call(
        self,
        tool_name: str,
        arguments: Any,
        ctx: ToolAccessContext,
        started_at: float,
    ) -> dict[str, Any] | ToolDefinition:
        tool_def = self.resolve(tool_name)
        if tool_def is None:
            return self._error_result(
                tool_name=tool_name,
                code="tool_not_found",
                message=f"Tool '{tool_name}' not found.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        coerced = _coerce_arguments(tool_def, arguments if isinstance(arguments, dict) else {})
        if isinstance(arguments, dict):
            arguments = coerced
        validation_error = _validate_arguments(tool_def, arguments)
        if validation_error is not None:
            return self._error_result(
                tool_name=tool_name,
                code="invalid_arguments",
                message=validation_error,
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        guard_result = _guard_tool_stock_scope(tool_def, arguments, ctx.stock_scope)
        if guard_result is not None:
            result_text = serialize_tool_result(guard_result)
            return self._error_result(
                tool_name=tool_name,
                code="stock_scope_violation",
                message="Tool call is outside the allowed stock scope.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                details=guard_result,
                result_text=result_text,
                arguments=arguments,
            )
        return tool_def

    def execute_tool(
        self,
        name: str,
        arguments: Any,
        context: ToolAccessContext | None = None,
    ) -> dict[str, Any]:
        ctx = context or ToolAccessContext()
        started_at = time.time()
        tool_name = name if isinstance(name, str) else str(name)

        val_res = self._validate_tool_call(tool_name, arguments, ctx, started_at)
        if isinstance(val_res, dict):
            return val_res
        tool_def = val_res
        if isinstance(arguments, dict):
            arguments = _coerce_arguments(tool_def, arguments)

        timeout = None if tool_name in _TIMEOUT_EXEMPT_TOOLS else ctx.timeout_seconds
        try:
            if timeout is not None and timeout > 0:
                result = _execute_with_timeout(tool_def, arguments, float(timeout))
            else:
                result = tool_def.handler(**arguments)
        except FuturesTimeoutError:
            return self._error_result(
                tool_name=tool_name,
                code="timeout",
                message=f"Tool execution timed out after {timeout:.2f}s.",
                started_at=started_at,
                context=ctx,
                retriable=True,
                details={"timeout_seconds": timeout},
                arguments=arguments,
            )
        except Exception as e:
            return self._error_result(
                tool_name=tool_name,
                code="handler_error",
                message=f"Tool handler failed: {e}",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        return self._build_success_result(tool_name, result, arguments, ctx, started_at)

    def _build_success_result(
        self,
        tool_name: str,
        result: Any,
        arguments: Any,
        ctx: ToolAccessContext,
        started_at: float,
    ) -> dict[str, Any]:
        try:
            result_text = serialize_tool_result(result)
        except Exception:
            return self._error_result(
                tool_name=tool_name,
                code="serialization_error",
                message="Tool result could not be serialized.",
                started_at=started_at,
                context=ctx,
                retriable=False,
                arguments=arguments,
            )

        public_result = _public_payload_from_result_text(result_text)
        result_truncated = False
        if ctx.max_result_bytes is not None and ctx.max_result_bytes >= 0:
            result_text, result_truncated = _truncate_text_bytes(result_text, int(ctx.max_result_bytes))
            public_result = None if result_truncated else _public_payload_from_result_text(result_text)

        duration = time.time() - started_at
        return {
            "ok": True,
            "tool_name": tool_name,
            "result": public_result,
            "result_text": result_text,
            "error": None,
            "audit": build_tool_audit(
                tool_name=tool_name,
                arguments=arguments,
                result=result_text,
                duration=duration,
                context=ctx,
            ),
            "diagnostics": {
                "redacted": True,
                "result_length": len(result_text.encode("utf-8")),
                "result_truncated": result_truncated,
                "preview": redact_diagnostic_value(result_text),
            },
        }

    def _error_result(
        self,
        *,
        tool_name: str,
        code: str,
        message: str,
        started_at: float,
        context: ToolAccessContext,
        retriable: bool,
        details: dict[str, Any] | None = None,
        result_text: str | None = None,
        arguments: Any = None,
    ) -> dict[str, Any]:
        duration = time.time() - started_at
        safe_text = result_text or json.dumps(
            {"error": message, "code": code, "retriable": retriable},
            ensure_ascii=False,
        )
        result_truncated = False
        if context.max_result_bytes is not None and context.max_result_bytes >= 0:
            safe_text, result_truncated = _truncate_text_bytes(safe_text, int(context.max_result_bytes))
        return {
            "ok": False,
            "tool_name": tool_name,
            "result": None,
            "result_text": safe_text,
            "error": {
                "code": code,
                "message": message,
                "retriable": retriable,
                "details": details or {},
            },
            "audit": build_tool_audit(
                tool_name=tool_name,
                arguments=arguments if arguments is not None else {},
                result=safe_text,
                error_code=code,
                duration=duration,
                context=context,
            ),
            "diagnostics": {
                "redacted": True,
                "result_length": len(safe_text.encode("utf-8")),
                "result_truncated": result_truncated,
                "preview": redact_diagnostic_value(safe_text),
            },
        }


def _execute_with_timeout(tool_def: ToolDefinition, arguments: dict[str, Any], timeout: float) -> Any:
    pool = ThreadPoolExecutor(max_workers=1)
    ctx = contextvars.copy_context()
    future = pool.submit(ctx.run, tool_def.handler, **arguments)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _coerce_arguments(tool_def: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    """Mild TypeBox-style Convert: fix common model type slips before strict Check."""

    params = {param.name: param for param in tool_def.parameters}
    coerced = dict(arguments)
    for key, value in list(coerced.items()):
        if key == "tool_context":
            continue
        param = params.get(key)
        if param is None:
            continue
        coerced[key] = _coerce_parameter_value(param, value)
    return coerced


def _coerce_parameter_value(param: ToolParameter, value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    text = value.strip()
    target = param.type
    if param.accepted_types:
        if bool in param.accepted_types and int not in param.accepted_types:
            target = "boolean"
        elif int in param.accepted_types and float not in param.accepted_types:
            target = "integer"
        elif float in param.accepted_types or int in param.accepted_types:
            target = "number"
    if target == "integer":
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        return value
    if target == "number":
        try:
            number = float(text)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if target == "boolean":
        lowered = text.lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return value


def _validate_arguments(tool_def: ToolDefinition, arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "arguments must be an object"

    params = {param.name: param for param in tool_def.parameters}
    for param in tool_def.parameters:
        if param.required and param.name not in arguments:
            return f"missing required argument: {param.name}"

    accepts_extra = tool_def.accepts_extra_arguments()
    for key in arguments:
        if key == "tool_context":
            continue
        if key not in params and not accepts_extra:
            return f"unexpected argument: {key}"

    for key, value in arguments.items():
        if key == "tool_context":
            continue
        param = params.get(key)
        if param is None:
            continue
        error = _validate_parameter_value(param, value)
        if error:
            return error
    return None


def _validate_parameter_value(param: ToolParameter, value: Any) -> str | None:
    if value is None:
        if not param.required:
            return None
        return f"argument {param.name} must not be null"
    if param.enum and value not in param.enum:
        return f"argument {param.name} must be one of: {', '.join(map(str, param.enum))}"
    if param.accepted_types:
        if _matches_accepted_types(value, param.accepted_types):
            return None
        return f"argument {param.name} must be {param.type}"
    expected = _JSON_TYPE_TO_PYTHON.get(param.type)
    if not expected:
        return None
    if param.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"argument {param.name} must be integer"
        return None
    if param.type == "number":
        if isinstance(value, bool) or not isinstance(value, expected):
            return f"argument {param.name} must be number"
        return None
    if not isinstance(value, expected):
        return f"argument {param.name} must be {param.type}"
    return None


def _matches_accepted_types(value: Any, accepted_types: tuple[type, ...]) -> bool:
    for expected in accepted_types:
        if expected is bool:
            if isinstance(value, bool):
                return True
            continue
        if expected is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return True
            continue
        if expected is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
            continue
        if isinstance(value, expected):
            return True
    return False


def _guard_tool_stock_scope(
    tool_def: ToolDefinition,
    arguments: dict[str, Any],
    stock_scope: Any,
) -> dict[str, Any] | None:
    if stock_scope is None or not isinstance(arguments, dict):
        return None
    param_name = next((p.name for p in tool_def.parameters if p.name in ("code", "stock_code")), None)
    if param_name is None or param_name not in arguments:
        return None

    requested = _normalize_guard_stock_code(arguments.get(param_name))
    expected = _normalize_guard_stock_code(getattr(stock_scope, "expected_stock_code", ""))
    allowed = {
        _normalize_guard_stock_code(code) for code in getattr(stock_scope, "allowed_stock_codes", set()) or set()
    }
    allowed.discard("")
    if requested and (requested == expected or requested in allowed):
        return None

    return {
        "error": "stock_scope_violation",
        "expected_stock_code": expected,
        "requested_stock_code": requested,
        "allowed_stock_codes": sorted(allowed),
        "retriable": False,
    }


def from_json_schema(schema: dict, handler: Callable) -> ToolDefinition:
    name = schema["name"]
    desc = schema.get("description", "")
    params_schema = schema.get("parameters", {})
    props = params_schema.get("properties", {})
    required_fields = params_schema.get("required", [])

    parameters = []
    for k, p in props.items():
        parameters.append(
            ToolParameter(
                name=k,
                type=p.get("type", "string"),
                description=p.get("description", ""),
                required=k in required_fields,
                enum=p.get("enum"),
                default=p.get("default"),
            )
        )

    # Infer policy: if has "code" or "stock_code" property, declare stock scope
    scope_dims = []
    if "code" in props or "stock_code" in props:
        scope_dims.append("stock")

    policy = ToolPolicy.declared(read_only=True, scope_dimensions=scope_dims)

    return ToolDefinition(
        name=name,
        description=desc,
        parameters=parameters,
        handler=handler,
        policy=policy,
    )


_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}
_NONE_TYPE = type(None)
_UNION_TYPE = getattr(types, "UnionType", None)


def _accepted_types_from_hint(hint: Any) -> tuple[type, ...]:
    if hint in (None, inspect.Parameter.empty, Any):
        return ()
    origin = get_origin(hint)
    if origin in (Union, _UNION_TYPE):
        out: list[type] = []
        for arg in get_args(hint):
            if arg is _NONE_TYPE:
                continue
            out.extend(_accepted_types_from_hint(arg))
        return tuple(dict.fromkeys(out))
    if origin is Literal:
        return tuple(dict.fromkeys(type(arg) for arg in get_args(hint) if arg is not None))
    if origin in (list, tuple, set):
        return (list,)
    if origin is dict:
        return (dict,)
    mapped = next((typ for typ in _TYPE_MAP if hint is typ), None)
    return (mapped,) if mapped is not None else ()


def _resolve_param_type(hint: Any) -> str:
    accepted = _accepted_types_from_hint(hint)
    if bool in accepted:
        return "boolean"
    if int in accepted:
        return "integer"
    if float in accepted:
        return "number"
    if list in accepted:
        return "array"
    if dict in accepted:
        return "object"
    return "string"


def _introspect_parameters(func: Callable) -> list[ToolParameter]:
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {})
    parameters = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls", "tool_context"):
            continue
        if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        hint = hints.get(param_name, str)
        param_type = _resolve_param_type(hint)
        accepted_types = _accepted_types_from_hint(hint)
        has_default = param.default is not inspect.Parameter.empty
        parameters.append(
            ToolParameter(
                name=param_name,
                type=param_type,
                description=f"Parameter: {param_name}",
                required=not has_default,
                default=param.default if has_default else None,
                accepted_types=accepted_types,
            )
        )
    return parameters


def from_handler(
    func: Callable,
    name: str | None = None,
    description: str | None = None,
    category: str = "data",
) -> ToolDefinition:
    parameters = _introspect_parameters(func)

    scope_dims = []
    if any(p.name in ("code", "stock_code") for p in parameters):
        scope_dims.append("stock")

    policy = ToolPolicy.declared(read_only=True, scope_dimensions=scope_dims)
    tool_name = name or func.__name__
    tool_desc = description or func.__doc__ or f"Tool: {tool_name}"

    return ToolDefinition(
        name=tool_name,
        description=tool_desc,
        parameters=parameters,
        handler=func,
        category=category,
        policy=policy,
    )
