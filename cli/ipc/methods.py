"""IPC 方法层 — 不知道传输是 stdio 还是 HTTP。

这一层是换传输时保持不变的部分：每个方法是一个生成器，yield 结构化事件。
传输层负责把事件序列化并送出去。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
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


def portfolio(_params: dict[str, Any]) -> Iterator[Event]:
    """
    持仓视图。必须带上会话的 tool_context —— 否则 has_cloud() 恒为 False，
    已登录用户的 Supabase 持仓永远读不到，界面会静默显示本地 SQLite 缓存
    （可能是旧的，也可能是空的），看起来像「持仓丢了」。

    读取顺序由 portfolio_tools 决定：有 token 就先读 Supabase 并回写本地缓存，
    没有 token（或云端读失败）才落到本地 SQLite。
    """
    from agents.portfolio_tools import portfolio as portfolio_tool
    from cli.ipc.session import get_session

    yield _ok(portfolio=portfolio_tool(mode="view", tool_context=get_session().tool_context))


# 桌面端允许的持仓写入动作。
#
# 刻意不含 delete_records：它删的是推荐跟踪表而不是持仓，而且在算出
# portfolio_id 之前就 return 了——压根没有用户隔离。放进持仓编辑入口是错的。
_PORTFOLIO_ACTIONS = frozenset({"add", "update", "remove", "set_cash"})


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
    from cli.ipc.session import get_session

    action = str(params.get("action") or "").strip().lower()
    if action not in _PORTFOLIO_ACTIONS:
        raise MethodError("invalid_params", f"不支持的持仓操作: {action or '(空)'}")

    result = update_portfolio(
        action=action,
        code=str(params.get("code") or ""),
        name=str(params.get("name") or ""),
        shares=int(params.get("shares") or 0),
        cost_price=float(params.get("cost_price") or 0),
        buy_dt=str(params.get("buy_dt") or ""),
        free_cash=float(params.get("free_cash") or 0),
        tool_context=get_session().tool_context,
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
    from cli.ipc.session import get_session

    code = str(params.get("code") or "")
    if not code:
        raise MethodError("invalid_params", "需要 code")
    if "stop_loss" not in params:
        raise MethodError("invalid_params", "需要 stop_loss（传 null 表示清除）")

    raw = params.get("stop_loss")
    result = set_stop_loss(
        code=code,
        stop_loss=None if raw is None else float(raw),
        tool_context=get_session().tool_context,
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
    from cli.ipc.session import get_session

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
        tool_context=get_session().tool_context,
        market=market,
    )
    if "error" in result:
        raise MethodError("tracking_failed", str(result["error"]))
    yield _ok(**result)


def attribution(params: dict[str, Any]) -> Iterator[Event]:
    """
    策略归因报告。全局数据（按 market 过滤，不分用户），但仍传上下文 ——
    有登录态时用用户客户端读，没有才退到匿名客户端。

    上限 10：这是按天累积的报告，翻更多没有意义，界面也只展示最新一份加历史列表。
    """
    from agents.history_tools import query_history
    from cli.ipc.session import get_session

    limit = _clamp_int(params.get("limit"), 5, 1, 10)
    result = query_history(source="attribution", limit=limit, tool_context=get_session().tool_context)
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
    from cli.daemon import is_daemon_running
    from cli.scheduler import load_schedules, schedule_status

    yield _ok(
        schedules=schedule_status(load_schedules()),
        daemon_running=is_daemon_running(),
    )


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
    """当前登录态。绝不返回 token 或密码，只返回身份标识。"""
    from integrations.local_auth import load_session

    session = load_session() or {}
    email = str(session.get("email") or "")
    yield _ok(
        signed_in=bool(session.get("access_token")),
        email=email,
        user_id=str(session.get("user_id") or ""),
    )


def artifact_list(_params: dict[str, Any]) -> Iterator[Event]:
    """列出报告目录里可预览的产物。"""
    from cli.ipc.artifacts import list_artifacts

    items = [
        {
            "name": a.name,
            "rel_path": a.rel_path,
            "kind": a.kind,
            "size": a.size,
            "modified_at": a.modified_at,
        }
        for a in list_artifacts()
    ]
    yield _ok(items=items, count=len(items))


def artifact_read(params: dict[str, Any]) -> Iterator[Event]:
    """读取单个产物内容供容器渲染。"""
    from cli.ipc.artifacts import ArtifactError, read_artifact

    try:
        yield _ok(**read_artifact(str(params.get("path") or "")))
    except ArtifactError as exc:
        raise MethodError(exc.code, str(exc)) from exc


def artifact_import(params: dict[str, Any]) -> Iterator[Event]:
    """把用户拖进来的文件复制到报告目录。"""
    from cli.ipc.artifacts import ArtifactError, import_file

    try:
        artifact = import_file(str(params.get("source") or ""))
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

    if not model_id:
        raise MethodError("invalid_params", "缺少模型标识")
    if provider_name not in PROVIDERS:
        raise MethodError("invalid_params", f"未知 provider: {provider_name}（可选: {', '.join(PROVIDERS)}）")
    if not model:
        raise MethodError("invalid_params", "缺少模型名")
    if not api_key:
        raise MethodError("invalid_params", "缺少 API Key")

    save_model_entry(
        {
            "id": model_id,
            "provider_name": provider_name,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
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

    from cli.provider_factory import create_provider
    from integrations.local_auth import load_model_configs

    model_id = str(params.get("id") or "").strip()
    entry = next((m for m in load_model_configs() if m.get("id") == model_id), None)
    if entry is None:
        raise MethodError("not_found", f"找不到模型: {model_id}")

    yield {"type": "progress", "message": f"正在连接 {model_id}…"}

    provider, error = create_provider(
        entry.get("provider_name", ""),
        entry.get("api_key", ""),
        entry.get("model", ""),
        entry.get("base_url", ""),
    )
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
    return value


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

    from cli.ipc.session import get_session

    session = get_session()
    yield from session.run_turn(text)


METHODS: dict[str, Callable[[dict[str, Any]], Iterator[Event]]] = {
    "health": health,
    "chat": chat,
    "approve_list": approve_list,
    "approve_decide": approve_decide,
    "portfolio": portfolio,
    "portfolio_edit": portfolio_edit,
    "portfolio_set_stop": portfolio_set_stop,
    "tracking": tracking,
    "attribution": attribution,
    "chart_data": chart_data,
    "ohlcv": ohlcv,
    "wyckoff_events": wyckoff_events,
    "schedules": schedules,
    "schedule_run": schedule_run,
    "mcp_list": mcp_list,
    "account": account,
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
