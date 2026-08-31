"""常驻会话 — agent 栈只初始化一次。

冷启动实测约 6 秒（pandas/supabase/工具注册表），常驻后每轮对话零启动开销。
这是选常驻 IPC 而非每次 spawn CLI 的唯一理由。
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Iterator
from typing import Any

from cli.text_repair import repair_text

logger = logging.getLogger(__name__)

# 前端答复问题的等待上限。
#
# 卡在 180 秒以下是硬约束：桌面端的 bridge 对每个请求有 180s 静默看门狗
# （desktop/src/python-bridge.js 的 IDLE_TIMEOUT_MS），一旦超时它会替这一轮补发
# error+end，前端那一轮就收尾了 —— 之后我们再发答复事件也没人接。所以这里等
# 150 秒，留 30 秒余量给答复往返。
ASK_TIMEOUT_SECONDS = 150.0

# 等待期间的心跳间隔。不发心跳的话，bridge 的静默计时器会在 180s 到点，
# 而人在界面上思考一两分钟是很正常的事。
ASK_HEARTBEAT_SECONDS = 20.0

# 与 TUI 一致：多轮对话保留在内存里，前端不必每次重传历史。
MAX_HISTORY_MESSAGES = 80

# 会话注册表。原来是单例，「新分析」直接清空 _messages —— 旧对话就没了。
_sessions: dict[str, DesktopSession] = {}
_active_session_id = ""
_session_lock = threading.RLock()

# 全局轮次锁：同一时刻只允许一轮对话在跑。
#
# 不是为了省资源，而是 ToolRegistry 只有**一个** _confirm_callback 字段
# （cli/tools.py:845）。多个会话共用一个 registry 时，两轮并发会让后一轮的回调
# 覆盖前一轮，待审批操作被记到错的会话上。工具还跑在线程池里
# （runtime.py:1038），所以按线程路由（threading.local）也不成立。
#
# 代价是不能同时向两个会话提问 —— 实际上也不会。**切换会话不受这把锁影响**，
# 那才是必须保证不卡的路径。
_turn_gate = threading.RLock()


class _PendingQuestion:
    """一个正在等前端作答的问题。

    工具线程在 `wait()` 上阻塞，前端的答复经另一个 IPC worker 走 `answer()` 进来
    （stdio 是 4 个 worker 的线程池，答复不会排在被阻塞的那一轮后面）。
    """

    __slots__ = ("question_id", "event", "_answer", "_lock")

    def __init__(self, question_id: str) -> None:
        self.question_id = question_id
        self.event = threading.Event()
        self._answer = ""
        self._lock = threading.Lock()

    def answer(self, value: str) -> bool:
        """记下答复。重复作答返回 False —— 第一个答复才算。"""
        with self._lock:
            if self.event.is_set():
                return False
            self._answer = value
            self.event.set()
            return True

    def wait(self, emit: Any) -> str | None:
        """等答复，返回 None 表示超时。

        `emit` 每隔 ASK_HEARTBEAT_SECONDS 调一次，用来喂 bridge 的静默看门狗。
        """
        waited = 0.0
        while waited < ASK_TIMEOUT_SECONDS:
            slice_s = min(ASK_HEARTBEAT_SECONDS, ASK_TIMEOUT_SECONDS - waited)
            if self.event.wait(timeout=slice_s):
                with self._lock:
                    return self._answer
            waited += slice_s
            emit()
        return None


class DesktopSession:
    """一个 IPC 进程内的对话状态。审批闸门把写操作交给前端确认。"""

    def __init__(self, session_id: str = "") -> None:
        self._messages: list[dict[str, Any]] = []
        self._tools: Any = None
        self._provider: Any = None
        self._mcp_manager: Any = None
        # 本轮要插进事件流的东西（确认卡、提问卡）。工具跑在线程池里，没法直接
        # yield，所以先进队列，由 _run_turn 的产出循环捞出来发走。
        self._outbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending_question: _PendingQuestion | None = None
        self._question_lock = threading.Lock()
        self._user_id = ""
        self._turn_lock = threading.RLock()
        self.ready_error = ""
        # 会话标识。桌面端原来没有 —— 于是对话既没进 chat_log 也没有 scratchpad，
        # 用户聊了几十轮误关窗口就全没了（TUI 用户不会，因为 TUI 建了这两样）。
        self._session_id = session_id or uuid.uuid4().hex[:12]
        self._scratchpad: Any = None
        self._model_name = ""
        self._provider_name = ""
        # 是不是这个会话自己 start() 建的工具环境。共享环境的会话不能停 MCP。
        self._owns_env = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def adopt_env(self, other: DesktopSession) -> None:
        """复用另一个会话已经建好的工具环境。

        为什么不各建一套:`start()` 要跑 6 秒左右（MCP 启动占大头），新建会话时
        再等一次是不可接受的。而 provider / ToolRegistry / MCP 本来就跟会话无关 ——
        它们属于「这个账号在这台机器上」。

        审批回调是唯一的例外:registry 上只有**一个** _confirm_callback 字段，
        所以每次轮次开始时要重新指向当前会话（见 run_turn 里的 _turn_gate）。
        """
        self._tools = other._tools
        self._provider = other._provider
        self._mcp_manager = other._mcp_manager
        self._user_id = other._user_id
        self._model_name = other._model_name
        self._provider_name = other._provider_name
        self.ready_error = other.ready_error
        self._owns_env = False

    def load_history(self) -> int:
        """从 chat_log 读回这个会话的历史，填进 _messages。返回消息条数。

        复用 TUI 的整套恢复逻辑（`cli/session_context.py`）:它已经处理了
        token 预算（28k）和消息数上限（120），并且在拿不到完整快照时降级成
        纯文本重建 —— 桌面端只存文本，正好走这条路。
        """
        try:
            from cli.session_context import build_resumed_model_context
            from integrations.local_db import load_chat_logs

            # **必须**带 user_id：只按 session_id 查会跨账号。前端切账号后如果
            # sessionId 没清（那是另一个 bug，见 useChat 的 account-changed 处理），
            # 新账号会拿着旧 session_id 过来，这里就把上一个账号的对话装进模型
            # 上下文 —— UI 那侧的账号过滤挡不住它，因为泄漏发生在模型输入里。
            #
            # 已构造数据复现过：不带 user_id 读到 1 条 Alice 的记录，
            # 带 user_id='bob' 读到 0 条。
            #
            # 写入侧（save_chat_log）一直是带 user_id 的，只有这条恢复路径漏了。
            rows = load_chat_logs(session_id=self._session_id, limit=400, user_id=self._user_id)
            if not rows:
                return 0
            resumed = build_resumed_model_context(rows)
            with self._turn_lock:
                self._messages = list(resumed.messages)
            logger.info(
                "resumed session %s: %d rows -> %d messages (%s)",
                self._session_id,
                resumed.source_rows,
                resumed.model_messages,
                resumed.mode,
            )
            return len(self._messages)
        except Exception:
            logger.warning("load history failed for %s", self._session_id, exc_info=True)
            return 0

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
        # 存下来给 chat_log 用。done 事件里没有这两个字段（我第一版从 event 里读，
        # 结果一直写空字符串），provider 状态才是它们的来源。
        self._model_name = str(state.get("model") or "")
        self._provider_name = str(state.get("provider_name") or "")
        if self._provider is None:
            self.ready_error = "no model provider configured"

        self._owns_env = True
        session = _load_session()
        self._user_id = str(session.get("user_id") or "")
        self._tools = ToolRegistry(
            user_id=self._user_id,
            access_token=str(session.get("access_token") or ""),
            refresh_token=str(session.get("refresh_token") or ""),
        )
        if self._provider is not None:
            self._tools.set_provider(self._provider)
        # 确认与提问都在当轮对话里就地问，阻塞等前端作答（见 _ask_inline）。
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
            # 只有建它的会话能停 MCP。多会话共享同一个 manager，每个会话都停一次
            # 会把别人正在用的连接掐掉（shutdown 时会遍历所有会话调 stop）。
            if self._mcp_manager is not None and self._owns_env:
                try:
                    self._mcp_manager.stop()
                except Exception:
                    logger.debug("mcp stop failed", exc_info=True)
            self._mcp_manager = None

    # -- 工具闸门 -------------------------------------------------------------

    def _confirm(self, name: str, args: dict[str, Any]) -> dict[str, str]:
        """就地问用户「这个写操作要不要执行」，等到答复再放行。

        原来是入队 + 返回 queued，让用户去「审批」页批准，由 approve_decide 在
        另一个请求里执行。那条路有两个问题：确认脱离了发起它的对话（用户批完回来
        找不到上下文），而且那一轮往往没有正文，历史落盘被 `if reply` 跳过。

        现在按它本来的性质处理 —— 这就是一次 ask_user_question：卡片发进当轮的
        事件流，工具线程阻塞等答复，同意就在**同一轮**里执行。人不在（超时）按
        未作答处理，绝不代替用户做决定。

        无人值守的定时任务不走这里，走 cli/headless.py 的 DaemonGuard（仍然入队）。
        """
        from cli import approval_queue as aq
        from cli.approval_policy import classify, explain, nav_ratio

        nav = self._nav()
        risk = classify(name, args, nav)
        summary = aq.summarize(name, args)
        reason = explain(name, args, nav)
        ratio = nav_ratio(args, nav)
        answer = self._ask_inline(
            {
                "type": "confirm_request",
                "tool_name": name,
                "summary": summary,
                "risk": risk,
                "args": aq.sanitized_args(args),
                "risk_reason": reason,
                "nav_ratio": ratio,
            }
        )
        decided = "" if answer is None else str(answer)
        self._record_decision(name, args, risk=risk, summary=summary, reason=reason, ratio=ratio, decision=decided)
        if decided == "allow":
            return {"action": "allow"}
        if decided == "deny":
            return {"action": "deny"}
        # 没答复。必须走 timeout 而不是 deny —— 说成「用户拒绝」是伪造一个没发生
        # 过的决定，模型只能照着那个措辞写，用户会读到自己从没做过的选择。
        return {"action": "timeout"}

    def _record_decision(
        self,
        name: str,
        args: dict[str, Any],
        *,
        risk: str,
        summary: str,
        reason: str,
        ratio: float,
        decision: str,
    ) -> None:
        """把这次确认记成一条流水。只是记录，没有状态机 —— 没人靠它推进执行。"""
        try:
            from cli import approval_queue as aq

            aq.log_decision(
                name,
                args,
                risk=risk,
                source="desktop",
                summary=summary,
                user_id=self._user_id,
                risk_reason=reason,
                nav_ratio=ratio,
                decision=decision,
                session_id=self._session_id,
            )
        except Exception:
            logger.debug("approval record failed", exc_info=True)

    def _ask(
        self,
        question: str,
        options: list[str] | None = None,
        allow_free_text: bool = True,
        default_answer: str = "",
    ) -> str:
        """ask_user_question 的桌面实现：问题进事件流，阻塞等前端作答。

        原来这里直接返回超时哨兵（桌面端没有可问的地方），于是模型每次想确认
        什么都只能拿到「问不了」。
        """
        answer = self._ask_inline(
            {
                "type": "question_request",
                "question": str(question or ""),
                "options": list(options or []),
                "allow_free_text": bool(allow_free_text),
                "default_answer": str(default_answer or ""),
            }
        )
        if answer is None:
            from cli.tools import ASK_USER_TIMEOUT_SENTINEL

            return default_answer or ASK_USER_TIMEOUT_SENTINEL
        return answer

    def _ask_inline(self, payload: dict[str, Any]) -> str | None:
        """发一张卡片到当轮事件流，阻塞等前端答复。超时返回 None。

        能这么写是因为 IPC 是 4 worker 的线程池（cli/ipc/stdio.py）：答复作为另一个
        请求进来，落在别的 worker 上，不会排在被阻塞的这一轮后面。
        """
        question_id = uuid.uuid4().hex[:12]
        pending = _PendingQuestion(question_id)
        with self._question_lock:
            # 一轮里同时问两件事不该发生（工具批次会并发，但确认闸门是串行的）。
            # 真撞上时让后来者直接超时，而不是覆盖掉前一个 —— 覆盖会让前一个
            # 永久挂在 wait 上。
            if self._pending_question is not None:
                logger.warning("another question is already pending; refusing to ask")
                return None
            self._pending_question = pending
        try:
            self._outbox.put({**payload, "question_id": question_id})
            return pending.wait(lambda: self._outbox.put({"type": "waiting_for_user", "question_id": question_id}))
        finally:
            with self._question_lock:
                if self._pending_question is pending:
                    self._pending_question = None

    def answer_question(self, question_id: str, answer: str) -> bool:
        """前端送答复进来（由 IPC 的 chat_answer 方法调用）。"""
        with self._question_lock:
            pending = self._pending_question
        if pending is None or pending.question_id != question_id:
            return False
        return pending.answer(answer)

    @property
    def pending_question_id(self) -> str:
        with self._question_lock:
            return self._pending_question.question_id if self._pending_question else ""

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

    def identity_aligned(self) -> bool:
        """工具上下文是否**确实**对齐到了磁盘上的登录态。

        为什么单独一个方法：`sync_identity()` 返回的是「账号有没有变」(changed)，
        而 `False` 同时covers两种完全不同的情况 ——

          1. 账号本来就一致，无需变更（**绝大多数调用都是这个**）
          2. 对话进行中拿不到锁，**跳过了对齐**（此刻用的还是旧账号）

        调用方要判断的是「现在能不能安全地写」，那个语义 `changed` 答不了。
        我上一版让调用方拿 `sync_identity()` 的返回值当对齐信号，结果稳定态下
        (同账号、锁空闲) 也被判成未对齐 —— **所有正常的持仓写入都报
        identity_busy**。这是把两个不同的问题挤进一个 bool 的必然结果。

        所以这里明确回答对齐问题：先尝试同步（拿不到锁就跳过），然后**直接比对**
        会话当前的 _user_id 和磁盘上的 user_id。比对结果不依赖锁抢到没抢到 ——
        锁忙但账号本来就一致时，答案仍然是「已对齐」，那种情况写入是安全的。
        """
        self.sync_identity()
        try:
            disk_user = str((_load_session() or {}).get("user_id") or "")
        except Exception:
            # 读不到磁盘登录态时不阻断写操作：那会让「本地文件暂时读不了」
            # 升级成「功能不可用」。同步本身已经尽力了。
            logger.debug("identity_aligned: cannot read disk session", exc_info=True)
            return True
        return disk_user == self._user_id

    def sync_identity(self) -> bool:
        """
        把工具上下文对齐到磁盘上当前的登录态，换了账号返回 True。

        为什么需要它：ToolRegistry 是 start() 时用当时的 token 建的，之后一直
        不变。而 account 方法每次都读磁盘。两者一旦分叉，portfolio 返回的是
        **上一个账号**的持仓，却被当成当前账号的数据缓存起来 —— 比不隔离更糟，
        因为看起来是隔离好的。

        只在身份变化时重建 ToolRegistry：它带着确认回调和 MCP 管理器，无条件
        重建会把待审批状态和 MCP 连接一起丢掉。

        并发安全：这个方法会清空 _messages，而 run_turn 正在流式产出时也在读写
        它。对话进行中另一个进程改了登录态（登录/登出）时，若在这里直接重建，
        那一轮的回复会 append 进新账号的空历史。

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
            changed = self._sync_identity_locked()
        finally:
            self._turn_lock.release()
        if changed:
            # 其余会话也得跟上：它们各自持着旧账号建的 registry 引用，只同步这一个
            # 的话，切过去就会读到上一个账号的持仓 —— 正是读路径一直在防的那件事。
            _propagate_identity(self)
        return changed

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
        # 换人了，上一个账号的对话历史不能留给新账号。
        self._messages = []
        self._drain_outbox()
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
        # 全局闸门:共享的 ToolRegistry 只有一个 confirm 回调，两轮并发会串号。
        # 见 _turn_gate 的说明。切换会话不走这里，所以不受影响。
        with _turn_gate, self._turn_lock:
            self._bind_confirm()
            yield from self._run_turn(text)

    def _bind_confirm(self) -> None:
        """把共享 registry 的审批回调指到这个会话。

        每轮都要重新绑:registry 是所有会话共用的，上一轮可能是别的会话绑的。
        不绑的话待审批操作会记到错的会话上（界面上表现为审批卡出现在另一个对话里）。
        """
        if self._tools is None:
            return
        try:
            self._tools.set_confirm_callback(self._confirm)
            self._tools.set_ask_user_question_callback(self._ask)
        except Exception:
            logger.debug("rebind confirm callback failed", exc_info=True)

    def _chatlog_save(self, role: str, content: str, **kwargs: Any) -> None:
        """写一条对话记录。静默失败：留存是附加价值，不该让它拖垮回答。"""
        try:
            from integrations.local_db import save_chat_log

            save_chat_log(self._session_id, role, content, user_id=self._user_id, **kwargs)
        except Exception:
            logger.debug("chat log save failed", exc_info=True)

    def _maybe_title(self, user_text: str, reply: str) -> None:
        """第一轮结束后，让模型给这个会话起个短标题。

        为什么不直接用首条提问:提问常常是「我的持仓怎么了」「用一句话说说今天
        大盘」这种口语，同一个人一周能问五遍，列表里全是重样的条目分不清哪个是
        哪个。让模型看过问答之后概括一句，才能带上「聊了什么」的信息。

        **在后台线程做**:多一次模型往返要一两秒，挂在 run_turn 里等于每轮
        对话都慢那么久，而标题只影响侧边栏好不好认。

        只在标题还是自动生成的那一版时才写 —— 用户手改过的名字不能被覆盖
        （upsert 与 rename 的分工见 integrations/local_db.py）。
        """
        if not user_text.strip() or not reply.strip():
            return
        threading.Thread(
            target=self._title_worker,
            args=(self._session_id, self._user_id, user_text, reply),
            daemon=True,
            name="chat-title",
        ).start()

    def _title_worker(self, session_id: str, user_id: str, user_text: str, reply: str) -> None:
        from integrations.local_db import list_chat_sessions, rename_chat_session

        try:
            # 只给第一轮起标题。第二轮之后会话已经有名字了，再改会让用户眼前
            # 的条目突然变名字。
            rows = [s for s in list_chat_sessions(limit=200, user_id=user_id) if s["session_id"] == session_id]
            if not rows or int(rows[0].get("msg_count") or 0) > 2:
                return
            # 记下开工时的标题。模型要十几秒才回，这期间用户完全可能自己改名 ——
            # 不比对就会把他刚起的名字覆盖掉。
            before = str(rows[0].get("title") or "")
            title = self._ask_for_title(user_text, reply)
            if not title:
                return
            after = [s for s in list_chat_sessions(limit=200, user_id=user_id) if s["session_id"] == session_id]
            if after and str(after[0].get("title") or "") != before:
                logger.info("skip auto title for %s: user renamed it meanwhile", session_id)
                return
            rename_chat_session(session_id, title, user_id)
        except Exception:
            # 标题是锦上添花。失败就留着首条提问当标题，不影响任何功能。
            logger.debug("title generation failed", exc_info=True)

    def _ask_for_title(self, user_text: str, reply: str) -> str:
        if self._provider is None:
            return ""
        prompt = (
            "给这段对话起一个标题，用于会话列表。要求："
            "只输出标题本身，不要引号、不要标点结尾；"
            "不超过 16 个字；"
            "带上具体对象（股票名或代码、主题），不要写成「持仓分析」这种谁都适用的空话。"
        )
        # 回复截断:标题只需要知道聊的是什么，把整篇分析发过去纯属浪费 token。
        body = f"用户：{user_text[:200]}\n\n助手：{reply[:400]}"
        result = self._provider.chat([{"role": "user", "content": body}], [], system_prompt=prompt)
        text = ""
        if isinstance(result, dict):
            text = str(result.get("text") or result.get("content") or "")
        else:
            text = str(result or "")
        # 模型有时仍会加引号或句号，清掉。
        return text.strip().strip("\"“”'。.！!").split("\n")[0][:30]

    def _touch_session(self, first_text: str) -> None:
        """保证会话在列表里有一行元数据，并把它顶到最前。

        标题只在为空时写入（见 upsert_chat_session）—— 用户手改过的名字不能被
        后面每一轮的自动标题覆盖。
        """
        try:
            from integrations.local_db import upsert_chat_session

            upsert_chat_session(self._session_id, self._user_id, first_text)
        except Exception:
            logger.debug("touch chat session failed", exc_info=True)

    def _ensure_scratchpad(self, user_text: str) -> Any:
        """本轮的 scratchpad（append-only JSONL）。

        每轮一份，和 TUI 的做法一致。它是逐事件落盘的，所以进程被 kill 之后
        磁盘上仍留有「这一轮做了哪些工具、结果是什么」。
        """
        try:
            from cli.scratchpad import AgentScratchpad

            return AgentScratchpad(user_text, session_id=self._session_id)
        except Exception:
            logger.debug("scratchpad init failed", exc_info=True)
            return None

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

        self._drain_outbox()
        self._messages.append({"role": "user", "content": text})
        self._chatlog_save("user", text)
        self._touch_session(text)
        self._scratchpad = self._ensure_scratchpad(text)
        runtime = AgentRuntime(self._provider, self._tools, scratchpad=self._scratchpad)

        # 每轮重读配置：用户可能刚在设置里改了语气，不该等到重启才生效。
        config = load_config()
        prompt = CHAT_AGENT_SYSTEM_PROMPT + tone_suffix(
            str(config.get("desktop_tone") or "default"),
            str(config.get("desktop_tone_custom") or ""),
        )

        try:
            for event in self._stream_with_outbox(runtime, prompt):
                if not isinstance(event, dict):
                    yield _project(event)
                    continue
                # 确认卡 / 提问卡是我们自己塞进来的，不过 _project 的白名单。
                if event.get("__inline__"):
                    yield {k: v for k, v in event.items() if k != "__inline__"}
                    continue
                yield _project(event)
                # 工具成功之后才发产物事件 —— 据 tool_start 开面板会在失败时
                # 留下一个空面板。
                if event.get("type") in ("tool_result", "tool_error"):
                    artifact = _chat_artifact(event)
                    if artifact is not None:
                        yield artifact
                if event.get("type") == "done":
                    self._persist_reply(text, event)
        except Exception as exc:
            logger.exception("chat turn failed")
            yield {"type": "error", "code": "turn_failed", "message": str(exc)}

    def _persist_reply(self, text: str, event: dict[str, Any]) -> None:
        """把这一轮的回复落进内存历史和 chat_log。

        **空正文也要落盘**。原来整段挂在 `if reply:` 下面：而以确认收尾的那一轮
        模型常常一个字都不写（它在等工具结果），于是 user 行进了库、assistant 行
        没有，标题也没生成 —— 侧边栏里那个会话看着像空的，用户「找不到会话了」。
        """
        reply = str(event.get("text") or "")
        if reply:
            self._messages.append({"role": "assistant", "content": reply})
        usage = event.get("usage") or {}
        self._chatlog_save(
            "assistant",
            reply,
            model=str(getattr(self._provider, "model", "") or self._model_name),
            provider=str(getattr(self._provider, "name", "") or self._provider_name),
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            elapsed_s=float(event.get("elapsed") or 0.0),
        )
        # 后台起标题。放在 chat_log 落盘之后 —— worker 要靠 msg_count 判断这是
        # 不是第一轮。空回复起不出标题，_maybe_title 自己会跳过。
        self._maybe_title(text, reply)
        self._trim()

    def _stream_with_outbox(self, runtime: Any, prompt: str) -> Iterator[Any]:
        """跑 runtime，同时把 outbox 里的卡片插进事件流。

        为什么要把 runtime 挪到另一个线程：确认闸门（`_confirm`）是在工具线程里
        **阻塞**等答复的，而那个线程正是驱动这个生成器的线程。不挪的话，卡片进了
        outbox 也发不出去 —— 生成器停在阻塞点上，前端永远收不到要它作答的那张卡，
        于是双方互等，150 秒后一起超时。
        """
        pump: queue.Queue[Any] = queue.Queue()
        done = object()

        def _produce() -> None:
            try:
                for event in runtime.run_stream(list(self._messages), prompt):
                    pump.put(event)
            except BaseException as exc:  # noqa: BLE001 - 交给消费侧重抛
                pump.put(exc)
            finally:
                pump.put(done)

        worker = threading.Thread(target=_produce, name="ipc-turn", daemon=True)
        worker.start()
        while True:
            yield from self._flush_outbox()
            try:
                item = pump.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is done:
                break
            if isinstance(item, BaseException):
                yield from self._flush_outbox()
                raise item
            yield item
        yield from self._flush_outbox()

    def _flush_outbox(self) -> Iterator[dict[str, Any]]:
        while True:
            try:
                yield {**self._outbox.get_nowait(), "__inline__": True}
            except queue.Empty:
                return

    def _drain_outbox(self) -> None:
        """丢掉上一轮没发完的卡片（比如那一轮出错提前退出了）。"""
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    def _trim(self) -> None:
        if len(self._messages) > MAX_HISTORY_MESSAGES:
            self._messages = self._messages[-MAX_HISTORY_MESSAGES:]

    def clear_context(self) -> None:
        """清掉这个会话的上下文（换账号时用）。"""
        with self._turn_lock:
            self._messages.clear()
            self._drain_outbox()

    def reset(self) -> None:
        with self._turn_lock:
            self._messages.clear()
            self._drain_outbox()


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

    进来的字符串先修一遍落单的代理字符。cli/runtime.py 已经在流式入口修过模型
    正文，但走到这儿的不止那一条路 —— 工具结果、args 里的文件内容都可能带上
    不可编码的字符。这里是 IPC 的最后一道关口，`len(...encode("utf-8"))` 一抛
    UnicodeEncodeError 整轮回答就没了，所以在这层也兜一次。
    """
    if isinstance(value, str):
        value = repair_text(value)
        if len(value.encode("utf-8")) > MAX_IPC_FIELD_BYTES:
            clipped = value.encode("utf-8")[:MAX_IPC_FIELD_BYTES].decode("utf-8", "ignore")
            return clipped + "\n\n…（内容过大，已在 IPC 层截断）"
        return value
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


def get_session(session_id: str = "") -> DesktopSession:
    """取一个会话。不传 id 就取当前活跃的（没有就建）。

    第一个会话负责 start()（约 6 秒，MCP 启动占大头）；之后的会话
    adopt_env() 复用同一套工具环境，所以新建会话是即时的。
    """
    global _active_session_id
    with _session_lock:
        if session_id and session_id in _sessions:
            _active_session_id = session_id
            return _sessions[session_id]

        if not _sessions:
            first = DesktopSession(session_id)
            first.start()
            _sessions[first.session_id] = first
            _active_session_id = first.session_id
            return first

        if session_id:
            # 请求一个还没在内存里的会话（比如从列表里点开一个历史会话）。
            # 建壳子、复用工具环境、把历史读回来。
            base = _sessions[_active_session_id or next(iter(_sessions))]
            session = DesktopSession(session_id)
            session.adopt_env(base)
            session.load_history()
            _sessions[session_id] = session
            _active_session_id = session_id
            return session

        if _active_session_id not in _sessions:
            _active_session_id = next(iter(_sessions))
        return _sessions[_active_session_id]


def find_waiting_session(question_id: str) -> DesktopSession | None:
    """找正在等这个 question_id 的会话。

    不走 get_session：那个会在会话不存在时**建一个**（还会跑 start()）。送答复
    只该找已经在内存里等着的那一轮，找不到就是超时或重复点击。
    """
    if not question_id:
        return None
    with _session_lock:
        candidates = list(_sessions.values())
    for session in candidates:
        if session.pending_question_id == question_id:
            return session
    return None


def new_session() -> DesktopSession:
    """开一个新会话并切过去。旧会话留在内存和 chat_log 里。

    这是「新分析」应有的语义。原来它调 reset() 直接清空 _messages ——
    旧对话既不在界面上也回不来。
    """
    global _active_session_id
    with _session_lock:
        base = get_session()  # 确保工具环境已经建好
        session = DesktopSession()
        session.adopt_env(base)
        _sessions[session.session_id] = session
        _active_session_id = session.session_id
        _prune_sessions()
        return session


def active_session_id() -> str:
    with _session_lock:
        return _active_session_id


def drop_session(session_id: str) -> None:
    """把会话从内存里摘掉（删除会话时用）。不动数据库。"""
    global _active_session_id
    with _session_lock:
        _sessions.pop(session_id, None)
        if _active_session_id == session_id:
            _active_session_id = next(iter(_sessions), "")


# 内存里最多留几个会话。超出的按插入顺序淘汰最旧的 —— 它们的历史都在
# chat_log 里，再点开时会重新 load_history()，代价只是一次查库。
MAX_LIVE_SESSIONS = 12


def _prune_sessions() -> None:
    while len(_sessions) > MAX_LIVE_SESSIONS:
        for sid in list(_sessions):
            if sid != _active_session_id:
                _sessions.pop(sid, None)
                break
        else:
            return


def _propagate_identity(base: DesktopSession) -> None:
    """把刚重建的工具环境推给其余会话，并清掉它们的上下文。

    由 base.sync_identity() 在检测到换账号后调用 —— 调用方（_synced_session）
    不需要知道有几个会话存在。

    为什么要清:上一个账号的对话历史不能留给新账号。只清当前会话的话，
    切过去就能看到别人的对话。
    """
    with _session_lock:
        for sid, session in _sessions.items():
            if sid == base.session_id:
                continue
            session.adopt_env(base)
            session.clear_context()


def sync_all_identities() -> bool:
    """对齐身份。入口保留给不持有会话引用的调用方。"""
    with _session_lock:
        if not _sessions:
            return False
        base = _sessions[_active_session_id or next(iter(_sessions))]
    return base.sync_identity()


def shutdown_session() -> None:
    global _active_session_id
    with _session_lock:
        for session in list(_sessions.values()):
            session.stop()
        _sessions.clear()
        _active_session_id = ""
