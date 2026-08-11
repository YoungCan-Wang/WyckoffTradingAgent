"""Gemini Provider — google-genai SDK 实现。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

from google import genai
from google.genai import types

from cli.providers.base import LLMProvider


def _part_thought_signature(part: Any) -> bytes | None:
    sig = getattr(part, "thought_signature", None)
    if sig is None:
        return None
    if isinstance(sig, bytes):
        return sig
    if isinstance(sig, str):
        return sig.encode("utf-8")
    return None


def tool_call_dict_from_part(part: Any, *, call_id: str | None = None) -> dict[str, Any]:
    """从 Gemini Part 提取统一 tool_call 结构，保留 thought_signature。"""
    fc = part.function_call
    call: dict[str, Any] = {
        "id": call_id or uuid.uuid4().hex[:12],
        "name": fc.name,
        "args": dict(fc.args) if fc.args else {},
    }
    sig = _part_thought_signature(part)
    if sig is not None:
        call["thought_signature"] = sig
    return call


def _gemini_usage_field(usage_meta: Any, name: str) -> int | None:
    """Return an int only when the SDK actually set the field (not a default null/0 placeholder)."""
    if usage_meta is None:
        return None
    fields_set = getattr(usage_meta, "model_fields_set", None)
    if isinstance(fields_set, (set, frozenset)) and name not in fields_set:
        return None
    value = getattr(usage_meta, name, None)
    if value is None:
        return None
    return int(value or 0)


def _gemini_usage_event(usage_meta: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "usage",
        "input_tokens": _gemini_usage_field(usage_meta, "prompt_token_count") or 0,
        "output_tokens": _gemini_usage_field(usage_meta, "candidates_token_count") or 0,
    }
    cached = _gemini_usage_field(usage_meta, "cached_content_token_count")
    if cached is not None:
        event["cache_read_tokens"] = cached
    written = _gemini_usage_field(usage_meta, "cache_tokens_input")
    if written is not None:
        event["cache_write_tokens"] = written
    return event


def function_call_part_from_tool_call(tc: dict[str, Any]) -> types.Part:
    """将统一 tool_call 转回 Gemini Part，回传 thought_signature。"""
    kwargs: dict[str, Any] = {
        "function_call": types.FunctionCall(
            name=tc["name"],
            args=tc.get("args") or {},
        ),
    }
    sig = tc.get("thought_signature")
    if sig is not None:
        kwargs["thought_signature"] = sig
    return types.Part(**kwargs)


class GeminiProvider(LLMProvider):
    """通过 google-genai SDK 调用 Gemini 模型。"""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    @property
    def name(self) -> str:
        return f"Gemini ({self._model})"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]:
        # 构建 Gemini contents
        contents = self._build_contents(messages)

        # 构建工具声明
        gemini_tools = self._build_tools(tools) if tools else None

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            tools=gemini_tools,
        )

        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )

        return self._parse_response(response)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        contents = self._build_contents(messages)
        gemini_tools = self._build_tools(tools) if tools else None

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            tools=gemini_tools,
        )

        text_buf = ""
        tool_calls = []
        usage_meta = None
        finish_reason = ""

        for chunk in self._client.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        ):
            # usage_metadata 可能出现在任意 chunk（尤其是最后一个无 candidates 的 chunk）
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:
                usage_meta = um
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            raw_finish = getattr(candidate, "finish_reason", None)
            if raw_finish is not None:
                finish_reason = str(getattr(raw_finish, "name", raw_finish))
            parts = getattr(getattr(candidate, "content", None), "parts", None) or []
            for part in parts:
                if part.function_call:
                    tool_calls.append(tool_call_dict_from_part(part))
                elif getattr(part, "thought", False) and part.text:
                    # thought part 的 text 是思维链，不能混进正文
                    yield {"type": "thinking_delta", "text": part.text}
                elif part.text:
                    text_buf += part.text
                    yield {"type": "text_delta", "text": part.text}

        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls, "text": text_buf}

        yield _gemini_usage_event(usage_meta)
        if finish_reason:
            reason = "length" if "MAX_TOKEN" in finish_reason.upper() else finish_reason
            yield {"type": "finish", "reason": reason}

    def _build_contents(self, messages: list[dict]) -> list[types.Content]:
        """将统一消息格式转为 Gemini Content 列表。"""
        contents = []
        for msg in messages:
            role = msg["role"]

            if role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=msg["content"])],
                    )
                )

            elif role == "assistant":
                parts = []
                # 文本部分
                if msg.get("content"):
                    parts.append(types.Part.from_text(text=msg["content"]))
                # 工具调用部分
                for tc in msg.get("tool_calls", []):
                    parts.append(function_call_part_from_tool_call(tc))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))

            elif role == "tool":
                # Gemini 要求 function_response 在一个 Content 里
                result = msg["content"]
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result = {"result": result}
                # Gemini FunctionResponse.response 必须是 dict，不能是 list
                if not isinstance(result, dict):
                    result = {"result": result}
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=msg.get("name", "unknown"),
                                    response=result,
                                )
                            )
                        ],
                    )
                )

        return contents

    def _build_tools(self, tools: list[dict]) -> list[types.Tool]:
        """将标准 function schema 转为 Gemini Tool 格式。"""
        declarations = []
        for t in tools:
            params = t.get("parameters", {})
            declarations.append(
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=params if params.get("properties") else None,
                )
            )
        return [types.Tool(function_declarations=declarations)]

    def _parse_response(self, response) -> dict[str, Any]:
        """解析 Gemini 响应为统一格式。"""
        if not response.candidates:
            return {"type": "text", "text": "(模型未返回内容)"}

        parts = response.candidates[0].content.parts
        tool_calls = []
        text_parts = []

        for part in parts:
            if part.function_call:
                tool_calls.append(tool_call_dict_from_part(part))
            elif part.text:
                text_parts.append(part.text)

        if tool_calls:
            return {"type": "tool_calls", "tool_calls": tool_calls, "text": "".join(text_parts)}

        return {"type": "text", "text": "".join(text_parts)}
