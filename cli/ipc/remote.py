"""远程传输 —— 手机通过云端信箱调用同一批 IPC 方法。

## 与 stdio 传输的关系

`cli/ipc/methods.py` 是传输无关的（它的第一行注释就说了「不知道传输是 stdio 还是
HTTP」）。所以这里不碰任何方法实现：63 个方法原样复用，只换了消息进出的管道。

`stdio.py` 是模板，但有三处必须不同：

1. **不需要 fd 守卫。** stdio 要把协议通道 dup 到私有 fd，因为原生扩展会往 fd 1
   乱写。这里协议不走 fd。
2. **背压要自己做。** 管道满了 `write` 会阻塞，这是 stdio 的隐式流控。WebSocket
   没有 —— 弱网下手机会被事件淹掉。见 `_Outbox`。
3. **要重连。** 本地管道断了等于进程没了；这条 WS 会因为网络抖动、DO 休眠、
   笔记本合盖而断，断了要自己爬回来。

## 消息形状

上行（手机→电脑）与 stdio 一致：`{id, method, params}`，外加信箱塞的 `from`
（哪台设备发的）。下行加 `to` 让信箱定向回那台设备，不广播给其他手机。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from cli.text_repair import repair_text

logger = logging.getLogger(__name__)

MAX_WORKERS = 4

# 单条消息上限。方法层已经把大字段裁到 768KB（session.py 的 MAX_IPC_FIELD_BYTES），
# 这里再挡一道是因为远程还要过公网 —— 超大帧会让整条连接卡住而不只是慢。
MAX_FRAME_BYTES = 512 * 1024

# 待发队列上限。**这是 stdio 没有的东西**：本地管道满了 write 会阻塞，天然限流；
# WebSocket 的 send 不阻塞，弱网下事件会在内存里堆成山。
#
# 超限时丢弃**中间的 text_delta**而不是最新事件：正文可以缺一段（手机上本来也是
# 快速滚过的流式文字），但 done / approval_pending / error 这些状态事件丢了会让
# 界面永久卡在「正在思考」。
MAX_QUEUED = 400

# 丢弃时优先牺牲这些类型。其余一律保留。
_DROPPABLE = frozenset({"text_delta", "stage_start", "stage_done", "usage"})

# 远程完全不发的事件类型。
#
# `thinking_delta` 在方法层的白名单里是**空 tuple** —— 内容已经被剥掉（推理不跨
# IPC 边界），但空壳事件仍然逐个过河。实测手机端一轮对话收到 88 个纯空的
# thinking_delta:本地管道无所谓，公网上就是 88 个无用帧，弱网下还会挤掉真正
# 有用的事件。前端拿它只做「还在动」的指示，而 stage_start 已经能表达这件事。
_SUPPRESSED = frozenset({"thinking_delta"})

# 重连退避。上限 30 秒 —— 再长的话用户拿起手机时要等太久才恢复。
_BACKOFF = (1, 2, 5, 10, 20, 30)


class _Outbox:
    """带背压的发送队列。

    单独一个线程发送，业务线程只入队 —— 否则一个慢连接会把 agent 的执行线程
    也拖住（那才是真正要保护的东西）。
    """

    def __init__(self, send: Any) -> None:
        self._send = send
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._alive = True
        self._dropped = 0
        self._thread = threading.Thread(target=self._pump, daemon=True, name="remote-outbox")
        self._thread.start()

    def put(self, payload: dict[str, Any]) -> None:
        # 先修落单的代理字符（网关把一个字符拆到两个 chunk 里留下的）：紧接着的
        # `encode("utf-8")` 是 strict 的，抛出去就是整轮回答变一行编码错误。
        raw = repair_text(json.dumps(payload, ensure_ascii=False, default=str))
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raw = json.dumps(
                {
                    "id": payload.get("id"),
                    "to": payload.get("to"),
                    "type": "error",
                    "code": "frame_too_large",
                    "message": "这条结果太大，无法通过远程通道传输。请在电脑上查看。",
                },
                ensure_ascii=False,
            )
        with self._lock:
            if len(self._queue) >= MAX_QUEUED:
                self._shed_locked()
            self._queue.append(raw)
        self._wake.set()

    def _shed_locked(self) -> None:
        """队列满了：从**队首**开始丢可丢的类型。

        丢队首而不是队尾:队首是最旧的正文片段，用户已经滚过去了；队尾是刚发生的
        事，最需要送到。
        """
        for index, raw in enumerate(self._queue):
            try:
                kind = str(json.loads(raw).get("type") or "")
            except Exception:
                continue
            if kind in _DROPPABLE:
                del self._queue[index]
                self._dropped += 1
                return
        # 一条可丢的都没有（全是状态事件）：丢最旧的，保住最新的。
        self._queue.popleft()
        self._dropped += 1

    def _pump(self) -> None:
        while self._alive:
            with self._lock:
                raw = self._queue.popleft() if self._queue else None
            if raw is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                self._send(raw)
            except Exception:
                # 连接断了。外层的重连循环会重建 outbox，这里只need停下。
                logger.debug("remote send failed", exc_info=True)
                self._alive = False
                return

    def close(self) -> None:
        self._alive = False
        self._wake.set()
        if self._dropped:
            logger.info("remote outbox dropped %d events under backpressure", self._dropped)


# 远程不可用的方法。
#
# 不是权限问题 —— 用户明确要求手机和电脑权限一致。这些是**物理上**在手机上没有
# 意义的:操作本机 launchd、读电脑上的文件路径、或者调的是桌面 UI 自己的偏好。
#
# 明确拒绝并给出人话，而不是让它们静默失败或返回一串手机上打不开的绝对路径。
REMOTE_UNAVAILABLE = {
    "daemon_status": "定时任务的安装状态要在电脑上看（它操作的是那台机器的 launchd）。",
    "daemon_install": "安装定时任务需要在电脑上操作。",
    "daemon_uninstall": "卸载定时任务需要在电脑上操作。",
    "artifact_import": "导入文件要用电脑上的文件选择器。",
    "export_pdf": "导出 PDF 要用电脑上的保存对话框。",
}


class RemoteBridge:
    """维持一条到云端信箱的连接，把收到的请求交给方法层。"""

    def __init__(self, url: str, token: str, label: str = "") -> None:
        self._url = url
        self._token = token
        self._label = label or "电脑"
        self._stop = threading.Event()
        self._outbox: _Outbox | None = None
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="remote-ipc")
        self._thread: threading.Thread | None = None
        self.connected = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="remote-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._outbox is not None:
            self._outbox.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._session()
                attempt = 0  # 正常断开（比如被踢），下次立刻重连
            except Exception:
                logger.info("remote bridge disconnected", exc_info=True)
            if self._stop.is_set():
                return
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            self._stop.wait(timeout=delay)

    def _session(self) -> None:
        from websockets.sync.client import connect

        # token 走 subprotocol 而不是 header —— 与 web 端一致（浏览器 WS 不能带
        # Authorization header，云端那侧就是按这个格式解析的）。
        url = f"{self._url}?role=host&label={self._label}"
        with connect(url, subprotocols=["bearer", self._token], open_timeout=20) as ws:
            self.connected = True
            self._outbox = _Outbox(ws.send)
            logger.info("remote bridge connected")
            try:
                for raw in ws:
                    if self._stop.is_set():
                        return
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    self._pool.submit(self._handle, str(raw))
            finally:
                self.connected = False
                if self._outbox is not None:
                    self._outbox.close()
                    self._outbox = None

    def _handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return

        kind = str(message.get("type") or "")
        if kind in ("presence", "host_offline"):
            return  # 信箱的状态广播，不是请求

        request_id = message.get("id")
        origin = str(message.get("from") or "")
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if method in REMOTE_UNAVAILABLE:
            self._respond(
                request_id,
                origin,
                {
                    "type": "error",
                    "code": "desktop_only",
                    "message": REMOTE_UNAVAILABLE[method],
                },
            )
            self._respond(request_id, origin, {"type": "end"})
            return

        from cli.ipc.methods import MethodError, dispatch

        try:
            for event in dispatch(method, params):
                self._respond(request_id, origin, event)
            self._respond(request_id, origin, {"type": "end"})
        except MethodError as exc:
            self._respond(request_id, origin, {"type": "error", "code": exc.code, "message": exc.message})
            self._respond(request_id, origin, {"type": "end"})
        except Exception as exc:
            logger.exception("remote method %s failed", method)
            self._respond(request_id, origin, {"type": "error", "code": "internal", "message": str(exc)})
            self._respond(request_id, origin, {"type": "end"})

    def _respond(self, request_id: Any, origin: str, event: dict[str, Any]) -> None:
        outbox = self._outbox
        if outbox is None:
            return
        if str(event.get("type") or "") in _SUPPRESSED:
            return
        # id 覆盖的理由和 stdio 一样（见 stdio.py:73）：传输层 id 必须是请求流 id，
        # 否则对面无法把事件分发给发起调用的那一轮。
        # `to` 是远程独有的:信箱按它定向回发起的那台设备，不广播给其他手机。
        payload = {**event, "id": request_id}
        if origin:
            payload["to"] = origin
        outbox.put(payload)


_bridge: RemoteBridge | None = None
_bridge_lock = threading.Lock()


def start_bridge(url: str, token: str, label: str = "") -> RemoteBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            _bridge.stop()
        _bridge = RemoteBridge(url, token, label)
        _bridge.start()
        return _bridge


def stop_bridge() -> None:
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            _bridge.stop()
            _bridge = None


def bridge_status() -> dict[str, Any]:
    with _bridge_lock:
        return {"running": _bridge is not None, "connected": bool(_bridge and _bridge.connected)}
