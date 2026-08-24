"""Append-only agent run scratchpad.

Each CLI/TUI turn can write a JSONL trace under ``~/.wyckoff/scratchpad``.
The file is deliberately independent from SQLite chat logs so partial runs,
crashes, and long tool calls still leave an inspectable trail.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|password|secret|authorization|cookie)", re.IGNORECASE)
_MAX_INLINE_STRING = 200_000


def wyckoff_home() -> Path:
    """Return the local Wyckoff state directory."""

    return Path(os.getenv("WYCKOFF_HOME", Path.home() / ".wyckoff")).expanduser()


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def scrub_sensitive_value(value: Any) -> Any:
    """Make values JSON-safe and redact obvious secrets."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            cleaned[key_text] = "***REDACTED***" if _SENSITIVE_KEY_RE.search(key_text) else scrub_sensitive_value(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [scrub_sensitive_value(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_INLINE_STRING:
            return value[:_MAX_INLINE_STRING] + f"\n...[truncated in scratchpad, original chars={len(value)}]"
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class AgentScratchpad:
    """JSONL trace for a single agent turn."""

    def __init__(
        self,
        query: str,
        *,
        session_id: str = "",
        scratchpad_dir: Path | None = None,
    ) -> None:
        self.query = query
        self.session_id = session_id
        self.dir = scratchpad_dir or wyckoff_home() / "scratchpad"
        self.dir.mkdir(parents=True, exist_ok=True)

        query_hash = hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()[:12]
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.path = self.dir / f"{stamp}_{query_hash}.jsonl"
        self._context_sources: list[dict[str, Any]] = []

        self.append(
            {
                "type": "init",
                "timestamp": _timestamp(),
                "session_id": session_id,
                "content": query,
            }
        )

    def append(self, entry: dict[str, Any]) -> None:
        safe_entry = scrub_sensitive_value(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe_entry, ensure_ascii=False, default=str))
            fh.write("\n")

    def record_thinking(self, content: str) -> None:
        if not content:
            return
        self.append(
            {
                "type": "thinking",
                "timestamp": _timestamp(),
                "content": content,
            }
        )

    def record_tool_start(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str = "",
    ) -> None:
        """在工具**执行之前**落一条意图记录。

        为什么要单独记一次:原来只有 `record_tool_result`,也就是副作用发生完
        才写盘。进程在工具跑到一半被 kill(SIGKILL、断电)时,那次调用在日志里
        完全无痕 —— 无法区分「没跑」和「跑了但结果丢了」。

        这个项目有真实副作用:调仓、设止损、写报告。所以「有没有可能已经改了
        但我不知道」是个必须能回答的问题。配对方式很简单:有 tool_started
        而没有对应 tool_call_id 的 tool_result,就是一次结果未知的调用。

        这和审批队列(approval_queue)已经在做的事是同一个形状 ——
        先落 pending 意图、执行、再落结果。这里只是把它推广到所有工具。
        """
        self.append(
            {
                "type": "tool_started",
                "timestamp": _timestamp(),
                "toolName": tool_name,
                "toolCallId": tool_call_id,
                "args": args,
            }
        )

    def record_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        *,
        duration_ms: int | None = None,
        status: str = "ok",
        tool_call_id: str = "",
    ) -> None:
        self._record_context_source(tool_name, result)
        entry: dict[str, Any] = {
            "type": "tool_result",
            "timestamp": _timestamp(),
            "toolName": tool_name,
            # 与 tool_started 配对用。少了这个 id，「有开始没结果」就无从判断，
            # 光按工具名匹配会在同一轮调用同名工具两次时错配。
            "toolCallId": tool_call_id,
            "args": args,
            "result": result,
            "status": status,
        }
        if duration_ms is not None:
            entry["durationMs"] = duration_ms
        self.append(entry)

    def _record_context_source(self, tool_name: str, result: Any) -> None:
        if not isinstance(result, dict):
            return
        quality = result.get("data_quality")
        source = result.get("data_source") or result.get("source")
        as_of = result.get("data_asof") or result.get("latest_date") or result.get("trade_date")
        if not any((quality, source, as_of)):
            return
        item = {
            "tool": str(tool_name or ""),
            "source": str(source or ""),
            "as_of": str(as_of or ""),
            "quality": scrub_sensitive_value(quality) if isinstance(quality, dict) else str(quality or ""),
        }
        if item not in self._context_sources:
            self._context_sources.append(item)

    def record_context_snapshot(self, *, provider: str = "", model: str = "") -> None:
        """Persist the low-sensitivity data context used by this analysis turn."""
        self.append(
            {
                "type": "context_snapshot",
                "timestamp": _timestamp(),
                "provider": provider,
                "model": model,
                "sources": self._context_sources,
            }
        )

    def record_compaction(
        self,
        *,
        before_messages: int,
        after_messages: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "type": "compaction",
            "timestamp": _timestamp(),
            "beforeMessages": before_messages,
            "afterMessages": after_messages,
        }
        if metadata:
            entry["contextArchive"] = metadata
        self.append(entry)

    def record_final(
        self,
        content: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_s: float = 0.0,
        provider: str = "",
        model: str = "",
    ) -> None:
        self.record_context_snapshot(provider=provider, model=model)
        self.append(
            {
                "type": "final",
                "timestamp": _timestamp(),
                "content": content,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "elapsed_s": round(elapsed_s, 3),
                },
            }
        )

    def record_error(self, error: str, *, elapsed_s: float = 0.0) -> None:
        self.append(
            {
                "type": "error",
                "timestamp": _timestamp(),
                "error": error,
                "elapsed_s": round(elapsed_s, 3),
            }
        )


def dangling_tool_calls(path: Path) -> list[dict[str, Any]]:
    """从一份 scratchpad 里找出「开始了但没有结果」的工具调用。

    这是把执行意图单独落盘换来的能力。用途:进程被 SIGKILL 或断电之后,
    回答「有没有可能已经改了持仓/止损,但我不知道结果」。

    只按 toolCallId 配对。按工具名配会在同一轮里两次调用同名工具时错配 ——
    比如连着查两只票的持仓,第一次的结果会把第二次的开始记录也「认领」掉。

    没有 id 的条目(历史文件、并发路径早期版本)直接跳过而不是猜:
    宁可漏报也不要给出一个错的悬空清单。
    """
    started: dict[str, dict[str, Any]] = {}
    done: set[str] = set()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # 被 kill 时最后一行可能只写了一半 —— 半条 JSON 不该让整份日志不可读。
            continue
        if not isinstance(entry, dict):
            continue
        call_id = str(entry.get("toolCallId") or "")
        if not call_id:
            continue
        kind = entry.get("type")
        if kind == "tool_started":
            started[call_id] = entry
        elif kind == "tool_result":
            done.add(call_id)
    return [entry for call_id, entry in started.items() if call_id not in done]
