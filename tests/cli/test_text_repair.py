"""落单代理字符的修复 —— Windows 用户遇到的 `\\udca5` 报错。

网关把 `💥`（UTF-16 代理对 `\\ud83d\\udca5`）拆到相邻两个 SSE delta 里，两半都
成了落单的代理字符。落单代理没法 strict UTF-8 编码，而 IPC 写盘、chat_log 落
SQLite、scratchpad 落 JSONL 全是 strict —— 任何一处抛异常，整轮回答都会被
run_turn 的兜底 except 吞掉，界面上只剩一行编码错误。
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from cli.ipc.remote import _Outbox
from cli.ipc.session import _cap, _project
from cli.runtime import _repair_split_chars
from cli.scratchpad import AgentScratchpad
from cli.text_repair import StreamTextRepair, repair_text
from tests.helpers.agent_loop_harness import AgentLoopHarness

# 用户截图里的那个字符：U+1F4A5 💥 == 💥
EMOJI_HIGH = "\ud83d"
EMOJI_LOW = "\udca5"

# 「股」= e8 82 a1，被按字节拆开时以 surrogateescape 形式到达
CN_BYTES = ("\udce8\udc82", "\udca1")


def encodable(text: str) -> str:
    """编一次 strict UTF-8 —— 编不过就是本 bug 复现了。"""
    text.encode("utf-8")
    return text


def test_lone_surrogate_is_not_encodable_without_repair():
    # 先钉住前提：不修的话下游确实会炸。
    with pytest.raises(UnicodeEncodeError):
        (EMOJI_HIGH + EMOJI_LOW).encode("utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("正常文本 💥 ok", "正常文本 💥 ok"),  # 干净的内容原样放过
        ("\udce8\udc82\udca1", "股"),  # 一块里的 UTF-8 字节碎片能还原
        (EMOJI_HIGH + EMOJI_LOW, "💥"),  # 相邻的代理对合起来
        ("x" + EMOJI_LOW, "x�"),  # 真落单的换成 U+FFFD
        ("a" + EMOJI_HIGH + "b", "a�b"),
    ],
)
def test_repair_text(raw: str, expected: str):
    assert encodable(repair_text(raw)) == expected


def test_split_emoji_across_chunks_is_rejoined():
    repair = StreamTextRepair()
    out = "".join(repair.feed(c) for c in ("大盘", EMOJI_HIGH, EMOJI_LOW + " 走弱"))
    assert encodable(out + repair.flush()) == "大盘💥 走弱"


def test_split_utf8_bytes_across_chunks_is_rejoined():
    repair = StreamTextRepair()
    out = "".join(repair.feed(c) for c in (CN_BYTES[0], CN_BYTES[1] + "票"))
    assert encodable(out + repair.flush()) == "股票"


def test_stream_ending_mid_character_still_emits_the_tail():
    """流结束在半个字符上时，攥着的尾巴要放出来 —— 否则那点内容凭空消失。"""
    repair = StreamTextRepair()
    body = repair.feed("收盘" + EMOJI_HIGH)
    assert encodable(body) == "收盘"
    assert encodable(repair.flush()) == "�"


def test_runtime_stream_repairs_split_emoji_and_keeps_other_chunks():
    chunks = [
        {"type": "text_delta", "text": "大盘", "round": 1},
        {"type": "text_delta", "text": EMOJI_HIGH, "round": 1},
        {"type": "text_delta", "text": EMOJI_LOW + " 走弱", "round": 1},
        {"type": "usage", "input_tokens": 10, "output_tokens": 20},
    ]

    events = list(_repair_split_chars(iter(chunks)))

    text = "".join(encodable(e["text"]) for e in events if e["type"] == "text_delta")
    assert text == "大盘💥 走弱"
    # 非文本 chunk 原样透传，round 之类的字段不能丢
    assert {"type": "usage", "input_tokens": 10, "output_tokens": 20} in events
    assert all(e.get("round") == 1 for e in events if e["type"] == "text_delta")


def test_runtime_stream_repairs_tool_calls_accumulated_text():
    """tool_calls 也带 text，是 provider 自己累计的串，不走逐块修复。"""
    chunks = [{"type": "tool_calls", "tool_calls": [], "text": "调用前正文" + EMOJI_LOW}]

    (event,) = list(_repair_split_chars(iter(chunks)))

    assert encodable(event["text"]) == "调用前正文�"


def test_runtime_stream_flushes_tail_before_non_text_chunk():
    """攥着的尾巴要排在 tool_calls 之前 —— 那块带的是累计正文，顺序颠倒会读乱。"""
    chunks = [
        {"type": "text_delta", "text": "正文" + EMOJI_HIGH},
        {"type": "tool_calls", "tool_calls": [], "text": ""},
    ]

    types = [e["type"] for e in _repair_split_chars(iter(chunks))]

    assert types == ["text_delta", "text_delta", "tool_calls"]


def test_runtime_stream_repairs_thinking_deltas_independently():
    """思维链和正文各自攥尾巴 —— 交错时不能把两条流的半个字符拼到一起。"""
    chunks = [
        {"type": "thinking_delta", "text": EMOJI_HIGH},
        {"type": "text_delta", "text": "正文"},
        {"type": "thinking_delta", "text": EMOJI_LOW},
    ]

    events = list(_repair_split_chars(iter(chunks)))

    thinking = "".join(encodable(e["text"]) for e in events if e["type"] == "thinking_delta")
    assert thinking == "💥"


def test_full_turn_answer_survives_split_emoji():
    """用户遇到的那一幕：整轮回答本来会被换成一行编码错误。

    done 事件的 text 由 runtime 累加得来，也是落 chat_log SQLite 的那份，
    所以这里同时钉住了持久化不会炸。
    """
    harness = AgentLoopHarness(
        rounds=[
            [
                {"type": "text_delta", "text": "今日市场观察：大盘"},
                {"type": "text_delta", "text": EMOJI_HIGH},
                {"type": "text_delta", "text": EMOJI_LOW + " 走弱。"},
                {"type": "usage", "input_tokens": 8, "output_tokens": 7},
            ]
        ]
    )

    outcome = harness.run_turn([{"role": "user", "content": "跑今天的市场观察，给我研报"}])

    assert encodable(outcome["result"]["text"]) == "今日市场观察：大盘💥 走弱。"


def test_ipc_projection_survives_lone_surrogate():
    """_cap 里的 len(...encode("utf-8")) 是原来的抛出点。"""
    event = _project({"type": "text_delta", "text": "大" + EMOJI_LOW})
    assert encodable(event["text"]) == "大�"


def test_ipc_cap_still_truncates_after_repair():
    """修复不能破坏原有的截断行为。"""
    from cli.ipc.session import MAX_IPC_FIELD_BYTES

    capped = _cap("阿" * MAX_IPC_FIELD_BYTES)
    assert "已在 IPC 层截断" in capped
    assert len(encodable(capped).encode("utf-8")) < MAX_IPC_FIELD_BYTES + 200


def test_repaired_text_can_be_written_to_sqlite_and_json():
    """chat_log 的 SQLite 写：绑参是 strict UTF-8。"""
    repaired = repair_text("大" + EMOJI_LOW + "盘")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (repaired,))
    assert conn.execute("SELECT x FROM t").fetchone()[0] == repaired

    assert json.dumps({"content": repaired}, ensure_ascii=False).encode("utf-8")


def test_scratchpad_append_survives_lone_surrogate(tmp_path):
    """scratchpad 的 JSONL 写在 runtime 主流程上，没有 try 包着。"""
    scratchpad = AgentScratchpad("市场观察", session_id="enc-test", scratchpad_dir=tmp_path)

    scratchpad.record_thinking("盘中" + EMOJI_LOW)

    written = json.loads(scratchpad.path.read_text(encoding="utf-8").splitlines()[-1])
    assert encodable(written["content"]) == "盘中�"


def test_remote_outbox_survives_lone_surrogate():
    """远程通道的帧长计算 len(raw.encode("utf-8")) 也是 strict 的。"""
    sent: list[str] = []
    outbox = _Outbox(sent.append)
    try:
        outbox.put({"type": "text_delta", "text": "大盘" + EMOJI_LOW})
        deadline = time.monotonic() + 2.0
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        outbox.close()

    assert sent, "帧没发出去"
    assert encodable(json.loads(sent[0])["text"]) == "大盘�"
