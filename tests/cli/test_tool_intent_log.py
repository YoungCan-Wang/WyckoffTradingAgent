"""工具执行的两阶段落盘：先写意图，执行，再写结果。

为什么需要意图那一半：原来只有 record_tool_result，也就是副作用发生完才写盘。
进程在工具跑到一半被 SIGKILL（或断电）时，那次调用在日志里完全无痕 ——
无法区分「没跑」和「跑了但结果丢了」。

这个项目的工具会真的改持仓、设止损、写报告文件，所以「有没有可能已经改了但
我不知道」是必须能回答的问题。审批队列早就是这个形状（pending → 执行 → 结果），
这里只是把它推广到所有工具。
"""

from __future__ import annotations

import json

import pytest

from cli.scratchpad import AgentScratchpad, dangling_tool_calls


@pytest.fixture
def pad(tmp_path):
    return AgentScratchpad("测试查询", session_id="t1", scratchpad_dir=tmp_path)


def _types(pad) -> list[str]:
    return [json.loads(line)["type"] for line in pad.path.read_text(encoding="utf-8").strip().split("\n")]


def test_intent_is_written_before_the_result(pad):
    pad.record_tool_start("portfolio", {"mode": "view"}, tool_call_id="c1")
    pad.record_tool_result("portfolio", {"mode": "view"}, {"ok": True}, tool_call_id="c1")
    assert _types(pad) == ["init", "tool_started", "tool_result"]


def test_intent_lands_on_disk_immediately(pad):
    """不能等到轮结束才 flush —— 那样崩溃时正是最需要的那条记录会丢。"""
    pad.record_tool_start("set_stop_loss", {"code": "600519"}, tool_call_id="c1")
    assert "tool_started" in pad.path.read_text(encoding="utf-8")


def test_completed_call_is_not_dangling(pad):
    pad.record_tool_start("portfolio", {}, tool_call_id="c1")
    pad.record_tool_result("portfolio", {}, {"ok": True}, tool_call_id="c1")
    assert dangling_tool_calls(pad.path) == []


def test_started_without_result_is_dangling(pad):
    """这是整件事的意义：崩溃后能指出「这个工具跑了，结果不明」。"""
    pad.record_tool_start("set_stop_loss", {"code": "600519", "price": 1400}, tool_call_id="c1")
    hanging = dangling_tool_calls(pad.path)
    assert len(hanging) == 1
    assert hanging[0]["toolName"] == "set_stop_loss"
    # 参数要带上，否则只知道「有事发生」却不知道发生在哪只票上
    assert hanging[0]["args"]["code"] == "600519"


def test_same_tool_twice_pairs_by_id_not_name(pad):
    """同一轮里两次调用同名工具，只有第二次挂起。

    按工具名配对会让第一次的结果「认领」掉第二次的开始记录，于是漏报。
    并发路径下同名工具并行是常见情形（比如一次分析多只票）。
    """
    pad.record_tool_start("analyze_stock", {"code": "600519"}, tool_call_id="c1")
    pad.record_tool_start("analyze_stock", {"code": "000001"}, tool_call_id="c2")
    pad.record_tool_result("analyze_stock", {"code": "600519"}, {"ok": True}, tool_call_id="c1")
    hanging = dangling_tool_calls(pad.path)
    assert [h["args"]["code"] for h in hanging] == ["000001"]


def test_half_written_last_line_does_not_break_the_reader(pad, tmp_path):
    """被 kill 时最后一行可能只写了一半，半条 JSON 不该让整份日志不可读。"""
    pad.record_tool_start("portfolio", {}, tool_call_id="c1")
    with pad.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "tool_res')
    assert len(dangling_tool_calls(pad.path)) == 1


def test_entries_without_ids_are_skipped_not_guessed(pad):
    """历史文件没有 toolCallId。宁可漏报也不要给出错的悬空清单。"""
    pad.append({"type": "tool_started", "toolName": "old"})
    assert dangling_tool_calls(pad.path) == []


def test_missing_file_is_not_an_error(tmp_path):
    assert dangling_tool_calls(tmp_path / "nope.jsonl") == []
