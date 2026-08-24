"""常驻会话 — agent 栈只初始化一次。

冷启动实测约 6 秒（pandas/supabase/工具注册表），常驻后每轮对话零启动开销。
这是选常驻 IPC 而非每次 spawn CLI 的唯一理由。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

# 与 TUI 一致：多轮对话保留在内存里，前端不必每次重传历史。
MAX_HISTORY_MESSAGES = 80

_session: DesktopSession | None = None
_session_lock = threading.RLock()


class DesktopSession:
    """一个 IPC 进程内的对话状态。审批闸门把写操作交给前端确认。"""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._tools: Any = None
        self._provider: Any = None
        self._mcp_manager: Any = None
        self._pending_confirms: list[dict[str, Any]] = []
        self._user_id = ""
        self._turn_lock = threading.RLock()
        self.ready_error = ""

    # -- 初始化 ---------------------------------------------------------------

    def start(self) -> None:
        from cli.bootstrap import build_provider_state, init_local_services, load_config_env
        from cli.tools import ToolRegistry

        init_local_services()
        try:
            load_config_env()
        except Exception:
            logger.debug("config env load failed", exc_info=True)

        state = build_provider_state()
        self._provider = state.get("provider")
        if self._provider is None:
            self.ready_error = "no model provider configured"

        session = _load_session()
        self._user_id = str(session.get("user_id") or "")
        self._tools = ToolRegistry(
            user_id=self._user_id,
            access_token=str(session.get("access_token") or ""),
            refresh_token=str(session.get("refresh_token") or ""),
        )
        if self._provider is not None:
            self._tools.set_provider(self._provider)
        # 桌面端有 UI，但确认要走前端；这里先入队，由 approve_decide 执行。
        self._tools.set_confirm_callback(self._confirm)
        self._tools.set_ask_user_question_callback(self._ask)
        self._start_mcp()

    def _start_mcp(self) -> None:
        try:
            from cli.mcp_client import McpClientManager
            from cli.mcp_config import enabled_servers

            servers = enabled_servers()
            if not servers:
                return
            manager = McpClientManager(servers)
            manager.start()
            if manager.tools():
                self._mcp_manager = manager
                self._tools.set_mcp_manager(manager)
            else:
                manager.stop()
        except Exception:
            logger.warning("external mcp start failed", exc_info=True)

    def stop(self) -> None:
        with self._turn_lock:
            if self._mcp_manager is not None:
                try:
                    self._mcp_manager.stop()
                except Exception:
                    logger.debug("mcp stop failed", exc_info=True)
                self._mcp_manager = None

    # -- 工具闸门 -------------------------------------------------------------

    def _confirm(self, name: str, args: dict[str, Any]) -> dict[str, str]:
        """写操作入待批准队列，由前端的审批卡决定，不阻塞当前轮。"""
        from cli import approval_queue as aq
        from cli.approval_policy import classify, explain, nav_ratio

        nav = self._nav()
        risk = classify(name, args, nav)
        summary = aq.summarize(name, args)
        reason = explain(name, args, nav)
        ratio = nav_ratio(args, nav)
        approval_id = aq.enqueue(
            name,
            args,
            risk=risk,
            source="desktop",
            summary=summary,
            user_id=self._user_id,
            risk_reason=reason,
            nav_ratio=ratio,
        )
        self._pending_confirms.append(
            {
                "approval_id": approval_id,
                "tool_name": name,
                "summary": summary,
                "risk": risk,
                "args": args,
                "risk_reason": reason,
                "nav_ratio": ratio,
            }
        )
        return {
            "action": "queued",
            "message": (
                f"操作 [{name}] 已提交审批（编号 {approval_id}），尚未执行。"
                "这不是拒绝，也不是失败——等待用户在界面上批准。"
                "不要重试，不要声称已完成，直接说明已提交审批。"
            ),
        }

    def _ask(self, *_args: Any, **_kwargs: Any) -> str:
        from cli.tools import ASK_USER_TIMEOUT_SENTINEL

        return ASK_USER_TIMEOUT_SENTINEL

    def _nav(self) -> float:
        try:
            from cli.headless import current_nav

            return current_nav(self._tools)
        except Exception:
            return 0.0

    @property
    def user_id(self) -> str:
        """这个会话的工具实际在用哪个账号 —— 不是磁盘上当前的登录态。"""
        return self._user_id

    def sync_identity(self) -> bool:
        """
        把工具上下文对齐到磁盘上当前的登录态，换了账号返回 True。

        为什么需要它：ToolRegistry 是 start() 时用当时的 token 建的，之后一直
        不变。而 account 方法每次都读磁盘。两者一旦分叉，portfolio 返回的是
        **上一个账号**的持仓，却被当成当前账号的数据缓存起来 —— 比不隔离更糟，
        因为看起来是隔离好的。

        只在身份变化时重建 ToolRegistry：它带着确认回调和 MCP 管理器，无条件
        重建会把待审批状态和 MCP 连接一起丢掉。

        并发安全：这个方法会清空 _messages 和 _pending_confirms，而 run_turn
        正在流式产出时也在读写它们。对话进行中另一个进程改了登录态（登录/登出）
        时，若在这里直接重建，那一轮的回复会 append 进新账号的空历史，且该轮
        入队的审批不会发出 approval_pending 事件（审批仍在队列里，只是界面不弹）。

        但**不能无条件等锁**：run_turn 持锁贯穿整个流式输出（可能几分钟），
        一次读持仓就会挂到对话结束，期间不发任何事件 —— 直接撞上桥的静默超时，
        表现为「读持仓卡死」。那是把罕见竞态换成了常见卡顿。

        所以用非阻塞获取：拿不到锁说明正有一轮在跑，此时**跳过重建**并返回
        False。代价是那一轮继续用旧账号的 registry 跑完（它本来就是用那个身份
        起的，自身一致），下一次调用自然会对齐。
        """
        if not self._turn_lock.acquire(blocking=False):
            logger.info("skip identity sync: a turn is in flight")
            return False
        try:
            return self._sync_identity_locked()
        finally:
            self._turn_lock.release()

    def _sync_identity_locked(self) -> bool:
        from cli.tools import ToolRegistry

        session = _load_session()
        next_user = str(session.get("user_id") or "")
        if next_user == self._user_id:
            return False

        logger.info("desktop session identity changed; rebuilding tool registry")
        self._user_id = next_user
        tools = ToolRegistry(
            user_id=next_user,
            access_token=str(session.get("access_token") or ""),
            refresh_token=str(session.get("refresh_token") or ""),
        )
        if self._provider is not None:
            tools.set_provider(self._provider)
        tools.set_confirm_callback(self._confirm)
        tools.set_ask_user_question_callback(self._ask)
        if self._mcp_manager is not None:
            tools.set_mcp_manager(self._mcp_manager)
        self._tools = tools
        # 换人了，上一个账号的对话历史和待审批不能留给新账号。
        self._messages = []
        self._pending_confirms = []
        return True

    @property
    def tool_context(self) -> Any:
        """
        工具执行上下文（含登录 token）。

        直接调用工具函数的 IPC 方法必须把它传下去：没有它，has_cloud() 恒为
        False，已登录用户的 Supabase 数据永远读不到，界面静默显示本地缓存。
        """
        if self._tools is None:
            return None
        return self._tools.tool_context

    # -- 对话 -----------------------------------------------------------------

    def run_turn(self, text: str) -> Iterator[dict[str, Any]]:
        with self._turn_lock:
            yield from self._run_turn(text)

    def _run_turn(self, text: str) -> Iterator[dict[str, Any]]:
        if self._tools is None:
            self.start()
        if self._provider is None:
            yield {"type": "error", "code": "no_provider", "message": self.ready_error}
            return

        from cli.ipc.tone import tone_suffix
        from cli.runtime import AgentRuntime
        from core.prompts import CHAT_AGENT_SYSTEM_PROMPT
        from integrations.local_auth import load_config

        self._pending_confirms.clear()
        self._messages.append({"role": "user", "content": text})
        runtime = AgentRuntime(self._provider, self._tools)

        # 每轮重读配置：用户可能刚在设置里改了语气，不该等到重启才生效。
        config = load_config()
        prompt = CHAT_AGENT_SYSTEM_PROMPT + tone_suffix(
            str(config.get("desktop_tone") or "default"),
            str(config.get("desktop_tone_custom") or ""),
        )

        try:
            for event in runtime.run_stream(list(self._messages), prompt):
                yield _project(event)
                # 工具成功之后才发产物事件 —— 据 tool_start 开面板会在失败时
                # 留下一个空面板。
                if isinstance(event, dict) and event.get("type") in ("tool_result", "tool_error"):
                    artifact = _chat_artifact(event)
                    if artifact is not None:
                        yield artifact
                if isinstance(event, dict) and event.get("type") == "done":
                    reply = str(event.get("text") or "")
                    if reply:
                        self._messages.append({"role": "assistant", "content": reply})
                    self._trim()
        except Exception as exc:
            logger.exception("chat turn failed")
            yield {"type": "error", "code": "turn_failed", "message": str(exc)}

        for pending in self._pending_confirms:
            yield {"type": "approval_pending", **pending}

    def _trim(self) -> None:
        if len(self._messages) > MAX_HISTORY_MESSAGES:
            self._messages = self._messages[-MAX_HISTORY_MESSAGES:]

    def reset(self) -> None:
        with self._turn_lock:
            self._messages.clear()
            self._pending_confirms.clear()


# 只透传前端要渲染的字段，避免把内部结构和凭据带出去。
_PASSTHROUGH = {
    "text_delta": ("text",),
    # 模型的内部推理不跨 IPC 边界；前端只展示稳定的正文与工具状态。
    "thinking_delta": (),
    "tool_start": ("name", "display_name", "args"),
    # 只放 name，**不放 result**：界面只需要知道「这一步做完了」好把转圈换成对勾。
    # 结果正文动辄几十 KB（持仓明细、K 线数据），过河一趟既无用又占带宽，
    # 而且真正要展示的东西已经走产物事件了。
    "tool_result": ("name", "display_name"),
    "tool_calls": ("names",),
    "tool_error": ("name", "error"),
    # 阶段进度。模型生成工具调用要十几到二十几秒，那段时间原来界面上只有一句
    # 静止的「正在思考…」—— 看着就像卡死。stage_start 自带 message 和 round，
    # 后端一直在发，只是从没过河。
    "stage_start": ("stage", "round", "message"),
    "stage_done": ("stage", "round", "success"),
    "model_start": ("model",),
    "usage": ("input_tokens", "output_tokens"),
    "retry": ("reason",),
    "done": ("text", "rounds", "elapsed"),
    "turn_failed": ("error",),
    "turn_cancelled": (),
}


# 单个字段经 IPC 的上限。
#
# 工具自己会拒绝超限的正文（render_dashboard 512 KiB、save_report 256 KiB），
# 但那只挡住了落盘 —— `tool_start` 的 args 里带着模型刚写的**完整** html/markdown，
# 失败的 chat_artifact 也带 payload。也就是说超限内容仍会原样经过 IPC 通道
# （实测 524289 和 262145 字节都过去了）。
#
# 上限设在这里，比工具的阈值略宽：这一层的目的不是替工具做业务校验，而是保证
# 一个失控的字段（模型内联了整个图表库）不会把 stdio 通道堵住。
MAX_IPC_FIELD_BYTES = 768 * 1024

# 可能很大的字段。只裁这些，不做通用遍历 —— 通用遍历要么漏掉嵌套结构，
# 要么把每个事件都变成一次深拷贝。
_LARGE_FIELDS = ("args", "text", "result")


def _cap(value: Any) -> Any:
    """把过大的字符串截断，并留一个明确的标记。

    截断而不是丢弃：前端拿到「前 768 KiB + 已截断」仍能显示个大概，而整个字段
    消失会让人以为工具什么都没返回。
    """
    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_IPC_FIELD_BYTES:
        clipped = value.encode("utf-8")[:MAX_IPC_FIELD_BYTES].decode("utf-8", "ignore")
        return clipped + "\n\n…（内容过大，已在 IPC 层截断）"
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    return value


def _project(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {"type": "unknown"}
    kind = str(event.get("type") or "unknown")
    fields = _PASSTHROUGH.get(kind)
    if fields is None:
        return {"type": kind}
    out: dict[str, Any] = {"type": kind}
    for key in fields:
        value = event.get(key)
        if value is None:
            continue
        out[key] = _cap(value) if key in _LARGE_FIELDS else value
    return out


# 哪些工具会产出「右侧面板里能打开的东西」，以及它算哪一类产物。
#
# 只有这张表里的工具才会生成产物事件 —— 白名单而非黑名单：新增工具默认不产出
# 产物，需要时显式登记。反过来（默认产出、遇到不想要的再排除）会让每个新工具
# 都可能意外弹开面板。
_ARTIFACT_TOOLS = {
    "annotate_chart": "kline",
    "render_dashboard": "dashboard",
    "save_report": "report",
}


def _chat_artifact(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    把 tool_result / tool_error 翻译成产物事件；不是产物则返回 None。

    **只给 call_id，不拼轮次前缀。** 这一层拿不到传输层的请求 id（那在 stdio
    层注入），所以曾经自己编了个 `turn-N` 序号 —— 而前端的 `turn.id` 是 IPC
    请求流 id（数字，如 17）。两个命名空间，`startsWith('17:')` 恒为假，于是
    对话里的产物卡片**一张都不显示**，报告去重也永远不生效。而两侧各自的
    单测都是绿的：它们各自造 id 各自验，从没让真实的两端拼一次。

    为什么翻译在这一层，而不是 runtime：runtime 是 CLI / TUI / 桌面共用的，
    「右侧面板」是桌面独有的概念。而且工具结果里可能带凭据和内部结构，
    这里是既有的安全边界 —— 按 kind 白名单挑字段，绝不整个透传 result。

    为什么用 tool_result 而不是 tool_start：tool_start 时工具还没成功，
    据它开面板会在失败时留下一个空面板（这正是旧实现的毛病）。
    """
    name = str(event.get("name") or "")
    kind = _ARTIFACT_TOOLS.get(name)
    if kind is None:
        return None

    args = event.get("args")
    args = args if isinstance(args, dict) else {}
    result = event.get("result")
    result = result if isinstance(result, dict) else {}
    failed = str(event.get("status") or "") == "error" or bool(result.get("error"))

    # 只带 call_id：轮次前缀由前端用它已知的 stream id 拼（见上面 docstring）。
    # 缺失时退回工具名 —— 同一轮多次调用会互相覆盖，但比没有标识好。
    call_id = str(event.get("tool_call_id") or name)

    if kind == "kline":
        symbol = str(args.get("code") or "").strip()
        if not symbol:
            return None
        # list / clear 不是「画了一张图」，不该弹开面板。
        # 这条是旧实现漏掉的：action=list 也会开图。
        action = str(args.get("action") or "draw").strip().lower()
        if action != "draw":
            return None
        return {
            "type": "chat_artifact",
            # 刻意不叫 "id"：传输层会把 event["id"] 覆盖成请求流 id
            # （stdio.py 的既有约定，审批事件同理改用 approval_id）。
            # 前端读 event.id 拿轮次、读这个字段拿调用，两者拼成产物 id。
            "artifact_call_id": call_id,
            "kind": "kline",
            "title": symbol,
            "status": "failed" if failed else "ready",
            # payload 只带重新打开这张图所必需的字段，不带标注内容本体 ——
            # 图自己会去后端取，事件里重复塞一份只会让每次工具调用都拖着几 KB。
            "payload": {"symbol": symbol, "timeframe": str(args.get("timeframe") or "1d")},
        }

    if kind == "dashboard":
        # HTML 从 args 取而不是 result：工具返回值刻意不含 html（那会把几百 KB
        # 回灌进模型上下文）。args 里是模型刚写的那一份，正是要渲染的东西。
        html = str(args.get("html") or "")
        title = str(args.get("title") or "").strip()
        if not html.strip() or not title:
            return None
        return {
            "type": "chat_artifact",
            "artifact_call_id": call_id,
            "kind": "dashboard",
            "title": title,
            "status": "failed" if failed else "ready",
            # 面板不能联网，数据必须在生成时嵌进 HTML，所以没有「让它自己去取」
            # 的选项 —— payload 只能带内容本体。
            "payload": {"html": _cap(html)},
        }

    if kind == "report":
        title = str(args.get("title") or "").strip()
        body = str(args.get("markdown") or "")
        rel = str(result.get("path") or "")
        if not title or not body.strip():
            return None
        return {
            "type": "chat_artifact",
            "artifact_call_id": call_id,
            "kind": "report",
            "title": title,
            "status": "failed" if failed else "ready",
            # body 从 args 取：工具返回值刻意不含正文（那会把报告回灌进模型
            # 上下文）。path 从 result 取 —— 它是落盘之后才知道的，带上它前端
            # 才能在关掉页签后从报告库找回同一份。
            "payload": {"body": _cap(body), "path": rel},
        }
    return None


def _load_session() -> dict[str, Any]:
    try:
        from cli.auth import load_session

        return load_session() or {}
    except Exception:
        logger.debug("load session failed", exc_info=True)
        return {}


def get_session() -> DesktopSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = DesktopSession()
            _session.start()
        return _session


def shutdown_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None
