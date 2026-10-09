"""会员车道对浏览器说 ai-sdk 的 UI message stream（v1）。

Web 读盘室前端用 @ai-sdk/react 的 useChat 消费这个协议。服务直接说它，网关
（免费档 Worker 每次调用只有 10ms CPU）只需原样转发字节，不做逐块翻译。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import uuid
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)

UI_STREAM_HEADERS = {"x-vercel-ai-ui-message-stream": "v1"}
DONE_FRAME = b"data: [DONE]\n\n"
MAX_TOOL_OUTPUT_CHARS = 200_000
MAX_ERROR_CHARS = 500
MAX_HISTORY_MESSAGES = 60
MAX_TEXT_CHARS = 20_000

Chunk = dict[str, Any]


def _json_safe(value: Any) -> Any:
    """JS 的 JSON.parse 不认 NaN / Infinity，而 pandas 算出来的结果里很常见；统一换成 null。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _tool_output(result: Any) -> Any:
    safe = _json_safe(result)
    rendered = json.dumps(safe, ensure_ascii=False, default=str)
    if len(rendered) <= MAX_TOOL_OUTPUT_CHARS:
        return safe
    return {"truncated": True, "preview": rendered[:2000]}


def sse(chunk: Chunk) -> bytes:
    payload = json.dumps(chunk, ensure_ascii=False, allow_nan=False, default=str)
    return b"data: " + payload.encode() + b"\n\n"


class UiStreamTranslator:
    """RuntimeEvent → UIMessageChunk。一个实例对应一条 assistant 消息。"""

    def __init__(self, message_id: str) -> None:
        self._message_id = message_id
        self._open_text: str | None = None
        self._text_parts = 0
        self.finished = False
        self._handlers = {
            "text_delta": self._on_text,
            "stage_start": self._on_stage_start,
            "tool_start": self._on_tool_start,
            "tool_result": self._on_tool_result,
            "tool_error": self._on_tool_error,
            "done": self._on_done,
            "turn_failed": self._on_failed,
            "turn_cancelled": self._on_cancelled,
        }

    def start(self) -> list[Chunk]:
        return [{"type": "start", "messageId": self._message_id}, {"type": "start-step"}]

    def feed(self, event: dict[str, Any]) -> list[Chunk]:
        handler = self._handlers.get(str(event.get("type")))
        if self.finished or handler is None:
            return []
        return handler(event)

    def fail(self, message: str) -> list[Chunk]:
        if self.finished:
            return []
        return [*self._close_text(), {"type": "error", "errorText": message[:MAX_ERROR_CHARS]}, *self._finish("error")]

    def close(self) -> list[Chunk]:
        """事件流没有收到终止事件就结束了：补一个错误收尾，别让前端的消息一直停在流式状态。"""
        return self.fail("stream ended unexpectedly")

    def _close_text(self) -> list[Chunk]:
        if self._open_text is None:
            return []
        chunk = {"type": "text-end", "id": self._open_text}
        self._open_text = None
        return [chunk]

    def _text(self, text: str) -> list[Chunk]:
        chunks: list[Chunk] = []
        if self._open_text is None:
            self._text_parts += 1
            self._open_text = f"text-{self._text_parts}"
            chunks.append({"type": "text-start", "id": self._open_text})
        chunks.append({"type": "text-delta", "id": self._open_text, "delta": text})
        return chunks

    def _finish(self, reason: str) -> list[Chunk]:
        self.finished = True
        return [{"type": "finish-step"}, {"type": "finish", "finishReason": reason}]

    def _on_text(self, event: dict[str, Any]) -> list[Chunk]:
        text = event.get("text")
        return self._text(text) if isinstance(text, str) and text else []

    def _on_stage_start(self, event: dict[str, Any]) -> list[Chunk]:
        if event.get("stage") != "model" or int(event.get("round") or 1) <= 1:
            return []
        return [*self._close_text(), {"type": "finish-step"}, {"type": "start-step"}]

    def _on_tool_start(self, event: dict[str, Any]) -> list[Chunk]:
        # dynamic：Python 工具的输出形状与前端为 TS 工具写的专用渲染器不一致，走通用渲染。
        chunk: Chunk = {
            "type": "tool-input-available",
            "toolCallId": str(event.get("tool_call_id")),
            "toolName": str(event.get("name")),
            "input": _json_safe(event.get("args") or {}),
            "dynamic": True,
        }
        if isinstance(event.get("display_name"), str):
            chunk["title"] = event["display_name"]
        return [*self._close_text(), chunk]

    def _on_tool_result(self, event: dict[str, Any]) -> list[Chunk]:
        return [
            {
                "type": "tool-output-available",
                "toolCallId": str(event.get("tool_call_id")),
                "output": _tool_output(event.get("result")),
                "dynamic": True,
            }
        ]

    def _on_tool_error(self, event: dict[str, Any]) -> list[Chunk]:
        return [
            {
                "type": "tool-output-error",
                "toolCallId": str(event.get("tool_call_id")),
                "errorText": str(event.get("error") or "tool failed")[:MAX_ERROR_CHARS],
                "dynamic": True,
            }
        ]

    def _on_done(self, event: dict[str, Any]) -> list[Chunk]:
        chunks = self._close_text()
        text = event.get("text")
        if self._text_parts == 0 and isinstance(text, str) and text:
            chunks += [*self._text(text), *self._close_text()]
        return [*chunks, *self._finish("stop")]

    def _on_failed(self, event: dict[str, Any]) -> list[Chunk]:
        # runtime 的 turn_failed 把原因放在 message（以及 failure.message），不是 error。
        failure = event.get("failure")
        detail = event.get("message") or (failure.get("message") if isinstance(failure, dict) else None)
        return self.fail(str(detail or "agent error"))

    def _on_cancelled(self, event: dict[str, Any]) -> list[Chunk]:
        self.finished = True
        return [*self._close_text(), {"type": "finish-step"}, {"type": "abort", "reason": "cancelled"}]


def ui_frames(events: Generator[dict[str, Any], None, None]) -> Generator[bytes, None, None]:
    translator = UiStreamTranslator(uuid.uuid4().hex)
    with contextlib.closing(events):
        yield from map(sse, translator.start())
        try:
            for event in events:
                yield from map(sse, translator.feed(event))
        except Exception:
            logger.exception("turn failed")
            yield from map(sse, translator.fail("agent error"))
        yield from map(sse, translator.close())
    yield DONE_FRAME


def _message_text(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict) and part.get("type") == "text"
    )


def history_from_ui_messages(messages: Any) -> tuple[str, list[dict[str, Any]]] | None:
    """最后一条必须是用户文本；之前的 user / assistant 文本作为历史。

    前端每次都带完整 messages，容器休眠丢了内存也不影响，所以这条路径不用服务端会话。
    工具片段不带入：把 Python 工具的结果还原成 provider 的 tool 消息需要逐工具对齐，留给后续。
    """
    if not isinstance(messages, list):
        return None
    turns = [(m.get("role"), _message_text(m)) for m in messages if isinstance(m, dict)]
    turns = [(role, text) for role, text in turns if role in ("user", "assistant") and text]
    if not turns or turns[-1][0] != "user" or len(turns[-1][1]) > MAX_TEXT_CHARS:
        return None
    history = [{"role": role, "content": text} for role, text in turns[:-1]]
    return turns[-1][1], history[-MAX_HISTORY_MESSAGES:]
