"""Gemini provider thought_signature round-trip tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.providers.gemini import (
    GeminiProvider,
    function_call_part_from_tool_call,
    tool_call_dict_from_part,
)
from cli.providers.openai import _extract_text_tool_calls, _openai_tool_call_payload


def test_tool_call_dict_from_part_preserves_thought_signature():
    part = SimpleNamespace(
        function_call=SimpleNamespace(name="view_portfolio", args={}),
        thought_signature=b"sig-bytes-123",
    )
    call = tool_call_dict_from_part(part, call_id="call-1")
    assert call["id"] == "call-1"
    assert call["name"] == "view_portfolio"
    assert call["args"] == {}
    assert call["thought_signature"] == b"sig-bytes-123"


def test_function_call_part_from_tool_call_round_trip():
    tc = {
        "id": "call-1",
        "name": "view_portfolio",
        "args": {},
        "thought_signature": b"sig-bytes-123",
    }
    part = function_call_part_from_tool_call(tc)
    assert part.function_call is not None
    assert part.function_call.name == "view_portfolio"
    assert part.thought_signature == b"sig-bytes-123"


def test_build_contents_includes_thought_signature_on_model_parts():
    provider = GeminiProvider(api_key="test-key", model="gemini-3.1-flash-lite")
    contents = provider._build_contents(
        [
            {"role": "user", "content": "我有什么持仓"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "view_portfolio",
                        "args": {},
                        "thought_signature": b"sig-bytes-123",
                    }
                ],
            },
            {
                "role": "tool",
                "name": "view_portfolio",
                "content": '{"result":"ok"}',
            },
        ]
    )
    assert len(contents) == 3
    model_parts = contents[1].parts
    assert len(model_parts) == 1
    assert model_parts[0].thought_signature == b"sig-bytes-123"
    assert model_parts[0].function_call is not None
    assert model_parts[0].function_call.name == "view_portfolio"


def test_openai_tool_call_payload_preserves_extra_content():
    payload = _openai_tool_call_payload(
        {
            "id": "call-1",
            "name": "view_portfolio",
            "args": {},
            "extra_content": {"google": {"thought_signature": "abc"}},
        }
    )
    assert payload["extra_content"] == {"google": {"thought_signature": "abc"}}


def test_extract_text_tool_calls_removes_tagged_payload():
    text = 'before <tool_call>{"name":"view_portfolio","arguments":{}}</tool_call> after'

    tool_map, cleaned = _extract_text_tool_calls(text)

    assert cleaned == "before  after"
    assert tool_map[0]["id"] == "text_tc_0"
    assert tool_map[0]["name"] == "view_portfolio"
    assert tool_map[0]["args_json"] == "{}"


@pytest.mark.parametrize("thought_signature", [b"bytes-sig", "str-sig"])
def test_tool_call_dict_accepts_string_or_bytes_signature(thought_signature):
    part = SimpleNamespace(
        function_call=SimpleNamespace(name="x", args={"k": 1}),
        thought_signature=thought_signature,
    )
    call = tool_call_dict_from_part(part)
    assert call["thought_signature"] is not None
