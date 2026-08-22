"""聊天产物事件：把工具结果翻译成「右侧面板能打开的东西」。

为什么需要这层建模：原来 K 线是在 `tool_start` 时由前端副作用打开的 ——
工具还没成功就开面板，失败会留一个空面板；而且 `action=list/clear` 也会开图。
报告则靠「>400 字且有标题或表格」猜出来。两条路径都没有可寻址的产物标识，
所以关掉页签就找不回来。

翻译刻意放在 IPC 层而不是 runtime：runtime 是 CLI / TUI / 桌面共用的，
「右侧面板」是桌面独有概念。而且这里是既有的安全边界，工具结果必须按字段
白名单挑，不能整个透传。
"""

from __future__ import annotations

from cli.ipc.session import _ARTIFACT_TOOLS, _chat_artifact


def _draw_event(**overrides):
    event = {
        "type": "tool_result",
        "name": "annotate_chart",
        "args": {"code": "600519", "action": "draw", "timeframe": "1d"},
        "result": {"chart_id": "cn:600519:1d", "count": 3},
        "tool_call_id": "call_7",
        "status": "ok",
    }
    event.update(overrides)
    return event


def test_successful_draw_becomes_a_ready_artifact():
    out = _chat_artifact(_draw_event(), "turn-1")
    assert out is not None
    assert out["type"] == "chat_artifact"
    assert out["kind"] == "kline"
    assert out["status"] == "ready"
    assert out["title"] == "600519"
    assert out["payload"] == {"symbol": "600519", "timeframe": "1d"}


def test_artifact_id_is_not_called_id():
    """传输层会用请求流 id 覆盖 event["id"]（stdio.py 的既有约定）。

    产物标识若叫 "id" 就会被冲掉，前端拿不到稳定标识、无法去重。
    审批事件当初也是因为同一个原因改用 approval_id。
    """
    out = _chat_artifact(_draw_event(), "turn-1")
    assert "id" not in out
    assert out["artifact_id"] == "turn-1:call_7"


def test_id_combines_turn_and_call_so_repeats_dedupe():
    first = _chat_artifact(_draw_event(), "turn-1")
    same = _chat_artifact(_draw_event(), "turn-1")
    later_turn = _chat_artifact(_draw_event(), "turn-2")
    assert first["artifact_id"] == same["artifact_id"], "同一次调用应给出同一个 id"
    assert first["artifact_id"] != later_turn["artifact_id"], "不同轮次不能撞 id"


def test_two_calls_in_one_turn_get_distinct_ids():
    """一轮里画多只票是常态（每只一次调用），它们必须是不同的产物。"""
    a = _chat_artifact(_draw_event(tool_call_id="call_1"), "turn-9")
    b = _chat_artifact(_draw_event(tool_call_id="call_2", args={"code": "000001", "action": "draw"}), "turn-9")
    assert a["artifact_id"] != b["artifact_id"]
    assert a["title"] == "600519"
    assert b["title"] == "000001"


def test_failed_tool_still_produces_a_failed_artifact():
    """失败也要有产物 —— 否则「工具跑了但什么都没出现」看起来像没发生过。

    但状态是 failed，前端据此不自动展开面板，只在对话里留一张失败卡片。
    """
    out = _chat_artifact(_draw_event(status="error", result={"error": "认不出这个代码"}), "turn-1")
    assert out["status"] == "failed"


def test_error_in_result_counts_as_failure_even_when_status_is_ok():
    """工具用返回值报错（{"error": ...}）而不是抛异常时，status 仍是 ok。

    只看 status 会把这种失败当成成功、把空图弹到用户面前。
    """
    out = _chat_artifact(_draw_event(result={"error": "annotations 必须是数组"}), "turn-1")
    assert out["status"] == "failed"


def test_list_and_clear_are_not_artifacts():
    """只有 draw 是「画了一张图」。

    旧实现在 tool_start 上判断，`action=list`（只是列出标注）也会弹开图表页。
    """
    for action in ("list", "clear"):
        assert _chat_artifact(_draw_event(args={"code": "600519", "action": action}), "turn-1") is None


def test_missing_code_is_not_an_artifact():
    assert _chat_artifact(_draw_event(args={"action": "draw"}), "turn-1") is None


def test_non_artifact_tools_are_ignored():
    """白名单而非黑名单：新增工具默认不产出产物。

    反过来（默认产出、遇到不想要的再排除）会让每个新工具都可能意外弹开面板。
    """
    for name in ("portfolio", "set_stop_loss", "update_portfolio", "exec_command"):
        assert _chat_artifact(_draw_event(name=name), "turn-1") is None
    assert "annotate_chart" in _ARTIFACT_TOOLS


def test_payload_does_not_carry_the_raw_tool_result():
    """payload 是白名单挑出来的，不是 result 的副本。

    工具结果里可能带凭据和内部结构；而且标注内容本身可能几 KB，图自己会去
    后端取，事件里重复塞一份只是让每次调用都变重。
    """
    out = _chat_artifact(
        _draw_event(result={"chart_id": "x", "annotations": [{"secret": "s"}] * 50, "token": "leak"}),
        "turn-1",
    )
    assert set(out["payload"]) == {"symbol", "timeframe"}
    assert "leak" not in str(out)
    assert "secret" not in str(out)


def test_non_dict_args_and_result_do_not_crash():
    """模型可以让工具收到奇怪的参数；翻译层不该因此抛异常。"""
    assert _chat_artifact(_draw_event(args=None), "turn-1") is None
    out = _chat_artifact(_draw_event(result=None), "turn-1")
    assert out is not None and out["status"] == "ready"


def test_timeframe_defaults_when_absent():
    out = _chat_artifact(_draw_event(args={"code": "600519", "action": "draw"}), "turn-1")
    assert out["payload"]["timeframe"] == "1d"
