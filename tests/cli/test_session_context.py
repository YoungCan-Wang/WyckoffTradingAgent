from __future__ import annotations

import json

from cli.session_context import build_resumed_model_context


def _row(role: str, content: str, *, metadata: dict | None = None) -> dict:
    return {
        "role": role,
        "content": content,
        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
        "tool_calls": "",
    }


def test_resume_prefers_saved_message_snapshot_with_tool_results():
    snapshot = [
        {"role": "user", "content": "帮我看 600519"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "name": "analyze_stock", "args": {"code": "600519"}}],
        },
        {"role": "tool", "name": "analyze_stock", "tool_call_id": "call_1", "content": '{"latest_close": 123}'},
        {"role": "assistant", "content": "600519 现在偏弱。"},
    ]
    rows = [
        _row("user", "帮我看 600519"),
        _row("assistant", "600519 现在偏弱。", metadata={"messages": snapshot}),
    ]

    result = build_resumed_model_context(rows)

    assert result.mode == "full"
    assert any(message.get("role") == "tool" for message in result.messages)
    assert result.messages[-1]["content"] == "600519 现在偏弱。"


def test_resume_falls_back_to_visible_chat_rows_without_snapshot():
    rows = [
        _row("user", "我持仓有什么"),
        _row("assistant", "你有两只持仓。"),
        _row("error", "", metadata={"error": "ignored"}),
    ]

    result = build_resumed_model_context(rows)

    assert result.mode == "fallback_full"
    assert result.messages == [
        {"role": "user", "content": "我持仓有什么"},
        {"role": "assistant", "content": "你有两只持仓。"},
    ]


def test_resume_summarizes_oversized_history(monkeypatch):
    monkeypatch.setattr("cli.session_context.RESUME_CONTEXT_TOKEN_BUDGET", 500)
    monkeypatch.setattr("cli.session_context.RESUME_TAIL_TOKEN_BUDGET", 220)
    snapshot = []
    for idx in range(30):
        snapshot.append({"role": "user", "content": f"历史问题 {idx} 600519 " + "长内容" * 30})
        snapshot.append({"role": "assistant", "content": f"历史回答 {idx} " + "分析" * 30})
    rows = [_row("assistant", "最后回答", metadata={"messages": snapshot})]

    result = build_resumed_model_context(rows)

    assert result.mode == "summary_tail"
    assert result.messages[0]["content"].startswith("[SYSTEM CONTEXT - RESUMED SESSION]")
    assert "600519" in result.messages[0]["content"]
    assert result.messages[-1]["content"].startswith("历史回答 29")
