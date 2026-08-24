"""远程传输：手机通过云端信箱调用同一批 IPC 方法。

方法层是传输无关的，所以这里不测任何业务逻辑 —— 只测「换了管道之后仍然对」，
以及三件 stdio 没有的事：背压、重连、远程不可用的方法。
"""

from __future__ import annotations

import json
import threading

import pytest

from cli.ipc import remote as R


class Sink:
    """收集发出去的消息。可以装成「发送失败」。"""

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.fail_after = fail_after
        self.done = threading.Event()

    def __call__(self, raw: str) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("socket closed")
        self.sent.append(raw)
        self.done.set()

    def types(self) -> list[str]:
        return [json.loads(r).get("type") for r in self.sent]

    def wait(self, count: int, timeout: float = 2.0) -> bool:
        deadline = threading.Event()
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.sent) >= count:
                return True
            deadline.wait(0.02)
        return len(self.sent) >= count


@pytest.fixture
def outbox():
    boxes: list[R._Outbox] = []

    def make(sink: Sink) -> R._Outbox:
        box = R._Outbox(sink)
        boxes.append(box)
        return box

    yield make
    for box in boxes:
        box.close()


def test_events_are_forwarded(outbox):
    sink = Sink()
    box = outbox(sink)
    box.put({"id": 1, "type": "done", "text": "结论"})
    assert sink.wait(1)
    assert json.loads(sink.sent[0]) == {"id": 1, "type": "done", "text": "结论"}


def test_oversized_frames_become_an_error_not_a_stuck_connection(outbox):
    """超大帧会让整条连接卡住，而不只是慢。所以在入队前就换成错误。"""
    sink = Sink()
    box = outbox(sink)
    box.put({"id": 7, "to": "phone-a", "type": "tool_start", "args": {"html": "x" * (R.MAX_FRAME_BYTES + 10)}})
    assert sink.wait(1)
    payload = json.loads(sink.sent[0])
    assert payload["code"] == "frame_too_large"
    # 错误也要能路由回发起的设备，否则手机永远等不到回应
    assert payload["to"] == "phone-a"
    assert payload["id"] == 7


class TestBackpressure:
    """stdio 靠管道阻塞天然限流；WebSocket 的 send 不阻塞，得自己做。"""

    def _stalled_box(self) -> R._Outbox:
        # 发送端永不消费：模拟弱网下队列堆积
        blocked = threading.Event()
        box = R._Outbox(lambda _raw: blocked.wait())
        return box

    def test_queue_is_bounded(self):
        box = self._stalled_box()
        try:
            for i in range(R.MAX_QUEUED + 200):
                box.put({"id": 1, "type": "text_delta", "text": f"{i}"})
            assert len(box._queue) <= R.MAX_QUEUED
        finally:
            box.close()

    def test_status_events_survive_a_flood_of_text(self):
        """正文可以缺一段（手机上本来就快速滚过）；done 丢了界面会永久卡住。"""
        box = self._stalled_box()
        try:
            box.put({"id": 1, "type": "approval_pending", "approval_id": "a1"})
            for i in range(R.MAX_QUEUED + 300):
                box.put({"id": 1, "type": "text_delta", "text": f"{i}"})
            box.put({"id": 1, "type": "done", "text": "结论"})
            kinds = [json.loads(r).get("type") for r in box._queue]
            assert "done" in kinds
            assert "approval_pending" in kinds
            assert box._dropped > 0
        finally:
            box.close()

    def test_oldest_text_is_shed_first(self):
        """丢队首:那是用户已经滚过去的内容。队尾是刚发生的事。"""
        box = self._stalled_box()
        try:
            for i in range(R.MAX_QUEUED + 50):
                box.put({"id": 1, "type": "text_delta", "text": f"seq{i}"})
            remaining = [json.loads(r).get("text") for r in box._queue]
            assert "seq0" not in remaining
            assert f"seq{R.MAX_QUEUED + 49}" in remaining
        finally:
            box.close()

    def test_a_flood_of_status_events_still_bounded(self):
        """全是不可丢的类型时也不能无界增长 —— 内存比完整性重要。"""
        box = self._stalled_box()
        try:
            for i in range(R.MAX_QUEUED + 100):
                box.put({"id": i, "type": "approval_pending"})
            assert len(box._queue) <= R.MAX_QUEUED
        finally:
            box.close()


class TestDispatch:
    """请求进来 → 方法层 → 事件带着 to 回去。"""

    def _bridge(self, sink: Sink) -> R.RemoteBridge:
        bridge = R.RemoteBridge("wss://example/ws", "token")
        bridge._outbox = R._Outbox(sink)
        return bridge

    def test_a_request_is_dispatched_and_terminated(self, monkeypatch):
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "cli.ipc.methods.dispatch",
            lambda m, p: (seen.append((m, p)), iter([{"type": "result", "ok": True}]))[1],
        )
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 9, "from": "phone-a", "method": "portfolio", "params": {}}))
            assert sink.wait(2)
            assert seen == [("portfolio", {})]
            assert sink.types() == ["result", "end"]
        finally:
            bridge._outbox.close()

    def test_responses_carry_the_origin_device(self, monkeypatch):
        """不带 to 的话信箱会把回复广播给所有在线手机。"""
        monkeypatch.setattr("cli.ipc.methods.dispatch", lambda _m, _p: iter([{"type": "result"}]))
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 1, "from": "phone-b", "method": "account"}))
            assert sink.wait(2)
            assert all(json.loads(r)["to"] == "phone-b" for r in sink.sent)
        finally:
            bridge._outbox.close()

    def test_transport_id_is_not_clobbered_by_business_id(self, monkeypatch):
        """和 stdio 同一个坑（stdio.py:73）：业务事件自带 id 会让对面分发失败。"""
        monkeypatch.setattr(
            "cli.ipc.methods.dispatch",
            lambda _m, _p: iter([{"type": "approval_pending", "id": "business-id", "approval_id": "a1"}]),
        )
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 42, "from": "p", "method": "chat"}))
            assert sink.wait(1)
            assert json.loads(sink.sent[0])["id"] == 42
            assert json.loads(sink.sent[0])["approval_id"] == "a1"
        finally:
            bridge._outbox.close()

    def test_method_errors_are_reported_and_terminated(self, monkeypatch):
        from cli.ipc.methods import MethodError

        def boom(_m, _p):
            raise MethodError("not_found", "会话不存在")
            yield

        monkeypatch.setattr("cli.ipc.methods.dispatch", boom)
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 1, "from": "p", "method": "chat_load"}))
            assert sink.wait(2)
            assert json.loads(sink.sent[0])["code"] == "not_found"
            assert sink.types()[-1] == "end", "必须有 end，否则手机永远转圈"
        finally:
            bridge._outbox.close()

    def test_unexpected_exceptions_still_terminate(self, monkeypatch):
        def boom(_m, _p):
            raise RuntimeError("挂了")
            yield

        monkeypatch.setattr("cli.ipc.methods.dispatch", boom)
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 1, "from": "p", "method": "x"}))
            assert sink.wait(2)
            assert json.loads(sink.sent[0])["code"] == "internal"
            assert sink.types()[-1] == "end"
        finally:
            bridge._outbox.close()

    @pytest.mark.parametrize("raw", ["not json", '"a string"', "[1,2]", "null"])
    def test_malformed_messages_are_ignored(self, raw):
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(raw)  # 不该抛
            assert sink.sent == []
        finally:
            bridge._outbox.close()

    def test_empty_thinking_frames_do_not_cross(self, monkeypatch):
        """实测一轮对话有 88 个纯空的 thinking_delta。

        方法层已把内容剥掉（推理不跨 IPC 边界），但空壳仍逐个过河。本地管道无所谓，
        公网上是 88 个无用帧，弱网下还会挤掉真正有用的事件。
        """
        monkeypatch.setattr(
            "cli.ipc.methods.dispatch",
            lambda _m, _p: iter(
                [{"type": "thinking_delta"}] * 50 + [{"type": "text_delta", "text": "答"}, {"type": "done"}]
            ),
        )
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"id": 1, "from": "p", "method": "chat"}))
            assert sink.wait(3)
            import time

            time.sleep(0.2)
            assert "thinking_delta" not in sink.types()
            assert "text_delta" in sink.types() and "done" in sink.types()
        finally:
            bridge._outbox.close()

    @pytest.mark.parametrize("kind", ["presence", "host_offline"])
    def test_relay_status_frames_are_not_treated_as_requests(self, kind):
        sink = Sink()
        bridge = self._bridge(sink)
        try:
            bridge._handle(json.dumps({"type": kind, "host_online": True}))
            assert sink.sent == []
        finally:
            bridge._outbox.close()


class TestDesktopOnlyMethods:
    """用户要求手机和电脑权限一致。这些不是权限限制，是物理上做不到的事。"""

    @pytest.mark.parametrize("method", sorted(R.REMOTE_UNAVAILABLE))
    def test_they_are_refused_with_a_human_reason(self, method):
        sink = Sink()
        bridge = R.RemoteBridge("wss://x", "t")
        bridge._outbox = R._Outbox(sink)
        try:
            bridge._handle(json.dumps({"id": 1, "from": "p", "method": method}))
            assert sink.wait(2)
            payload = json.loads(sink.sent[0])
            assert payload["code"] == "desktop_only"
            # 要说明「为什么在手机上做不了」，不是一句 unsupported
            assert "电脑" in payload["message"]
            assert sink.types()[-1] == "end"
        finally:
            bridge._outbox.close()

    def test_business_methods_are_not_blocked(self):
        """对话、持仓、止损、审批 —— 手机全都有，和电脑一样。"""
        for method in ("chat", "portfolio", "portfolio_edit", "portfolio_set_stop", "approve_decide"):
            assert method not in R.REMOTE_UNAVAILABLE


class TestBridgeLifecycle:
    def test_status_reports_disconnected_before_start(self):
        R.stop_bridge()
        assert R.bridge_status() == {"running": False, "connected": False}

    def test_starting_twice_replaces_the_old_bridge(self, monkeypatch):
        # 不真连网：_run 里的 connect 会失败并进退避，这里只验注册表语义
        monkeypatch.setattr(R.RemoteBridge, "start", lambda self: None)
        first = R.start_bridge("wss://a", "t1")
        second = R.start_bridge("wss://b", "t2")
        assert first is not second
        assert R.bridge_status()["running"] is True
        R.stop_bridge()
        assert R.bridge_status()["running"] is False

    def test_outbox_stops_pumping_after_a_send_failure(self):
        """连接断了就停下，由外层重连循环重建 —— 不要在死连接上空转。"""
        sink = Sink(fail_after=0)
        box = R._Outbox(sink)
        try:
            box.put({"type": "done"})
            import time

            time.sleep(0.3)
            assert box._alive is False
        finally:
            box.close()
