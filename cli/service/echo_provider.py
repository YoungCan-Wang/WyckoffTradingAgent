"""无密钥冒烟用的回显 provider：镜像、SSE 通道和网关链路不依赖任何模型账号就能跑通。"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

from cli.providers.base import LLMProvider

_PIECE_CHARS = 8


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


class EchoProvider(LLMProvider):
    def __init__(self, delay_s: float = 0.0) -> None:
        # 控制出字节奏，用来验证长连接链路不缓冲、不被中途掐断。
        self.delay_s = delay_s

    @property
    def name(self) -> str:
        return "echo"

    def chat(self, messages, tools, system_prompt="") -> dict[str, Any]:
        return {"type": "text", "text": _last_user_text(messages)}

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        text = _last_user_text(messages)
        for start in range(0, len(text), _PIECE_CHARS):
            yield {"type": "text_delta", "text": text[start : start + _PIECE_CHARS]}
            if self.delay_s:
                time.sleep(self.delay_s)
        yield {"type": "usage", "input_tokens": len(text), "output_tokens": len(text)}
        yield {"type": "finish", "reason": "stop"}
