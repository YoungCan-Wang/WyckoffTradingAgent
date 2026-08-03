"""Classify provider/runtime exceptions into FailureKind."""

from __future__ import annotations

from cli.conversation.state import FailureInfo, FailureKind


def classify_failure(exc: BaseException) -> FailureInfo:
    """Map an exception to a stable FailureInfo for resume UX."""

    name = type(exc).__name__
    message = str(exc) or name
    lowered = f"{name} {message}".lower()
    kind = _kind_from_text(name, lowered)
    return FailureInfo(kind=kind, message=message[:500], exception_type=name)


def _kind_from_text(name: str, lowered: str) -> FailureKind:
    if name in {"ResourceExhausted", "RateLimitError"} or "429" in lowered or "rate limit" in lowered:
        return FailureKind.RATE_LIMIT
    if name in {"AuthenticationError", "PermissionDenied"} or "401" in lowered or "403" in lowered:
        return FailureKind.AUTH
    if name in {"TimeoutError", "ReadTimeout", "ConnectTimeout"} or "timeout" in lowered:
        return FailureKind.TIMEOUT
    if name in {"ConnectionError", "ConnectError", "APIConnectionError"} or "connection" in lowered:
        return FailureKind.NETWORK
    if "resourceexhausted" in lowered or "quota" in lowered:
        return FailureKind.RATE_LIMIT
    if any(token in lowered for token in ("provider", "openai", "anthropic", "api error")):
        return FailureKind.PROVIDER
    return FailureKind.UNKNOWN
