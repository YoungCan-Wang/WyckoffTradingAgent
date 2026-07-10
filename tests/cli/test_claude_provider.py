from __future__ import annotations

from cli.providers.claude import ClaudeProvider


def test_build_messages_drops_unsigned_reasoning_content():
    provider = ClaudeProvider.__new__(ClaudeProvider)

    messages = [
        {"role": "user", "content": "查一下持仓"},
        {
            "role": "assistant",
            "content": "我需要调用工具。",
            "reasoning_content": "Need to inspect portfolio first.",
            "tool_calls": [{"id": "tc_1", "name": "portfolio", "args": {"mode": "view"}}],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": {"positions": []}},
    ]

    built = provider._build_messages(messages)

    assistant_content = built[1]["content"]
    assert [block["type"] for block in assistant_content] == ["text", "tool_use"]
    assert "thinking" not in {block["type"] for block in assistant_content}
