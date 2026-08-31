"""IPC 方法层 — 不知道传输是 stdio 还是 HTTP。

这一层是换传输时保持不变的部分：每个方法是一个生成器，yield 结构化事件。
传输层负责把事件序列化并送出去。
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Event = dict[str, Any]

# 单次请求最多回传的事件数，防止异常循环把前端刷爆。
MAX_EVENTS_PER_CALL = 100_000


class MethodError(Exception):
    """方法执行失败，带机器可读的 code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ok(**payload: Any) -> Event:
    return {"type": "result", **payload}


# -- 查询类：一次返回 ---------------------------------------------------------


def approve_list(_params: dict[str, Any]) -> Iterator[Event]:
    from cli import approval_queue as aq

    current_user = _current_user_id()

    items = [
        {
            "id": item.id,
            "tool_name": item.tool_name,
            "summary": item.summary,
            "risk": item.risk,
            "source": item.source,
            "schedule_id": item.schedule_id,
            "created_at": item.created_at,
            "args": aq.sanitized_args(item.args),
            "risk_reason": item.risk_reason,
            "nav_ratio": item.nav_ratio,
        }
        for item in aq.list_pending()
        if aq.owner_matches(item, current_user)
    ]
    yield _ok(items=items, count=len(items))


def approve_decide(params: dict[str, Any]) -> Iterator[Event]:
    """批准或拒绝一项。批准后立即执行，与 CLI 同一条路径。"""
    from cli import approval_queue as aq

    approval_id = str(params.get("id") or "").strip()
    if not approval_id:
        raise MethodError("invalid_params", "缺少 id")
    approved = bool(params.get("approved"))

    pending = aq.get(approval_id)
    current_user = _current_user_id()
    if pending is not None and not aq.owner_matches(pending, current_user):
        raise MethodError("account_mismatch", "审批项所属账户与当前登录不一致，已拒绝处理")

    record = aq.decide(approval_id, approved=approved)
    if record is None:
        raise MethodError(
            "not_actionable",
            f"{approval_id} 不存在、已决策，或已超过 {aq.DEFAULT_TTL_HOURS} 小时过期",
        )
    if not approved:
        yield _ok(status=record.status, summary=record.summary)
        return

    from cli.approval_executor import execute_approved

    yield {"type": "progress", "message": f"正在执行：{record.summary or record.tool_name}"}
    result = execute_approved(record.tool_name, record.args, expected_user_id=record.user_id)
    succeeded = not (isinstance(result, dict) and result.get("error"))
    aq.record_execution(record.id, result, succeeded=succeeded)
    yield _ok(status="executed" if succeeded else "failed", result=result, succeeded=succeeded)


def _synced_session(session_id: str = ""):
    """
    取会话，并先把身份对齐到磁盘上的登录态。

    session_id 为空时取当前活跃的会话（单会话时代的行为）。传了就切过去 ——
    多会话下每次对话都要说明是哪个会话。

    磁盘上的登录态可能已经变了（换账号登录），而 ToolRegistry 是 start() 时建的。
    不对齐时读操作会返回上一个账号的数据（前端还会把它当成当前账号的内容缓存
    起来），而**写操作会把新账号的改动落到旧账号的云端** —— 后者更严重，且完全
    无声。

    所有按账号取数的方法都必须过这里。原先这段逻辑内联在 portfolio() 的函数体
    里，两条写路径和 tracking/attribution 都漏了 —— 正因为它没有名字，复制不
    过去就等于忘掉。

    用 getattr 而不是直接调用：会话还没起来时（或测试里的替身）不该让整个面板
    报错，宁可退回匿名结果。
    """
    from cli.ipc.session import get_session

    # 只在真要切会话时带参数调用。大量既有测试把 get_session 打成零参 lambda
    # （持仓、跟踪、审批那些路径都不关心会话 id），无条件传参会让它们全炸。
    session = get_session(session_id) if session_id else get_session()
    sync = getattr(session, "sync_identity", None)
    if callable(sync):
        sync()
    return session


def _sync_ok(session) -> bool:
    """身份是否已对齐到磁盘上的登录态。

    问 `identity_aligned()`，**不要**拿 `sync_identity()` 的返回值当信号：后者
    返回的是「账号有没有变」(changed)，而 `False` 同时表示「账号本来就一致」
    （最常见）和「锁忙、跳过了对齐」（危险）。

    我上一版正是这么错的 —— 把所有 `False` 当成未对齐，于是稳定态下
    (同账号、锁空闲) `_sync_ok` 也返回 False，**所有正常的持仓写入都报
    identity_busy**。实测复现过。教训是：安全判断要问「现在对齐了吗」，
    而不是从一个语义不同的 bool 去推。

    没有 identity_aligned 的替身（大量既有测试）视为已对齐 —— 它们本来就不
    涉及账号切换。但仍要调一次 sync_identity 保持原有副作用。
    """
    aligned = getattr(session, "identity_aligned", None)
    if callable(aligned):
        return bool(aligned())
    # 老形态的替身：只有 sync_identity。调它保持副作用，然后视为已对齐 ——
    # 这些替身压根不模拟账号切换，拿它们的返回值当安全信号只会误伤。
    sync = getattr(session, "sync_identity", None)
    if callable(sync):
        sync()
    return True


def _write_session(session_id: str = ""):
    """写操作专用：身份没对齐就**拒绝执行**。

    写操作不能「先做了再说」—— 落到旧账号云端的改动没法自动撤回。宁可让用户
    看到一句「正在切换账号，请重试」，也不要静默写错账户。

    读操作不走这里：它们退回旧数据虽然不理想，但可恢复（下一次调用自然对齐），
    而拒绝读会让界面在切换账号的那一两秒里整片报错。

    遥控路径额外对照请求开始时钉住的 host 账号：桥 stop(wait=False) 不会打断
    已在跑的 handler，换号后若只看磁盘登录态，会把旧手机的写入落到新账号。
    """
    from cli.ipc.remote import remote_request_user_id
    from cli.ipc.session import get_session
    from integrations.local_auth import load_session

    remote_user = remote_request_user_id()
    if remote_user is not None:
        disk_user = str((load_session() or {}).get("user_id") or "")
        if remote_user != disk_user:
            raise MethodError(
                "identity_busy",
                "桌面端已切换账号，这次远程改动没有执行。请用当前登录账号重新连接后再试。",
            )

    session = get_session(session_id) if session_id else get_session()
    if not _sync_ok(session):
        raise MethodError(
            "identity_busy",
            "正在切换账号，这次没能确认是哪个账户。请稍等一下再试 —— 为避免把改动写到上一个账号，本次操作没有执行。",
        )
    return session


def _active_session() -> str:
    """当前活跃的会话 id。会话层没起来时返回空串而不是抛错。"""
    try:
        from cli.ipc.session import active_session_id

        return active_session_id()
    except Exception:
        logger.debug("active session lookup failed", exc_info=True)
        return ""


def portfolio(_params: dict[str, Any]) -> Iterator[Event]:
    """
    持仓视图。必须带上会话的 tool_context —— 否则 has_cloud() 恒为 False，
    已登录用户的 Supabase 持仓永远读不到，界面会静默显示本地 SQLite 缓存
    （可能是旧的，也可能是空的），看起来像「持仓丢了」。

    读取顺序由 portfolio_tools 决定：有 token 就先读 Supabase 并回写本地缓存，
    没有 token（或云端读失败）才落到本地 SQLite。
    """
    from agents.portfolio_tools import portfolio as portfolio_tool

    session = _synced_session()
    # 回传实际读取所用的账号。前端拿它做缓存 key，绝不能用「它自己以为的」账号：
    # 那正是缓存 key 写着 B、内容却是 A 的成因。
    yield _ok(
        portfolio=portfolio_tool(mode="view", tool_context=session.tool_context),
        user_id=str(getattr(session, "user_id", "") or ""),
    )


# 桌面端允许的持仓写入动作。
#
# 刻意不含 delete_records：它删的是推荐跟踪表而不是持仓，而且在算出
# portfolio_id 之前就 return 了——压根没有用户隔离。放进持仓编辑入口是错的。
_PORTFOLIO_ACTIONS = frozenset({"add", "update", "remove", "set_cash"})


def _exact_shares(raw: Any) -> int:
    """
    股数必须是整数，小数一律拒绝。

    原来直接 int(raw)：输入 1.9 会被静默写成 1 —— 悄悄改掉用户填的数字比报错
    危险得多，他以为记的是 1.9，账面上是 1，对不上账时也查不出是哪一步丢的。
    即便产品只支持整股，也该说「不行」，而不是替他改。
    """
    if raw is None or raw == "":
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise MethodError("invalid_params", f"股数不是数字：{raw!r}") from None
    if value != int(value):
        raise MethodError("invalid_params", f"股数必须是整数，收到 {raw}")
    return int(value)


def _portfolio_write_failed(result: dict[str, Any]) -> str:
    """
    判断一次写入是否失败，失败则返回原因。

    不能只看 success：批量部分失败时它仍然是 True，还带着非空 failures。
    只认 success 会把「3 条里错了 2 条」报成成功。
    """
    if not isinstance(result, dict):
        return "写入返回了意外的结果"
    if result.get("error"):
        return str(result["error"])
    if int(result.get("failed_count") or 0) > 0:
        failures = result.get("failures")
        return f"部分写入失败: {failures}" if failures else "部分写入失败"
    return ""


def portfolio_edit(params: dict[str, Any]) -> Iterator[Event]:
    """
    手动增删改持仓。

    这条路径**不经过审批闸门**——闸门是 ToolRegistry 里的 _confirm_callback，
    只拦 agent 提出的写操作。用户在界面上直接点的改动就是用户的意图，再让他
    审批一次自己等于没有意义。agent 走的仍是原来那条要审批的路。
    """
    from agents.portfolio_tools import update_portfolio

    action = str(params.get("action") or "").strip().lower()
    if action not in _PORTFOLIO_ACTIONS:
        raise MethodError("invalid_params", f"不支持的持仓操作: {action or '(空)'}")

    result = update_portfolio(
        action=action,
        code=str(params.get("code") or ""),
        name=str(params.get("name") or ""),
        shares=_exact_shares(params.get("shares")),
        cost_price=float(params.get("cost_price") or 0),
        buy_dt=str(params.get("buy_dt") or ""),
        free_cash=float(params.get("free_cash") or 0),
        tool_context=_write_session().tool_context,
    )
    failure = _portfolio_write_failed(result)
    if failure:
        raise MethodError("portfolio_write_failed", failure)
    yield _ok(**result)


def portfolio_set_stop(params: dict[str, Any]) -> Iterator[Event]:
    """
    设置或清除止损价。

    stop_loss 传 null 表示清除。用 'stop_loss' in params 区分「没传」和「传了
    null」—— params.get() 两者都是 None，直接用会把漏传当成清除。
    """
    from agents.portfolio_tools import set_stop_loss

    code = str(params.get("code") or "")
    if not code:
        raise MethodError("invalid_params", "需要 code")
    if "stop_loss" not in params:
        raise MethodError("invalid_params", "需要 stop_loss（传 null 表示清除）")

    raw = params.get("stop_loss")
    clears_stop = raw is None or (isinstance(raw, str) and not raw.strip())
    try:
        stop_loss = None if clears_stop else float(raw)
    except (TypeError, ValueError) as exc:
        raise MethodError("invalid_params", "stop_loss 需为数字或 null") from exc
    result = set_stop_loss(
        code=code,
        stop_loss=stop_loss,
        tool_context=_write_session().tool_context,
    )
    failure = _portfolio_write_failed(result)
    if failure:
        raise MethodError("portfolio_write_failed", failure)
    yield _ok(**result)


def tracking(params: dict[str, Any]) -> Iterator[Event]:
    """
    推荐跟踪。与持仓同理：必须带上会话上下文，否则读不到用户自己的云端记录，
    只能拿到本地缓存（缺 mfe/mae 列，最高/最低会是空的）。

    query_history 内建云端 + 本地回退，这里不重复实现读取顺序。
    """
    from agents.history_tools import query_history
    from integrations.supabase_recommendation import RECOMMENDATION_TABLES

    # 三个市场是三张表，切市场必须重查。在这里就把市场收敛到白名单里 ——
    # 让未知值一路传到下游再兜底，中间任何一环拿它拼表名都会成为漏洞。
    market = str(params.get("market") or "cn").strip().lower()
    if market not in RECOMMENDATION_TABLES:
        market = "cn"
    limit = _clamp_int(params.get("limit"), 200, 1, 200)
    result = query_history(
        source="recommendation",
        limit=limit,
        tool_context=_synced_session().tool_context,
        market=market,
    )
    if "error" in result:
        raise MethodError("tracking_failed", str(result["error"]))
    yield _ok(**result)


def attribution_dates(params: dict[str, Any]) -> Iterator[Event]:
    """
    报告日期列表，不含正文。

    页签只需要日期。整份报告约 14 KB，把 20 份正文一起拉下来要 8 秒 —— 页签
    却只用到其中两三个字段。这条走一次窄 select，页签能立刻出来。
    """
    from agents.history_tools import attribution_dates as load_dates

    limit = _clamp_int(params.get("limit"), 60, 1, 200)
    result = load_dates(limit=limit, tool_context=_synced_session().tool_context)
    if "error" in result:
        raise MethodError("attribution_failed", str(result["error"]))
    yield _ok(**result)


def attribution(params: dict[str, Any]) -> Iterator[Event]:
    """
    策略归因报告。全局数据（按 market 过滤，不分用户），但仍传上下文 ——
    有登录态时用用户客户端读，没有才退到匿名客户端。

    默认 20、上限 40：界面现在可以翻看任意一天的完整报告，不再只展开最新一份，
    所以原来那个「翻更多没有意义」的上限 10 不再成立。库里按天累积（目前 35 份），
    40 够覆盖两个多月。
    """
    from agents.history_tools import query_history

    # 默认只拉 1 份：整份报告约 14 KB，一次 20 份要 8 秒。页签用
    # attribution_dates 单独取（只两列），正文按翻到哪页再取哪页。
    limit = _clamp_int(params.get("limit"), 1, 1, 40)
    report_date = str(params.get("report_date") or "").strip()
    result = query_history(
        source="attribution",
        limit=limit,
        tool_context=_synced_session().tool_context,
        report_date=report_date,
    )
    if "error" in result:
        raise MethodError("attribution_failed", str(result["error"]))
    yield _ok(**result)


# 日线上限：一屏 K 线图看不了更多，且列式 payload 也要有个封顶。
MAX_OHLCV_DAYS = 1200
DEFAULT_OHLCV_DAYS = 320


def ohlcv(params: dict[str, Any]) -> Iterator[Event]:
    """K 线序列，列式返回。

    列式（{date: [...], close: [...]}）而非对象数组：320 根日线约 40KB，
    对象数组要 100KB 上下，而画图端本来就按列消费。
    """
    adjust = str(params.get("adjust") or "qfq")
    if adjust not in ("qfq", "hfq", ""):
        raise MethodError("invalid_params", f"不支持的 adjust: {adjust}")
    symbol, frame = _chart_frame(params, adjust=adjust or "qfq")
    yield _ok(symbol=symbol, adjust=adjust or "qfq", bars=_columnar(frame))


def wyckoff_events(params: dict[str, Any]) -> Iterator[Event]:
    """图表标注所需的威科夫结构：历史事件 + 支撑阻力带 + 目标位。

    事件由 ``core.event_replay`` 重放生产检测器得到，因此图上标的和漏斗选的
    是同一套规则。结构缺失时对应字段为 None —— 图少画一层比画错一层好。
    """
    from core.event_replay import replay_events

    symbol, frame = _chart_frame(params)
    frame = frame.reset_index(drop=True)
    events = replay_events(frame, code=symbol)
    yield _ok(
        symbol=symbol,
        events=events,
        trading_range=_trading_range_payload(frame),
        targets=_targets_payload(frame),
        annotations=_annotations_payload(symbol),
    )


def chart_data(params: dict[str, Any]) -> Iterator[Event]:
    """同一份行情快照派生 K 线与所有标注，避免重复取数和快照漂移。"""
    from core.event_replay import replay_events

    symbol, frame = _chart_frame(params)
    frame = frame.reset_index(drop=True)
    yield _ok(
        symbol=symbol,
        adjust="qfq",
        bars=_columnar(frame),
        events=replay_events(frame, code=symbol),
        trading_range=_trading_range_payload(frame),
        targets=_targets_payload(frame),
        annotations=_annotations_payload(symbol),
    )


def _chart_frame(params: dict[str, Any], *, adjust: str = "qfq") -> tuple[str, Any]:
    from datetime import date, timedelta

    from core.portfolio_symbol import normalize_portfolio_code
    from integrations.stock_hist_repository import get_stock_hist, normalize_hist_df

    raw_symbol = str(params.get("symbol") or "").strip()
    if not raw_symbol:
        raise MethodError("invalid_params", "缺少 symbol")
    # 用户现在能手输代码，不再只有 agent 传规范码进来。复用持仓账本那套
    # 正规化规则，而不是在图表这边另写一份 —— 两份规则迟早会漂移。
    symbol = normalize_portfolio_code(raw_symbol)
    if not symbol:
        raise MethodError("invalid_params", f"认不出这个代码：{raw_symbol}")
    days = _clamp_int(params.get("days"), DEFAULT_OHLCV_DAYS, 2, MAX_OHLCV_DAYS)
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 40)
    try:
        raw = get_stock_hist(symbol, start, end, adjust=adjust)
    except Exception as exc:
        raise MethodError("data_unavailable", f"取不到 {symbol} 的历史行情") from exc
    if raw is None or raw.empty:
        raise MethodError("data_unavailable", f"{symbol} 没有历史行情数据")
    return symbol, normalize_hist_df(raw).tail(days)


def _annotations_payload(symbol: str) -> list[dict[str, Any]]:
    """agent 画的标注。读不到就当没有 —— 图照常显示自动识别的部分。"""
    try:
        from integrations.chart_annotations import load, make_chart_id

        return load(make_chart_id(symbol))
    except Exception:
        return []


def _trading_range_payload(frame: Any) -> dict[str, Any] | None:
    """支撑/阻力带 —— 图上画成一个矩形区。"""
    try:
        from core.wyckoff_structure import identify_trading_range

        found = identify_trading_range(frame)
    except Exception:
        return None
    if found is None:
        return None
    return {
        "support": found.support,
        "resistance": found.resistance,
        "mid": found.mid,
        "width_pct": found.width_pct,
        "support_tests": found.support_tests,
        "resistance_tests": found.resistance_tests,
        "quality_score": found.quality_score,
    }


def _targets_payload(frame: Any) -> dict[str, Any] | None:
    """目标位 —— 图上画成几条水平线。"""
    try:
        from core.price_targets import compute_price_targets

        found = compute_price_targets(frame["close"], frame["high"], frame["low"])
    except Exception:
        return None
    if found is None:
        return None
    return {
        "last_close": found.last_close,
        "measured_move": found.measured_move,
        "prior_high": found.prior_high,
        "atr_multiple": found.atr_multiple,
        "conservative": found.conservative,
        "aggressive": found.aggressive,
    }


def _columnar(frame: Any) -> dict[str, list[Any]]:
    """DataFrame -> 列式 dict，NaN 换成 None（JSON 没有 NaN）。"""
    import math

    out: dict[str, list[Any]] = {}
    for col in ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if col not in frame.columns:
            continue
        values = frame[col].tolist()
        if col == "date":
            out[col] = [str(v)[:10] for v in values]
            continue
        cleaned: list[Any] = []
        for v in values:
            try:
                num = float(v)
            except (TypeError, ValueError):
                cleaned.append(None)
                continue
            cleaned.append(None if math.isnan(num) or math.isinf(num) else num)
        out[col] = cleaned
    return out


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def schedules(_params: dict[str, Any]) -> Iterator[Event]:
    """已有任务 + 可推荐的预置。

    预置一起返回，界面才能在「还没有任何任务」时给出可点的建议。已经添加过的
    （按 id 判断）从推荐里剔掉 —— 列一个点了会重复的选项等于埋个坑。
    """
    from cli.daemon import is_daemon_running
    from cli.scheduler import DEFAULT_PRESETS, load_schedules, schedule_status

    try:
        existing = load_schedules()
    except Exception as exc:
        # 文件坏了要说出来。返回空列表会让界面显示「还没有任务」，用户新建一个就
        # 把原来的覆盖掉了。
        raise MethodError("schedules_unreadable", f"读取定时任务失败：{exc}") from exc

    taken = {s.id for s in existing}
    yield _ok(
        schedules=schedule_status(existing),
        presets=[
            {"id": p["id"], "name": p["name"], "cron": p["cron"], "action": p["action"]}
            for p in DEFAULT_PRESETS
            if p["id"] not in taken
        ],
        daemon_running=is_daemon_running(),
    )


def _load_for_write() -> list[Any]:
    """写路径专用的读取。必须在 schedules_lock 里调。"""
    from cli.scheduler import load_schedules

    try:
        return load_schedules()
    except Exception as exc:
        raise MethodError("schedules_unreadable", f"读取定时任务失败：{exc}") from exc


def _checked_cron(cron: str) -> str:
    """校验 cron，不合法就带上具体原因拒掉。

    一个坏的 cron 不只是它自己不触发 —— `_field_matches` 里的 int() 会抛
    ValueError，被 daemon 的宽 except 抓住，那一轮**所有**任务都被跳过。
    所以绝不能让它落盘。
    """
    from cli.scheduler import validate_cron

    text = str(cron or "").strip()
    problem = validate_cron(text)
    if problem:
        raise MethodError("invalid_cron", f"触发时间不合法：{problem}")
    return text


def _checked_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise MethodError("invalid_params", "需要任务名称")
    return text[:60]


def _checked_action(action: str) -> str:
    text = str(action or "").strip()
    if not text:
        raise MethodError("invalid_params", "需要任务内容")
    return text[:2000]


def schedule_create(params: dict[str, Any]) -> Iterator[Event]:
    """新建一个定时任务。"""
    from cli.scheduler import DEFAULT_PRESETS, Schedule, save_schedules, schedule_status, schedules_lock

    name = _checked_name(params.get("name"))
    cron = _checked_cron(params.get("cron"))
    action = _checked_action(params.get("action"))
    # 新建默认**启用**。用户刚填完表单点确定，那个意图就是「让它开始跑」；
    # 建完还得再点一下开关才生效，只会让人以为没保存成功。
    enabled = bool(params.get("enabled", True))

    # 从推荐添加时沿用预置的 id。
    #
    # 不这样做的话，新任务拿到的是时间戳 id，而 `schedules` 是按 id 把已添加的预置
    # 从推荐里剔掉的 —— 于是推荐永远不消失，用户可以把同一个预置加进去好几次。
    # 只认白名单里的 id，别让前端随便指定。
    preset_ids = {p["id"] for p in DEFAULT_PRESETS}
    wanted = str(params.get("id") or "").strip()
    if wanted and wanted not in preset_ids:
        raise MethodError("invalid_params", "id 只能是预置任务的标识")

    with schedules_lock():
        current = _load_for_write()
        if len(current) >= 40:
            raise MethodError("too_many", "定时任务最多 40 个")
        taken = {s.id for s in current}
        if wanted:
            if wanted in taken:
                raise MethodError("already_exists", "这个推荐任务已经添加过了")
            new_id = wanted
        else:
            # id 用时间戳而不是 uuid：便于排查问题时对上日志里的时间。
            # 带上计数后缀防同一毫秒内建两个。
            base = f"s{int(time.time() * 1000)}"
            new_id = (
                base
                if base not in taken
                else next(f"{base}-{n}" for n in itertools.count(2) if f"{base}-{n}" not in taken)
            )
        created = Schedule(id=new_id, name=name, cron=cron, action=action, enabled=enabled)
        current.append(created)
        save_schedules(current)

    yield _ok(created=schedule_status([created])[0])


def schedule_update(params: dict[str, Any]) -> Iterator[Event]:
    """改一个任务的名称 / 触发时间 / 内容。

    局部更新：用 `"k" in params` 区分「这次不改这个字段」和「显式传了空值」。
    照 portfolio_set_stop 的先例 —— params.get() 两者都是 None，会把漏传当成清空。
    """
    from cli.scheduler import save_schedules, schedule_status, schedules_lock

    schedule_id = str(params.get("id") or "").strip()
    if not schedule_id:
        raise MethodError("invalid_params", "需要 id")
    if not any(k in params for k in ("name", "cron", "action")):
        raise MethodError("invalid_params", "至少要改一个字段（name / cron / action）")

    with schedules_lock():
        current = _load_for_write()
        target = next((s for s in current if s.id == schedule_id), None)
        if target is None:
            raise MethodError("not_found", f"没有这个定时任务：{schedule_id}")
        if "name" in params:
            target.name = _checked_name(params.get("name"))
        if "cron" in params:
            target.cron = _checked_cron(params.get("cron"))
        if "action" in params:
            target.action = _checked_action(params.get("action"))
        save_schedules(current)
        updated = schedule_status([target])[0]

    yield _ok(updated=updated)


def schedule_toggle(params: dict[str, Any]) -> Iterator[Event]:
    """开启或关闭一个任务。"""
    from cli.scheduler import save_schedules, schedule_status, schedules_lock

    schedule_id = str(params.get("id") or "").strip()
    if not schedule_id:
        raise MethodError("invalid_params", "需要 id")
    if "enabled" not in params:
        raise MethodError("invalid_params", "需要 enabled")
    enabled = bool(params.get("enabled"))

    with schedules_lock():
        current = _load_for_write()
        target = next((s for s in current if s.id == schedule_id), None)
        if target is None:
            raise MethodError("not_found", f"没有这个定时任务：{schedule_id}")
        # 开启前再校验一次 cron。老任务可能是 TUI 或手改文件写进来的，
        # 从没被校验过；带着坏 cron 开启会打死整个调度轮次。
        if enabled:
            _checked_cron(target.cron)
        target.enabled = enabled
        save_schedules(current)
        result = schedule_status([target])[0]

    yield _ok(updated=result)


def schedule_delete(params: dict[str, Any]) -> Iterator[Event]:
    """删除一个任务。"""
    from cli.scheduler import save_schedules, schedules_lock

    schedule_id = str(params.get("id") or "").strip()
    if not schedule_id:
        raise MethodError("invalid_params", "需要 id")

    with schedules_lock():
        current = _load_for_write()
        remaining = [s for s in current if s.id != schedule_id]
        if len(remaining) == len(current):
            raise MethodError("not_found", f"没有这个定时任务：{schedule_id}")
        save_schedules(remaining)

    yield _ok(deleted=schedule_id)


# 正在手动重跑的 schedule id。重跑要跑完整一轮 agent（可能几分钟），期间用户
# 很容易再点一次；同一个任务并行跑两轮会写重复的审批和记录。
_rerunning: set[str] = set()
_rerun_lock = threading.Lock()


def schedule_run(params: dict[str, Any]) -> Iterator[Event]:
    """手动重跑一个定时任务。用于失败后重试，不改动它的 cron 与下次触发时间。"""
    from cli.headless import run_once
    from cli.scheduler import load_schedules

    schedule_id = str(params.get("id") or "").strip()
    if not schedule_id:
        raise MethodError("invalid_params", "缺少 id")

    schedule = next((s for s in load_schedules() if s.id == schedule_id), None)
    if schedule is None:
        raise MethodError("not_found", f"找不到定时任务 {schedule_id}")
    if not schedule.action.strip():
        raise MethodError("invalid_params", f"{schedule.name} 没有配置要执行的动作")

    with _rerun_lock:
        if schedule_id in _rerunning:
            raise MethodError("already_running", f"{schedule.name} 正在运行，请等这一轮结束")
        _rerunning.add(schedule_id)

    try:
        yield {"type": "progress", "message": f"正在重跑：{schedule.name}"}
        # source="manual" 而不是 "daemon"：这一轮产生的审批要能看出是人点的，
        # 否则事后分不清哪些是无人值守跑出来的。
        result = run_once(schedule.action, source="manual", schedule_id=schedule_id)
    finally:
        with _rerun_lock:
            _rerunning.discard(schedule_id)

    # 不回写 last_status：那几个字段记录的是排程自动执行的结果，手动重跑覆盖它
    # 会让「上次自动跑成功了吗」这个问题再也答不上来。
    if not result.ok:
        yield _ok(ok=False, error=result.error[:500], queued=list(result.queued), name=schedule.name)
        return
    yield _ok(ok=True, queued=list(result.queued), name=schedule.name)


def account(_params: dict[str, Any]) -> Iterator[Event]:
    """当前登录态。绝不返回 token 或密码，只返回身份标识。

    用 `restore_session()` 而不是 `load_session()`：后者只是读一下文件，
    **不会**续期、也不会用已保存的凭据重登。CLI 启动走 restore，桌面端原来走
    load —— 于是同一台机器上 CLI 已登录而桌面端显示未登录；token 过期后桌面端
    也会直接把用户踢回登录页，而它本来能自己续上。

    restore 会验 token、过期则续、续不上则用保存的凭据重登；真拿不到才返回 None。
    """
    from integrations.local_auth import restore_session

    # restore 会走到 Supabase 调用；网络不通时不该让这个方法抛异常 ——
    # 界面需要一个明确的「未登录」，而不是一个错误弹窗。
    try:
        session = restore_session() or {}
    except Exception:
        logger.warning("session restore failed", exc_info=True)
        session = {}
    # last_email 只用于登录页预填。它**不是**凭据：退出登录会清掉 email/password
    # 而刻意保留它，所以拿到它并不代表能登录。
    from integrations.local_auth import load_config

    last_email = ""
    try:
        last_email = str((load_config() or {}).get("last_email") or "")
    except Exception:
        logger.debug("last_email read failed", exc_info=True)

    yield _ok(
        signed_in=bool(session.get("access_token")),
        email=str(session.get("email") or ""),
        user_id=str(session.get("user_id") or ""),
        last_email=last_email,
    )


def auth_login(params: dict[str, Any]) -> Iterator[Event]:
    """邮箱密码登录。与 CLI 同一条路径、同一份 session 文件。

    密码只在这个进程里存在一次：它从 IPC 参数进来，交给 Supabase，然后由
    local_auth 写进 `~/.wyckoff/wyckoff.json`（0600）供 auto_relogin 用。
    **绝不 log、绝不回传**给渲染层 —— 那份 json 是 CLI 早就有的行为，
    但日志和 IPC 响应是新增面，不能顺手扩大暴露。

    登录成功后拉一次云端配置：用户可能把模型和数据源都配在 web 端，
    不拉下来的话桌面端登录了却还是「未配置模型」。
    """
    from integrations.local_auth import login

    email = str(params.get("email") or "").strip()
    password = str(params.get("password") or "")
    if not email or not password:
        raise MethodError("invalid_params", "需要邮箱和密码")

    # 换号前先拆遥控：必须还握着旧 token，才能 revoke 已配对手机。
    _teardown_remote_on_identity_change()

    try:
        session = login(email, password)
    except Exception as exc:  # supabase 的异常类型随版本变，不按类型分支
        # 区分「密码不对」和「连不上」—— 两者的下一步动作完全不同，
        # 统一报「登录失败」会让用户反复试密码而其实是网络问题。
        text = str(exc).lower()
        if "invalid" in text or "credential" in text or "password" in text:
            raise MethodError("bad_credentials", "邮箱或密码不正确") from None
        logger.warning("desktop login failed", exc_info=True)
        raise MethodError("login_failed", "登录没能完成，请检查网络后重试") from None

    # 登录换了身份，常驻会话的 ToolRegistry 必须跟着重建，
    # 否则这一轮之后的工具还在用上一个账号的 token。
    _synced_session()

    from cli.ipc.cloud_config import pull_cloud_config

    pulled = pull_cloud_config(session.get("user_id", ""), session.get("access_token", ""))
    yield _ok(
        signed_in=True,
        email=str(session.get("email") or ""),
        user_id=str(session.get("user_id") or ""),
        # 拉下来几项配置。前端据此决定要不要提示「已同步云端配置」。
        synced=pulled,
    )


def auth_logout(_params: dict[str, Any]) -> Iterator[Event]:
    """退出登录。清掉 session 与自动重登凭据。"""
    from integrations.local_auth import logout

    # 先拆遥控再清 session：revoke 还需要当前 token。
    _teardown_remote_on_identity_change()
    logout()
    # 同上：身份变了要重建 registry，否则退出后工具还拿着旧 token 读云端。
    _synced_session()
    yield _ok(signed_in=False)


def artifact_list(_params: dict[str, Any]) -> Iterator[Event]:
    """列出**当前账号的**报告产物。

    身份从会话取，不接受前端传参：那等于让渲染层决定读谁的报告。
    """
    from cli.ipc.artifacts import list_artifacts

    user_id = _synced_session().user_id
    items = [
        {
            "name": a.name,
            "rel_path": a.rel_path,
            "kind": a.kind,
            "size": a.size,
            "modified_at": a.modified_at,
        }
        for a in list_artifacts(user_id)
    ]
    yield _ok(items=items, count=len(items))


def artifact_read(params: dict[str, Any]) -> Iterator[Event]:
    """读取单个产物内容供容器渲染。"""
    from cli.ipc.artifacts import ArtifactError, read_artifact

    try:
        # 同 artifact_list：身份来自会话，不是前端参数。
        yield _ok(**read_artifact(str(params.get("path") or ""), _synced_session().user_id))
    except ArtifactError as exc:
        raise MethodError(exc.code, str(exc)) from exc


def artifact_import(params: dict[str, Any]) -> Iterator[Event]:
    """把用户拖进来的文件复制到报告目录。

    走 _write_session：这是**写操作**（往账号分区里复制文件）。用
    _synced_session 的话，身份错位时会把文件写进上一个账号的分区 ——
    用户以为导入到自己名下，实际进了别人的目录，而且完全无声。
    """
    from cli.ipc.artifacts import ArtifactError, import_file

    try:
        artifact = import_file(str(params.get("source") or ""), _write_session().user_id)
    except ArtifactError as exc:
        raise MethodError(exc.code, str(exc)) from exc

    yield _ok(
        imported=True,
        name=artifact.name,
        rel_path=artifact.rel_path,
        kind=artifact.kind,
        size=artifact.size,
    )


def model_add(params: dict[str, Any]) -> Iterator[Event]:
    """新增或覆盖一个模型条目。

    api_key 只进配置文件，永不回传给前端（settings_get 只报 has_key）。
    """
    from cli.providers import PROVIDERS
    from integrations.local_auth import save_model_entry

    model_id = str(params.get("id") or "").strip()
    provider_name = str(params.get("provider_name") or "").strip()
    model = str(params.get("model") or "").strip()
    api_key = str(params.get("api_key") or "").strip()
    base_url = str(params.get("base_url") or "").strip()
    thinking_level = str(params.get("thinking_level") or "").strip().lower()

    if not model_id:
        raise MethodError("invalid_params", "缺少模型标识")
    if provider_name not in PROVIDERS:
        raise MethodError("invalid_params", f"未知 provider: {provider_name}（可选: {', '.join(PROVIDERS)}）")
    if not model:
        raise MethodError("invalid_params", "缺少模型名")
    if not api_key:
        raise MethodError("invalid_params", "缺少 API Key")
    if thinking_level and thinking_level not in {"off", "low", "high", "max"}:
        raise MethodError("invalid_params", "DeepSeek 思考强度必须是 off/low/high/max")

    save_model_entry(
        {
            "id": model_id,
            "provider_name": provider_name,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "thinking_level": thinking_level if provider_name == "deepseek" else "",
        }
    )
    _reload_desktop_session()
    yield _ok(saved=True, model_id=model_id)


def model_remove(params: dict[str, Any]) -> Iterator[Event]:
    """删除模型条目。最后一个不允许删——删光了应用就没法工作。"""
    from integrations.local_auth import remove_model_entry

    model_id = str(params.get("id") or "").strip()
    if not model_id:
        raise MethodError("invalid_params", "缺少模型标识")
    if not remove_model_entry(model_id):
        raise MethodError("last_model", "至少要保留一个模型")
    _reload_desktop_session()
    yield _ok(removed=True, model_id=model_id)


def model_test(params: dict[str, Any]) -> Iterator[Event]:
    """实际发一次最小请求验证连通性。

    只验证「密钥有效且模型可达」，因此把 token 压到最低。不做流式，也不
    进对话历史。
    """
    import time

    from cli.provider_factory import create_provider, provider_config_kwargs
    from integrations.local_auth import load_model_configs

    model_id = str(params.get("id") or "").strip()
    entry = next((m for m in load_model_configs() if m.get("id") == model_id), None)
    if entry is None:
        raise MethodError("not_found", f"找不到模型: {model_id}")

    yield {"type": "progress", "message": f"正在连接 {model_id}…"}

    test_entry = {**entry, "thinking_level": "off"} if entry.get("provider_name") == "deepseek" else entry
    provider, error = create_provider(**provider_config_kwargs(test_entry))
    if provider is None:
        yield _ok(model_id=model_id, connected=False, error=error or "无法构造 provider")
        return

    started = time.monotonic()
    try:
        # tools 是必需位置参数；传空列表，连通性测试不需要工具。
        reply = provider.chat(
            [{"role": "user", "content": "ping"}],
            [],
            system_prompt="Reply with the single word: pong",
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都算连不通，要原样报给用户
        yield _ok(model_id=model_id, connected=False, error=f"{type(exc).__name__}: {exc}"[:300])
        return

    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = ""
    if isinstance(reply, dict):
        text = str(reply.get("text") or reply.get("content") or "")
    else:
        text = str(reply or "")
    yield _ok(model_id=model_id, connected=True, latency_ms=elapsed_ms, sample=text.strip()[:80])


_LAUNCHD_LABEL = "com.wyckoff.daemon"


def _launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def daemon_status(_params: dict[str, Any]) -> Iterator[Event]:
    """定时 daemon 是否在跑、是否已注册为登录项。

    当前桌面契约是应用打开期间运行；installed/loaded 只用于发现和清理
    历史 launchd 服务，避免桌面关闭后仍意外执行任务。
    """
    from cli.daemon import is_daemon_running

    installed = _launchd_plist().exists()
    loaded = False
    if sys.platform == "darwin" and installed:
        result = subprocess.run(  # noqa: S603
            ["/bin/launchctl", "list", _LAUNCHD_LABEL],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        loaded = result.returncode == 0

    yield _ok(
        running=is_daemon_running(),
        installed=installed,
        loaded=loaded,
        supported=sys.platform == "darwin",
        plist=str(_launchd_plist()),
    )


def daemon_uninstall(_params: dict[str, Any]) -> Iterator[Event]:
    """注销 launchd 服务并删除 plist。

    刻意不提供对称的 install：调度进程由桌面应用自己拉起（见
    desktop/src/daemon-runner.js），关掉应用就不该再跑定时任务。这个方法只用于
    清理历史遗留的 launchd 服务——它会在应用关闭时照样执行任务，与设计相悖。
    """
    if sys.platform != "darwin":
        raise MethodError("unsupported", "launchd 只在 macOS 可用。")

    plist = _launchd_plist()
    yield {"type": "progress", "message": "正在注销 launchd 服务…"}
    # bootout 失败通常只是本来就没加载，不该因此中断删除 plist。
    subprocess.run(  # noqa: S603
        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        plist.unlink(missing_ok=True)
    except OSError as exc:
        raise MethodError("uninstall_failed", f"删除 plist 失败: {exc}") from exc

    yield _ok(installed=False)


def sign_out(_params: dict[str, Any]) -> Iterator[Event]:
    """清除本地会话。同时清掉配置里的凭据，否则 auto_relogin 会立刻登回去。"""
    from integrations.local_auth import load_config, logout, save_config_key

    # 先拆遥控再清 session：否则已配对手机仍经本机 IPC 操作后续登录者的数据。
    _teardown_remote_on_identity_change()
    logout()
    # login() 会把 email/password 写进 config，auto_relogin 靠它静默恢复会话。
    # 只删 session 文件的话，「退出登录」下一次启动就自动失效了。
    config = load_config()
    for key in ("email", "password"):
        if key in config:
            save_config_key(key, "")
    from cli.ipc.session import shutdown_session

    shutdown_session()
    yield _ok(signed_out=True)


def _current_user_id() -> str:
    from cli.auth import load_session

    return str((load_session() or {}).get("user_id") or "")


def settings_get(_params: dict[str, Any]) -> Iterator[Event]:
    """设置页需要的真实配置。api_key 只报是否已配置，不回传值。"""
    from integrations.local_auth import (
        DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
        DEFAULT_TOOL_TIMEOUT_SECONDS,
        load_config,
        load_default_model_id,
        load_fallback_model_id,
        load_model_configs,
    )

    config = load_config()
    models = [
        {
            "id": str(m.get("id") or ""),
            "model": str(m.get("model") or ""),
            "provider_name": str(m.get("provider_name") or ""),
            # base_url 不是机密（密钥才是），前端要靠它区分同名自建端点。
            "base_url": str(m.get("base_url") or ""),
            "thinking_level": str(m.get("thinking_level") or ""),
            "has_key": bool(m.get("api_key")),
        }
        for m in load_model_configs()
    ]
    appearance = {key: config.get(key, default) for key, default in DESKTOP_APPEARANCE_DEFAULTS.items()}

    yield _ok(
        models=models,
        default_model=load_default_model_id() or "",
        fallback_model=load_fallback_model_id() or "",
        theme=str(config.get("theme") or "light"),
        **appearance,
        stream_chunk_timeout_seconds=int(
            config.get("stream_chunk_timeout_seconds") or DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS
        ),
        tool_timeout_seconds=int(config.get("tool_timeout_seconds") or DEFAULT_TOOL_TIMEOUT_SECONDS),
        has_tickflow_key=bool(config.get("tickflow_api_key")),
        has_tushare_token=bool(config.get("tushare_token")),
    )


# 桌面端外观/行为配置。全部用 desktop_ 前缀，避免和 TUI 的 theme（值是
# textual-dark 这类 Textual 专用主题名）互相覆盖——两边含义不同。
DESKTOP_APPEARANCE_DEFAULTS: dict[str, Any] = {
    "desktop_appearance": "system",  # system | light | dark
    "desktop_font_scale": 100,  # 80–140，百分比
    "desktop_font_family": "sans",  # sans | serif
    "desktop_density": "cozy",  # cozy | compact
    "desktop_reduce_motion": False,
    "desktop_send_on_enter": True,
    "desktop_tone": "default",  # default = 沿用 TUI 提示词，不额外插入
    "desktop_tone_custom": "",
}

_APPEARANCE_CHOICES: dict[str, frozenset[str]] = {
    "desktop_appearance": frozenset({"system", "light", "dark"}),
    "desktop_font_family": frozenset({"sans", "serif"}),
    "desktop_density": frozenset({"cozy", "compact"}),
    "desktop_tone": frozenset({"default", "brief", "detailed", "evidence", "custom"}),
}

_TONE_CUSTOM_MAX = 600

_SETTABLE_KEYS = frozenset(
    {"theme", "stream_chunk_timeout_seconds", "tool_timeout_seconds"} | set(DESKTOP_APPEARANCE_DEFAULTS)
)

# 超时类键的合法区间（秒）。
#
# 上界不是洁癖：这两个值决定「等多久算卡住」，写成 0 会让每次调用立刻超时、
# 写成 10 天等于没有超时 —— 两种都能让应用看起来坏掉，而用户改不回来（见
# _coerge 兜底那段注释：坏值会让整个设置页读不出来）。
_TIMEOUT_BOUNDS = {
    "stream_chunk_timeout_seconds": (5, 1800),
    "tool_timeout_seconds": (5, 1800),
}

# theme 只有这两种。它不在 DESKTOP_APPEARANCE_DEFAULTS 里（那套是 desktop_ 前缀
# 的外观键），所以要单独列，否则会掉进「没有校验规则」的兜底里。
_THEME_CHOICES = frozenset({"light", "dark"})


def _coerce_timeout(key: str, value: Any) -> int:
    low, high = _TIMEOUT_BOUNDS[key]
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise MethodError("invalid_value", f"{key} 需为整数秒，收到 {value!r}") from None
    if isinstance(value, float) and not value.is_integer():
        raise MethodError("invalid_value", f"{key} 需为整数秒，收到 {value!r}")
    if isinstance(value, str) and value.strip() != str(seconds):
        raise MethodError("invalid_value", f"{key} 需为整数秒，收到 {value!r}")
    if not low <= seconds <= high:
        raise MethodError("invalid_value", f"{key} 需在 {low}~{high} 秒之间，收到 {seconds}")
    return seconds


def _coerce_desktop_value(key: str, value: Any) -> Any:
    """把前端传来的值收敛到合法范围，非法就抛错而不是静默写坏配置。"""
    if key in _APPEARANCE_CHOICES:
        text = str(value or "")
        if text not in _APPEARANCE_CHOICES[key]:
            raise MethodError("invalid_value", f"{key} 不接受的值: {text}")
        return text
    if key == "desktop_font_scale":
        try:
            scale = int(value)
        except (TypeError, ValueError):
            raise MethodError("invalid_value", "字号需为整数百分比") from None
        # 钳制而不是报错：滑块拖到边界属于正常操作。
        return max(80, min(140, scale))
    if key in {"desktop_reduce_motion", "desktop_send_on_enter"}:
        return bool(value)
    if key == "desktop_tone_custom":
        # 会拼进系统提示词，必须限长，否则可挤掉真正的指令。
        return str(value or "")[:_TONE_CUSTOM_MAX]
    if key in _TIMEOUT_BOUNDS:
        return _coerce_timeout(key, value)
    if key == "theme":
        text = str(value or "")
        if text not in _THEME_CHOICES:
            raise MethodError("invalid_value", f"theme 不接受的值: {text}")
        return text
    # 兜底不能是 return value：那样任何新加进 _SETTABLE_KEYS 又忘了写分支的键
    # 都会被原样写进配置。这两个超时键就是这么漏的 —— 存进一个非整数之后，
    # settings_get 的 int(...) 会抛 ValueError，**整个设置页读不出来**，
    # 而且用户没法从界面上改回去（读不出来就渲染不了表单）。
    raise MethodError("invalid_key", f"{key} 没有对应的校验规则，拒绝写入")


def settings_set(params: dict[str, Any]) -> Iterator[Event]:
    """只允许白名单里的键。避免前端把任意配置（含凭据）写进配置文件。"""
    from integrations.local_auth import save_config_key, set_default_model, set_fallback_model

    key = str(params.get("key") or "")
    value = params.get("value")

    if key == "default_model":
        set_default_model(str(value or ""))
        _reload_desktop_session()
    elif key == "fallback_model":
        set_fallback_model(str(value or ""))
        _reload_desktop_session()
    elif key in _SETTABLE_KEYS:
        save_config_key(key, _coerce_desktop_value(key, value))
    else:
        raise MethodError("invalid_key", f"不可设置的配置项: {key}")

    yield _ok(saved=True, key=key)


def _reload_desktop_session() -> None:
    """让下轮对话使用刚保存的模型配置。"""
    from cli.ipc.session import shutdown_session

    shutdown_session()


def mcp_list(_params: dict[str, Any]) -> Iterator[Event]:
    from cli.mcp_config import is_builtin_duplicate, load_servers

    yield _ok(
        servers=[
            {
                "name": s.name,
                "command": s.command,
                "args": s.args,
                "enabled": s.enabled,
                "builtin_duplicate": is_builtin_duplicate(s),
            }
            for s in load_servers()
        ]
    )


def health(_params: dict[str, Any]) -> Iterator[Event]:
    from cli.daemon import is_daemon_running

    yield _ok(ready=True, daemon_running=is_daemon_running())


# -- 对话：流式 --------------------------------------------------------------


def chat(params: dict[str, Any]) -> Iterator[Event]:
    """跑一轮对话，把 runtime 事件透传给前端。"""
    text = str(params.get("text") or "").strip()
    if not text:
        raise MethodError("invalid_params", "缺少 text")

    session = _synced_session(str(params.get("session_id") or ""))
    # 先告诉前端这一轮归哪个会话 —— 用户可能在流式输出期间切走，事件到达时
    # 需要知道往哪个会话的时间线上贴。getattr 兜底是给测试替身留的余地。
    sid = str(getattr(session, "session_id", "") or "")
    if sid:
        yield _ok(session_id=sid)
    yield from session.run_turn(text)


def chat_reset(_params: dict[str, Any]) -> Iterator[Event]:
    """开一个新会话。

    语义变了：原来是把当前会话清空（旧对话就没了）。现在开新的、旧的留在列表里 ——
    这才是「新分析」该做的事。方法名保留，前端不用改调用点。
    """
    from cli.ipc.session import new_session

    _synced_session()  # 先对齐身份，再开会话
    session = new_session()
    yield _ok(reset=True, session_id=session.session_id)


# 云端信箱的地址。与 web 端硬编码的同一个 Worker（web/apps/web/src/lib/api-url.ts）。
# 允许用环境变量覆盖，好在本地 wrangler dev 上联调。
REMOTE_API_BASE = os.environ.get("WYCKOFF_API_BASE", "https://wyckoff-api.yongkai-wang.workers.dev")


def _teardown_remote_on_identity_change() -> None:
    """身份切换前拆遥控桥。

    遥控复用同一批 IPC：手机打到本机后，dispatch 读的是**磁盘当前登录态**；
    桥上挂的却是开启时的 host JWT。登出/换号若不拆桥，已配对的旧手机仍会
    连着，却以新账号身份读写持仓与审批 —— 跨账号读写，不是单纯断连问题。
    """
    from cli.ipc.remote import bridge_status, stop_bridge

    if not bridge_status().get("running"):
        return
    try:
        _remote_http("revoke", "POST", {"conn_id": "*"})
    except MethodError:
        logger.info("remote revoke failed on identity change", exc_info=True)
    stop_bridge()


def _remote_ws_url() -> str:
    base = REMOTE_API_BASE.rstrip("/")
    scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[-1]
    return f"{scheme}://{host}/api/remote/ws"


def _remote_credentials() -> tuple[str, str]:
    """当前登录态里的 token 和 user_id。没登录就没法用遥控。

    直接读库层而不是 session 里那个同名包装：架构边界测试禁止跨模块 import 私有
    成员（`session._load_session`），而它本来就只是这个函数的一层壳。
    """
    from integrations.local_auth import load_session

    session = load_session() or {}
    return str(session.get("access_token") or ""), str(session.get("user_id") or "")


def _remote_http(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """调云端的遥控接口（配对、设备列表、撤销）。"""
    import httpx

    token, _ = _remote_credentials()
    if not token:
        raise MethodError("not_signed_in", "远程遥控需要先登录 —— 手机要用同一个账号连上来。")
    url = f"{REMOTE_API_BASE.rstrip('/')}/api/remote/{path}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, json=payload if payload else None
            )
    except Exception as exc:
        raise MethodError("relay_unreachable", f"连不上云端中转：{exc}") from exc
    if resp.status_code == 403:
        raise MethodError("planet_membership_required", "这个账号还没开通星球会员，暂时不能使用远程遥控。")
    if resp.status_code >= 400:
        raise MethodError("relay_error", f"云端中转返回 {resp.status_code}")
    try:
        return dict(resp.json())
    except Exception:
        return {}


def remote_status(_params: dict[str, Any]) -> Iterator[Event]:
    """遥控是否已开启、有没有手机连着。"""
    from cli.ipc.remote import bridge_status

    token, _ = _remote_credentials()
    yield _ok(**bridge_status(), signed_in=bool(token))


def remote_enable(_params: dict[str, Any]) -> Iterator[Event]:
    """开启遥控：连上云端信箱，并阻止电脑睡眠。

    锁屏/息屏不影响（实测进程与网络都照常），但**系统睡眠**会断连。所以开启期间
    由前端挂上 powerSaveBlocker —— 那是主进程的能力，这里只负责建连接。
    """
    from cli.ipc.remote import start_bridge

    token, user_id = _remote_credentials()
    if not token or not user_id:
        raise MethodError("not_signed_in", "远程遥控需要先登录 —— 手机要用同一个账号连上来。")
    import platform

    label = platform.node() or "电脑"
    start_bridge(_remote_ws_url(), token, label)
    yield _ok(enabled=True, label=label)


def remote_disable(_params: dict[str, Any]) -> Iterator[Event]:
    """关闭遥控并踢掉所有已连的手机。

    只断电脑这一端不够：配对码若还没过期，手机能再连回来。所以同时让云端作废
    配对码并断开全部远程设备。
    """
    from cli.ipc.remote import stop_bridge

    stop_bridge()
    try:
        _remote_http("revoke", "POST", {"conn_id": "*"})
    except MethodError:
        # 云端不可达时本地仍要停掉 —— 用户点了关闭就该关闭。
        logger.info("remote revoke failed while disabling", exc_info=True)
    yield _ok(enabled=False)


def remote_pair(_params: dict[str, Any]) -> Iterator[Event]:
    """要一个配对码，前端把它编成二维码。"""
    data = _remote_http("pair", "POST")
    code = str(data.get("code") or "")
    if not code:
        raise MethodError("relay_error", "云端没有返回配对码")
    # 手机扫码后打开的地址。带着 code，登录同一账号后即可配对。
    _, user_id = _remote_credentials()
    url = f"{REMOTE_API_BASE.rstrip('/')}/m/#code={code}"
    yield _ok(code=code, url=url, expires_in_ms=int(data.get("expires_in_ms") or 0))


def remote_devices(_params: dict[str, Any]) -> Iterator[Event]:
    """在线设备列表，供设置页显示与断开。"""
    yield _ok(**_remote_http("devices"))


def remote_revoke(params: dict[str, Any]) -> Iterator[Event]:
    """踢掉一台设备。传 "*" 断开全部并作废配对码。"""
    conn_id = str(params.get("conn_id") or "").strip()
    if not conn_id:
        raise MethodError("invalid_params", "缺少 conn_id")
    yield _ok(**_remote_http("revoke", "POST", {"conn_id": conn_id}))


def chat_sessions(params: dict[str, Any]) -> Iterator[Event]:
    """会话列表：置顶优先，其余按最近活动倒序。

    archived 不传 = 只看未归档（侧栏）；传 true = 只看已归档（设置页的管理区）；
    传 "all" = 两者都要。默认值刻意是「未归档」而不是「全部」——
    侧栏是最常见的调用方，让它拿到全部会导致归档了却还在原地。
    """
    from integrations.local_db import list_chat_sessions

    limit = max(1, min(int(params.get("limit") or 60), 200))
    search = str(params.get("search") or "")
    raw = params.get("archived")
    archived: bool | None = None if raw == "all" else bool(raw)
    rows = list_chat_sessions(limit=limit, user_id=_current_user_id(), search=search, archived=archived)
    yield _ok(sessions=rows, active=_active_session())


def chat_load(params: dict[str, Any]) -> Iterator[Event]:
    """切到某个会话，返回它的历史消息给前端渲染。

    历史从 chat_log 读（只有 user/assistant 文本），所以前端拿到的是一串
    简化的轮次，不含工具调用细节 —— 那些留在 scratchpad 里做取证，不重放到界面。
    """
    from integrations.local_db import load_chat_logs

    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        raise MethodError("invalid_params", "缺少 session_id")

    user_id = _current_user_id()
    rows = load_chat_logs(session_id=session_id, limit=400, user_id=user_id)
    if not rows:
        # 空结果既可能是「不存在」也可能是「不属于你」。不区分 —— 区分就等于
        # 告诉调用方「这个 id 存在但你没权限」。
        raise MethodError("not_found", "会话不存在")

    session = _synced_session(session_id)
    turns = [
        {"role": str(r.get("role") or ""), "content": str(r.get("content") or ""), "at": str(r.get("created_at") or "")}
        for r in rows
        if r.get("role") in ("user", "assistant") and r.get("content")
    ]
    yield _ok(session_id=session.session_id, turns=turns, messages=len(session._messages))


def chat_delete(params: dict[str, Any]) -> Iterator[Event]:
    from cli.ipc.session import active_session_id, drop_session, new_session
    from integrations.local_db import delete_chat_session

    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        raise MethodError("invalid_params", "缺少 session_id")

    removed = delete_chat_session(session_id, _current_user_id())
    drop_session(session_id)
    # 删掉的正是当前会话时要有个落脚处，否则下一轮对话会写进一个刚被删掉的 id。
    next_id = active_session_id() or new_session().session_id
    yield _ok(deleted=removed, session_id=next_id)


def chat_rename(params: dict[str, Any]) -> Iterator[Event]:
    from integrations.local_db import rename_chat_session

    session_id = str(params.get("session_id") or "").strip()
    title = str(params.get("title") or "").strip()
    if not session_id or not title:
        raise MethodError("invalid_params", "缺少 session_id 或 title")
    if not rename_chat_session(session_id, title, _current_user_id()):
        raise MethodError("not_found", "会话不存在")
    yield _ok(renamed=True)


def chat_archive(params: dict[str, Any]) -> Iterator[Event]:
    """归档 / 取消归档一个会话。

    归档掉的正好是当前会话时，要像 chat_delete 一样给个落脚处 —— 否则下一轮
    对话会写进一个已经从侧栏消失的会话里，用户看不见自己刚说的话。

    只在**归档**时换落脚点。取消归档不用换：那个会话本来就不是当前会话。
    """
    from cli.ipc.session import active_session_id, new_session
    from integrations.local_db import set_chat_session_archived

    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        raise MethodError("invalid_params", "缺少 session_id")
    archived = bool(params.get("archived", True))
    if not set_chat_session_archived(session_id, archived, _current_user_id()):
        raise MethodError("not_found", "会话不存在")

    next_id = active_session_id()
    if archived and session_id == next_id:
        # 不 drop_session：归档不是删除，内容还在，会话对象留着下次恢复能直接接上。
        next_id = new_session().session_id
    yield _ok(archived=archived, session_id=next_id or "")


def chat_delete_archived(_params: dict[str, Any]) -> Iterator[Event]:
    """清空已归档会话。不可逆 —— 确认在前端做。

    一次 SQL 删完，不接受 id 列表：让前端循环单删的话，中间失败就留下删一半的
    状态，而用户以为「全部删除」是原子的。

    删完给个落脚会话。当前会话理论上不该是已归档的（归档时就换过落脚点了），
    但可能被 TUI 或另一个窗口改过 —— 保持和 chat_delete 一致，别让下一轮对话
    写进一个刚被删掉的 id。
    """
    from cli.ipc.session import active_session_id, new_session
    from integrations.local_db import delete_archived_chat_sessions, list_chat_sessions

    user_id = _current_user_id()
    # 先记下哪些会被删，好判断当前会话是否在其中。
    doomed = {str(r.get("session_id") or "") for r in list_chat_sessions(limit=200, user_id=user_id, archived=True)}
    removed = delete_archived_chat_sessions(user_id)

    next_id = active_session_id()
    if not next_id or next_id in doomed:
        next_id = new_session().session_id
    yield _ok(deleted=removed, session_id=next_id)


def chat_pin(params: dict[str, Any]) -> Iterator[Event]:
    from integrations.local_db import set_chat_session_pinned

    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        raise MethodError("invalid_params", "缺少 session_id")
    pinned = bool(params.get("pinned"))
    if not set_chat_session_pinned(session_id, pinned, _current_user_id()):
        raise MethodError("not_found", "会话不存在")
    yield _ok(pinned=pinned)


METHODS: dict[str, Callable[[dict[str, Any]], Iterator[Event]]] = {
    "health": health,
    "chat": chat,
    "chat_reset": chat_reset,
    "chat_sessions": chat_sessions,
    "chat_load": chat_load,
    "chat_delete": chat_delete,
    "chat_rename": chat_rename,
    "chat_pin": chat_pin,
    "chat_archive": chat_archive,
    "chat_delete_archived": chat_delete_archived,
    "remote_status": remote_status,
    "remote_enable": remote_enable,
    "remote_disable": remote_disable,
    "remote_pair": remote_pair,
    "remote_devices": remote_devices,
    "remote_revoke": remote_revoke,
    "approve_list": approve_list,
    "approve_decide": approve_decide,
    "portfolio": portfolio,
    "portfolio_edit": portfolio_edit,
    "portfolio_set_stop": portfolio_set_stop,
    "tracking": tracking,
    "attribution": attribution,
    "attribution_dates": attribution_dates,
    "chart_data": chart_data,
    "ohlcv": ohlcv,
    "wyckoff_events": wyckoff_events,
    "schedules": schedules,
    "schedule_run": schedule_run,
    "schedule_create": schedule_create,
    "schedule_update": schedule_update,
    "schedule_toggle": schedule_toggle,
    "schedule_delete": schedule_delete,
    "mcp_list": mcp_list,
    "account": account,
    "auth_login": auth_login,
    "auth_logout": auth_logout,
    "sign_out": sign_out,
    "artifact_list": artifact_list,
    "artifact_read": artifact_read,
    "artifact_import": artifact_import,
    "model_add": model_add,
    "model_remove": model_remove,
    "model_test": model_test,
    "daemon_status": daemon_status,
    "daemon_uninstall": daemon_uninstall,
    "settings_get": settings_get,
    "settings_set": settings_set,
}


def dispatch(method: str, params: dict[str, Any] | None) -> Iterator[Event]:
    """执行方法并逐个 yield 事件。未知方法抛 MethodError。"""
    handler = METHODS.get(method)
    if handler is None:
        raise MethodError("unknown_method", f"未知方法: {method}")
    # 记录每次调用：渲染进程是否真的连上来了，只看进程状态是看不出的。
    logger.info("dispatch %s", method)
    emitted = 0
    for event in handler(params or {}):
        emitted += 1
        if emitted > MAX_EVENTS_PER_CALL:
            logger.warning("method %s exceeded event cap", method)
            yield {"type": "error", "code": "event_cap", "message": "事件数超限，已截断"}
            return
        yield event
