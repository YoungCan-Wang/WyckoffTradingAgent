"""OpenAI Provider — openai SDK 实现。"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai

from cli.providers.base import LLMProvider

_TIMEOUT = httpx.Timeout(300.0, connect=60.0)


@dataclass
class OpenAIStreamState:
    tool_map: dict[int, dict[str, Any]] = field(default_factory=dict)
    text_buf: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


def _openai_extra_content(obj: Any) -> dict[str, Any] | None:
    """读取 Gemini OpenAI 兼容层附带的 extra_content（含 thought_signature）。"""
    extra = getattr(obj, "model_extra", None) or {}
    if isinstance(extra, dict):
        content = extra.get("extra_content")
        if content:
            return content
    dump_fn = getattr(obj, "model_dump", None)
    if callable(dump_fn):
        try:
            dump = dump_fn()
            if isinstance(dump, dict):
                content = dump.get("extra_content")
                if content:
                    return content
        except TypeError:
            pass
    return None


def _openai_tool_call_payload(tc: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": tc["id"],
        "type": "function",
        "function": {
            "name": tc["name"],
            "arguments": json.dumps(tc["args"], ensure_ascii=False),
        },
    }
    extra_content = tc.get("extra_content")
    if extra_content:
        payload["extra_content"] = extra_content
    return payload


def _create_openai_stream(client: openai.OpenAI, kwargs: dict[str, Any]):
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        kwargs.pop("stream_options", None)
        try:
            return client.chat.completions.create(**kwargs)
        except Exception:
            kwargs.pop("tool_choice", None)
            kwargs.pop("frequency_penalty", None)
            return client.chat.completions.create(**kwargs)


def _consume_usage_chunk(state: OpenAIStreamState, chunk: Any) -> bool:
    if chunk.choices or not chunk.usage:
        return False
    state.input_tokens = chunk.usage.prompt_tokens or 0
    state.output_tokens = chunk.usage.completion_tokens or 0
    details = getattr(chunk.usage, "prompt_tokens_details", None)
    if details:
        state.cache_read = getattr(details, "cached_tokens", 0) or 0
    comp_details = getattr(chunk.usage, "completion_tokens_details", None)
    if comp_details:
        state.cache_write = getattr(comp_details, "cached_tokens", 0) or 0
    return True


def _accumulate_tool_delta(tool_map: dict[int, dict[str, Any]], tc_delta: Any) -> None:
    idx = tc_delta.index
    if idx not in tool_map:
        tool_map[idx] = {
            "id": tc_delta.id or "",
            "name": tc_delta.function.name or "" if tc_delta.function else "",
            "args_json": "",
        }
    if tc_delta.id:
        tool_map[idx]["id"] = tc_delta.id
    extra_content = _openai_extra_content(tc_delta)
    if extra_content:
        tool_map[idx]["extra_content"] = extra_content
    if tc_delta.function:
        if tc_delta.function.name:
            tool_map[idx]["name"] = tc_delta.function.name
        if tc_delta.function.arguments:
            tool_map[idx]["args_json"] += tc_delta.function.arguments


# 各家 OpenAI 兼容网关放思维链的字段名不统一，逐个试。
_REASONING_DELTA_FIELDS = ("reasoning_content", "reasoning", "thinking", "thought")


def _reasoning_delta_text(delta: Any) -> str:
    for field_name in _REASONING_DELTA_FIELDS:
        value = getattr(delta, field_name, None)
        if isinstance(value, str) and value:
            return value
        # 有的网关给 {"reasoning": {"content": "..."}} 这种嵌套结构
        if isinstance(value, dict):
            nested = value.get("content") or value.get("text")
            if isinstance(nested, str) and nested:
                return nested
    return ""


def _consume_delta_events(state: OpenAIStreamState, delta: Any) -> Generator[dict[str, Any], None, None]:
    if reasoning := _reasoning_delta_text(delta):
        yield {"type": "thinking_delta", "text": reasoning}

    if delta.content:
        state.text_buf += delta.content
        yield {"type": "text_delta", "text": delta.content}

    if delta.tool_calls:
        for tc_delta in delta.tool_calls:
            _accumulate_tool_delta(state.tool_map, tc_delta)


def _tool_calls_from_map(tool_map: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    tool_calls = []
    for idx in sorted(tool_map):
        entry = tool_map[idx]
        try:
            args = json.loads(entry["args_json"]) if entry["args_json"] else {}
        except json.JSONDecodeError:
            args = {}
        call = {"id": entry["id"], "name": entry["name"], "args": args}
        if entry.get("extra_content"):
            call["extra_content"] = entry["extra_content"]
        tool_calls.append(call)
    return tool_calls


def _extract_text_tool_calls(text: str) -> tuple[dict[int, dict[str, Any]], str]:
    tool_map: dict[int, dict[str, Any]] = {}
    for match in re.finditer(
        r"<tool_call>\s*\{.*?\"name\"\s*:\s*\"([^\"]+)\".*?\"arguments\"\s*:\s*(\{.*?\})\s*\}\s*</tool_call>",
        text,
        re.DOTALL,
    ):
        name, args_text = match.group(1), match.group(2)
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError:
            args = {}
        tool_map[len(tool_map)] = {
            "id": f"text_tc_{len(tool_map)}",
            "name": name,
            "args_json": json.dumps(args, ensure_ascii=False),
        }
    if tool_map:
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()
    return tool_map, text


def _usage_event(state: OpenAIStreamState) -> dict[str, Any]:
    return {
        "type": "usage",
        "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens,
        "cache_read_tokens": state.cache_read,
        "cache_write_tokens": state.cache_write,
    }


class OpenAIProvider(LLMProvider):
    """通过 openai SDK 调用 OpenAI 模型。"""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str = ""):
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": _TIMEOUT}
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self._client = openai.OpenAI(**kwargs)
        self._model = model

    @property
    def name(self) -> str:
        return f"OpenAI ({self._model})"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]:
        # 构建 OpenAI messages
        oai_messages = self._build_messages(messages, system_prompt)

        # 构建工具声明
        oai_tools = self._build_tools(tools) if tools else None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "frequency_penalty": 0.3,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        result = self._parse_response(response)
        if hasattr(response, "usage") and response.usage:
            p_details = getattr(response.usage, "prompt_tokens_details", None)
            c_details = getattr(response.usage, "completion_tokens_details", None)
            result["usage"] = {
                "input_tokens": response.usage.prompt_tokens or 0,
                "output_tokens": response.usage.completion_tokens or 0,
                "cache_read_tokens": (getattr(p_details, "cached_tokens", 0) or 0) if p_details else 0,
                "cache_write_tokens": (getattr(c_details, "cached_tokens", 0) or 0) if c_details else 0,
            }
        return result

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        oai_messages = self._build_messages(messages, system_prompt)
        oai_tools = self._build_tools(tools) if tools else None
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "frequency_penalty": 0.3,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        state = OpenAIStreamState()
        stream = _create_openai_stream(self._client, kwargs)

        try:
            for chunk in stream:
                if _consume_usage_chunk(state, chunk):
                    continue
                if not chunk.choices:
                    continue
                yield from _consume_delta_events(state, chunk.choices[0].delta)
        finally:
            if hasattr(stream, "close"):
                stream.close()

        if not state.tool_map and "<tool_call>" in state.text_buf:
            state.tool_map, state.text_buf = _extract_text_tool_calls(state.text_buf)
        if state.tool_map:
            yield {"type": "tool_calls", "tool_calls": _tool_calls_from_map(state.tool_map), "text": state.text_buf}
        yield _usage_event(state)

    def _build_messages(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """将统一消息格式转为 OpenAI messages 格式。"""
        oai_msgs = []
        if system_prompt:
            oai_msgs.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg["role"]

            if role == "user":
                oai_msgs.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                oai_msg: dict[str, Any] = {"role": "assistant"}
                if msg.get("content"):
                    oai_msg["content"] = msg["content"]
                if msg.get("reasoning_content"):
                    oai_msg["reasoning_content"] = msg["reasoning_content"]
                if msg.get("tool_calls"):
                    oai_msg["tool_calls"] = [_openai_tool_call_payload(tc) for tc in msg["tool_calls"]]
                oai_msgs.append(oai_msg)

            elif role == "tool":
                result = msg["content"]
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                oai_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": result,
                    }
                )

        return oai_msgs

    def _build_tools(self, tools: list[dict]) -> list[dict]:
        """将标准 function schema 转为 OpenAI tools 格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    def _parse_response(self, response) -> dict[str, Any]:
        """解析 OpenAI 响应为统一格式。"""
        choice = response.choices[0]
        message = choice.message

        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                call = {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args,
                }
                extra_content = _openai_extra_content(tc)
                if extra_content:
                    call["extra_content"] = extra_content
                tool_calls.append(call)
            return {
                "type": "tool_calls",
                "tool_calls": tool_calls,
                "text": message.content or "",
            }

        return {"type": "text", "text": message.content or ""}
