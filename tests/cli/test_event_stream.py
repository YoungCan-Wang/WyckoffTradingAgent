from __future__ import annotations

import json
from pathlib import Path

from cli.event_stream import EVENT_SCHEMA, load_scratchpad_events, scratchpad_events_jsonl


def test_load_scratchpad_events_normalizes_trace(tmp_path: Path):
    trace = tmp_path / "run.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"type": "init", "timestamp": "2026-01-01T00:00:00", "session_id": "s1", "content": "hi"}),
                json.dumps(
                    {
                        "type": "tool_result",
                        "timestamp": "2026-01-01T00:00:01",
                        "toolName": "analyze_stock",
                        "args": {"code": "000001"},
                        "result": {"ok": True},
                        "durationMs": 12,
                    }
                ),
                json.dumps({"type": "final", "timestamp": "2026-01-01T00:00:02", "content": "done"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events = load_scratchpad_events(trace)

    assert [event["type"] for event in events] == ["user_message", "tool_result", "assistant_message"]
    assert events[0]["schema"] == EVENT_SCHEMA
    assert events[0]["session_id"] == "s1"
    assert events[1]["tool_name"] == "analyze_stock"
    assert events[1]["duration_ms"] == 12
    assert json.loads(scratchpad_events_jsonl([trace]).splitlines()[0])["schema"] == EVENT_SCHEMA
