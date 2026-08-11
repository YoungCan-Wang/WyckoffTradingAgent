"""
威科夫终端读盘室 — 入口。

用法:
    wyckoff                         # 启动 TUI
    wyckoff update                  # 升级到最新版
    wyckoff screen                  # 全市场漏斗筛选
    wyckoff backtest                # 策略历史回测
    wyckoff report 000001,600519    # AI 深度研报
    wyckoff mcp                     # 启动 MCP Server
    wyckoff memory                  # 查看 Agent 记忆
    wyckoff log                     # 查看对话日志
    wyckoff session                 # 会话列表 / 导出 / 分叉
    wyckoff trace                   # 查看 JSONL 运行轨迹
    wyckoff prompt                  # 查看/渲染 Prompt 模板
    wyckoff diag                    # 导出会话诊断包
    wyckoff dashboard               # 启动本地可视化面板
    wyckoff auth <email>            # 登录
    wyckoff model list/add/rm       # 模型管理
    wyckoff config                  # 数据源配置
    wyckoff portfolio list/add/rm   # 持仓管理
    wyckoff portfolio fill          # 回填真实成交
    wyckoff signal                  # 信号确认池
    wyckoff recommend               # 威科夫形态复盘
    wyckoff sync                    # 同步 Supabase → SQLite
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT in sys.path:
    sys.path.remove(_PACKAGE_ROOT)
sys.path.insert(0, _PACKAGE_ROOT)

import logging as _logging

logger = _logging.getLogger(__name__)

# CLI 环境：只显示 CRITICAL，不泄漏 traceback 给用户
import warnings as _warnings

_warnings.filterwarnings("ignore", category=DeprecationWarning)
_warnings.filterwarnings("ignore", category=ResourceWarning)
_logging.basicConfig(level=_logging.CRITICAL)


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------


from cli.provider_factory import create_provider, provider_config_kwargs  # noqa: F401


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("youngcan-wyckoff-analysis")
    except Exception:
        return "dev"


def _version_sort_key(value: str) -> tuple[int, ...]:
    """把版本号转成可比较的整数元组，无法解析时返回空元组。

    原先直接对每一段做 int()，PyPI 一旦出现 1.0.0rc1 / 0.9.235.post1 这类非纯数字版本就抛
    ValueError，被外层 except 吞掉——升级提示从此静默失效，且正好在最需要提示的大版本上失效。
    """
    parts: list[int] = []
    for chunk in str(value).strip().split("."):
        match = re.match(r"^\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def _check_update_async() -> None:
    """后台检查 PyPI 最新版本，有新版则打印提示。"""
    import threading

    def _check():
        try:
            import urllib.request

            local_ver = _get_version()
            if local_ver == "dev":
                return
            url = "https://pypi.org/pypi/youngcan-wyckoff-analysis/json"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            latest = data.get("info", {}).get("version", "")
            if not latest or latest == local_ver:
                return
            local_parts = _version_sort_key(local_ver)
            latest_parts = _version_sort_key(latest)
            if not local_parts or not latest_parts:
                return
            if latest_parts > local_parts:
                print(f"\033[33m⬆ 新版本可用: {latest}（当前 {local_ver}），运行 wyckoff update 升级\033[0m")
        except Exception:
            logger.debug("version check failed", exc_info=True)

    threading.Thread(target=_check, daemon=True).start()


def _mask(val: str) -> str:
    if len(val) > 8:
        return val[:4] + "****" + val[-4:]
    return "****" if val else ""


def _set_terminal_title(title: str) -> None:
    """Set the terminal tab title only for interactive terminals."""

    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        logger.debug("set terminal title failed", exc_info=True)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def _cmd_update(_args):
    import shutil

    print("正在升级 youngcan-wyckoff-analysis ...")
    pkg = "youngcan-wyckoff-analysis"
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, "--upgrade", pkg]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", pkg]
    try:
        subprocess.check_call(cmd)
        url = "https://wyckoff-analysis.pages.dev/"
        try:
            subprocess.run(["pbcopy"], input=url.encode(), check=True)
        except FileNotFoundError:
            try:
                subprocess.run(["xclip", "-selection", "clipboard"], input=url.encode(), check=True)
            except FileNotFoundError:
                logger.debug("no clipboard tool available", exc_info=True)
        print(f"\n✓ 升级完成！请重新运行 wyckoff。\n  Web 版已复制到剪切板: {url}")
    except subprocess.CalledProcessError as e:
        print(f"\n✗ 升级失败: {e}")
        sys.exit(1)


def _cmd_auth(args):
    from cli.auth import load_session, login, logout, restore_session

    sub = args.auth_cmd

    if sub == "logout":
        logout()
        print("✓ 已退出登录")
        return

    if sub == "status":
        session = load_session()
        if not session:
            print("未登录")
            return
        restored = restore_session()
        if restored:
            print(f"✓ 已登录: {restored['email']}")
            print(f"  user_id: {restored['user_id']}")
        else:
            print("⚠ 登录已过期，请重新登录")
        return

    # wyckoff auth <email> <password>
    email = sub
    password = args.password
    if not password:
        import getpass

        password = getpass.getpass("密码: ")
    try:
        session = login(email, password)
        print(f"✓ 登录成功: {session['email']}")
        print(f"  user_id: {session['user_id']}")
    except Exception as e:
        err = str(e)
        if "Invalid login" in err or "invalid" in err.lower():
            print("✗ 邮箱或密码错误")
        else:
            print(f"✗ 登录失败: {err}")
        sys.exit(1)


def _model_list():
    from cli.auth import load_default_model_id, load_fallback_model_id, load_model_configs
    from cli.model_registry import format_model_metadata, infer_model_info

    configs = load_model_configs()
    default_id = load_default_model_id()
    fallback_id = load_fallback_model_id()
    if not configs:
        print("尚无模型配置，使用 wyckoff model add 添加")
        return
    for c in configs:
        marks = ""
        if c["id"] == default_id:
            marks += " *"
        if c["id"] == fallback_id:
            marks += " ⚡"
        metadata = format_model_metadata(infer_model_info(c))
        print(
            f"  {c['id']}{marks}  provider={c.get('provider_name', '')}  "
            f"model={c.get('model', '')}  {metadata}  base_url={c.get('base_url', '') or '(default)'}"
        )
    legend = "  * = 默认"
    if fallback_id:
        legend += "  ⚡ = fallback"
    print(f"\n{legend}")


def _model_add():
    from cli.auth import load_model_configs, save_model_entry, set_default_model

    model_id = input("别名 (如 gemini, longcat): ").strip().lower()
    if not model_id:
        print("已取消")
        return
    provider = input("供应商 (gemini/openai/claude): ").strip().lower()
    if provider not in ("gemini", "openai", "claude"):
        print(f"✗ 不支持: {provider}")
        sys.exit(1)
    import getpass

    api_key = getpass.getpass("API Key (购买: https://www.1route.dev/register?aff=359904261): ").strip()
    if not api_key:
        print("已取消")
        return
    default_models = {"gemini": "gemini-2.0-flash", "openai": "gpt-4o", "claude": "claude-sonnet-4-20250514"}
    model = input(f"模型名 (留空使用 {default_models.get(provider, '')}): ").strip()
    model = model or default_models.get(provider, "")
    base_url = input("Base URL (留空使用默认): ").strip()
    entry = {"id": model_id, "provider_name": provider, "api_key": api_key, "model": model, "base_url": base_url}
    save_model_entry(entry)
    if len(load_model_configs()) == 1:
        set_default_model(model_id)
    print(f"✓ 模型 {model_id} 已保存")


def _model_fallback_set(args):
    """设置 fallback 模型。"""
    from cli.auth import (
        load_fallback_model_id,
        load_model_configs,
        set_fallback_model,
    )

    label, empty_msg = "fallback", "未设置（降级到所有模型）"
    model_id = args.model_id
    if not model_id:
        print(f"当前 {label}: {load_fallback_model_id() or empty_msg}")
        return
    if model_id == "none":
        set_fallback_model("")
        print(f"✓ 已清除 {label} 设置")
        return
    configs = load_model_configs()
    if not any(c["id"] == model_id for c in configs):
        print(f"✗ 模型 {model_id} 不存在")
        sys.exit(1)
    set_fallback_model(model_id)
    print(f"✓ {label.capitalize()} 模型已设为 {model_id}")


def _cmd_model(args):
    from cli.auth import load_model_configs, remove_model_entry, save_model_entry, set_default_model

    sub = args.model_cmd or "list"

    if sub == "list":
        return _model_list()
    if sub == "add":
        return _model_add()
    if sub == "set":
        model_id, provider, api_key = args.model_id, args.provider, args.api_key
        if not all([model_id, provider, api_key]):
            print("用法: wyckoff model set <id> <provider> <api_key> [--model X] [--base-url X]")
            sys.exit(1)
        save_model_entry(
            {
                "id": model_id,
                "provider_name": provider,
                "api_key": api_key,
                "model": args.model_name or "",
                "base_url": args.base_url or "",
            }
        )
        print(f"✓ 模型 {model_id} 已保存")
        return
    if sub == "rm":
        if not args.model_id:
            print("用法: wyckoff model rm <id>")
            sys.exit(1)
        print(f"✓ 模型 {args.model_id} 已删除" if remove_model_entry(args.model_id) else "✗ 至少保留一个模型")
        return
    if sub == "default":
        if not args.model_id:
            print("用法: wyckoff model default <id>")
            sys.exit(1)
        configs = load_model_configs()
        if not any(c["id"] == args.model_id for c in configs):
            print(f"✗ 模型 {args.model_id} 不存在")
            sys.exit(1)
        set_default_model(args.model_id)
        print(f"✓ 默认模型已切换为 {args.model_id}")
        return
    if sub == "fallback":
        return _model_fallback_set(args)
    if sub == "cost":
        if not args.model_id:
            print("用法: wyckoff model cost <id> --input-per-1m N --output-per-1m N [--context-window N]")
            sys.exit(1)
        configs = load_model_configs()
        cfg = next((c for c in configs if c["id"] == args.model_id), None)
        if not cfg:
            print(f"✗ 模型 {args.model_id} 不存在")
            sys.exit(1)
        updated = dict(cfg)
        if args.input_per_1m is not None:
            updated["input_cost_per_1m"] = args.input_per_1m
        if args.output_per_1m is not None:
            updated["output_cost_per_1m"] = args.output_per_1m
        if args.context_window is not None:
            updated["context_window"] = args.context_window
        save_model_entry(updated)
        print(f"✓ 模型 {args.model_id} 的成本/上下文元数据已保存")
        return
    if sub == "usage":
        from cli.model_registry import summarize_model_usage

        rows = summarize_model_usage(days=args.days, configs=load_model_configs())
        if not rows:
            print(f"最近 {args.days} 天暂无模型用量记录")
            return
        print(f"最近 {args.days} 天模型用量")
        print(f"{'provider':<12} {'model':<28} {'req':>4} {'in':>10} {'out':>10} {'cost':>12}")
        print("-" * 82)
        total_cost = 0.0
        total_known = False
        for row in rows:
            cost_s = "-"
            if row.estimated_cost is not None:
                total_known = True
                total_cost += row.estimated_cost
                cost_s = f"${row.estimated_cost:.4f}"
            print(
                f"{row.provider[:12]:<12} {row.model[:28]:<28} {row.requests:>4} "
                f"{row.tokens_in:>10,} {row.tokens_out:>10,} {cost_s:>12}"
            )
        if total_known:
            print(f"\n估算合计: ${total_cost:.4f}")
        else:
            print("\n成本未知：用 wyckoff model cost <id> --input-per-1m N --output-per-1m N 配置")
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff model [list|add|set|rm|default|fallback|cost|usage]")
    sys.exit(1)


def _cmd_config(args):
    from cli.auth import (
        get_stream_chunk_timeout_seconds,
        get_tool_timeout_seconds,
        load_config,
        save_config_key,
    )

    CONFIG_KEYS = {
        "tushare": ("tushare_token", "Tushare Token", "TUSHARE_TOKEN", "secret"),
        "tickflow": ("tickflow_api_key", "TickFlow API Key", "TICKFLOW_API_KEY", "secret"),
        "stream_timeout": ("stream_chunk_timeout_seconds", "模型空闲/首token超时(秒)", "", "timeout"),
        "tool_timeout": ("tool_timeout_seconds", "工具执行超时(秒)", "", "timeout"),
    }

    sub = args.config_cmd

    if not sub:
        cfg = load_config()
        print("本地配置 (~/.wyckoff/wyckoff.json)")
        print()
        for _alias, (key, label, _, kind) in CONFIG_KEYS.items():
            if kind == "timeout":
                current = (
                    get_stream_chunk_timeout_seconds(cfg)
                    if key == "stream_chunk_timeout_seconds"
                    else get_tool_timeout_seconds(cfg)
                )
                mark = "自定义" if key in cfg else "默认"
                print(f"  {label}: \033[32m{int(current)}\033[0m ({mark})")
                continue
            val = str(cfg.get(key, "") or "").strip()
            status = f"\033[32m{_mask(val)}\033[0m" if val else "\033[90m未配置\033[0m"
            print(f"  {label}: {status}")
        print()
        print("用法: wyckoff config tushare|tickflow|stream_timeout|tool_timeout <value>")
        return

    if sub not in CONFIG_KEYS:
        print(f"✗ 未知配置项: {sub}")
        print(f"可选: {', '.join(CONFIG_KEYS)}")
        sys.exit(1)

    key, label, env_key, kind = CONFIG_KEYS[sub]
    value = args.value
    if not value:
        import getpass

        prompt = f"{label}: "
        value = getpass.getpass(prompt).strip() if kind == "secret" else input(prompt).strip()
    if not value:
        print("已取消")
        return
    try:
        save_config_key(key, value)
    except ValueError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    if kind == "secret" and env_key:
        os.environ[env_key] = value
    print(f"✓ {label} 已保存")


# ---------------------------------------------------------------------------
# 持仓 helpers
# ---------------------------------------------------------------------------


def _get_session_client():
    """从 session.json 获取 user client，返回 (client, user_id, portfolio_id) 或退出。"""
    from cli.auth import restore_session

    session = restore_session()
    if not session or not session.get("access_token"):
        print("✗ 未登录，请先执行 wyckoff auth <email> <password>")
        sys.exit(1)
    from integrations.supabase_base import create_user_client
    from integrations.supabase_portfolio import build_user_live_portfolio_id

    client = create_user_client(session["access_token"], session["refresh_token"])
    uid = session["user_id"]
    pid = build_user_live_portfolio_id(uid)
    return client, uid, pid


def _cmd_portfolio(args):
    sub = args.portfolio_cmd or "list"

    if sub == "list":
        client, uid, pid = _get_session_client()
        from integrations.supabase_portfolio import load_portfolio_state

        state = load_portfolio_state(pid, client=client)
        if not state or not state.get("positions"):
            print("暂无持仓记录")
            if state:
                print(f"可用资金: {state.get('free_cash', 0):,.2f}")
                if state.get("total_equity") is not None:
                    print(f"最新总权益: {state['total_equity']:,.2f}")
            return
        print(f"持仓 ({len(state['positions'])} 只)  可用资金: {state.get('free_cash', 0):,.2f}")
        if state.get("total_equity") is not None:
            print(f"最新总权益: {state['total_equity']:,.2f}  估值时间: {state.get('updated_at', '-')}")
        print(f"{'代码':<8} {'名称':<10} {'股数':>6} {'成本':>8} {'买入日期':<10} {'止损':>8}")
        print("-" * 60)
        for p in state["positions"]:
            sl = f"{p.get('stop_loss', 0):.2f}" if p.get("stop_loss") else "-"
            print(
                f"{p['code']:<8} {p.get('name', ''):<10} {p.get('shares', 0):>6} {p.get('cost', 0):>8.3f} {p.get('buy_dt', ''):<10} {sl:>8}"
            )
        return

    if sub == "add":
        client, uid, pid = _get_session_client()
        code = args.code
        if not code:
            print("用法: wyckoff portfolio add <code> --name X --shares N --cost N [--buy-dt YYYYMMDD]")
            sys.exit(1)
        from integrations.supabase_portfolio import upsert_position

        position = {
            "code": code,
            "name": args.name or "",
            "shares": args.shares or 0,
            "cost_price": args.cost or 0,
            "buy_dt": args.buy_dt or "",
        }
        ok, msg = upsert_position(pid, position, client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    if sub == "rm":
        client, uid, pid = _get_session_client()
        code = args.code
        if not code:
            print("用法: wyckoff portfolio rm <code>")
            sys.exit(1)
        from integrations.supabase_portfolio import delete_position

        ok, msg = delete_position(pid, code, client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    if sub == "cash":
        client, uid, pid = _get_session_client()
        amount = args.amount
        if amount is None:
            # 查看
            from integrations.supabase_portfolio import load_portfolio_state

            state = load_portfolio_state(pid, client=client)
            print(f"可用资金: {state.get('free_cash', 0):,.2f}" if state else "暂无记录")
            return
        from integrations.supabase_portfolio import update_free_cash

        ok, msg = update_free_cash(pid, float(amount), client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    if sub == "fill":
        _cmd_portfolio_fill(args)
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff portfolio [list|add|rm|cash|fill]")
    sys.exit(1)


def _cmd_portfolio_fill(args):
    """回填一笔真实成交：按增量更新持仓与现金，而不是覆盖快照。"""
    from datetime import datetime

    from core.trade_fill import Fill
    from integrations.supabase_portfolio import record_fill
    from utils.trading_clock import CN_TZ

    if not args.code or not args.side or not args.shares or not args.price:
        print("用法: wyckoff portfolio fill <code> --side buy|sell --shares N --price P [--date YYYYMMDD] [--name X]")
        sys.exit(1)
    client, _uid, pid = _get_session_client()
    try:
        fill = Fill(
            code=args.code,
            side=str(args.side).lower(),
            shares=int(args.shares),
            price=float(args.price),
            trade_date=args.date or datetime.now(CN_TZ).strftime("%Y%m%d"),
            name=args.name or "",
        )
    except ValueError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    result = record_fill(pid, fill, client=client)
    print(f"{'✓' if result.ok else '✗'} {result.message}")
    if not result.ok:
        sys.exit(1)


def _cmd_signal(args):
    status = args.status or "all"
    limit = args.limit or 30
    from agents.history_tools import query_history

    result = query_history(source="signal", status=status, limit=limit)
    if result.get("error"):
        print(f"✗ {result['error']}")
        sys.exit(1)
    records = result.get("records", [])
    if not records:
        print(result.get("message", "暂无信号记录"))
        return
    counts = result.get("status_counts", {})
    print(f"信号确认池 ({result.get('total', 0)} 条)  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))
    print(f"{'代码':<8} {'名称':<8} {'信号':<6} {'状态':<10} {'日期':<10} {'天数':>4} {'评分':>6} {'行业':<10}")
    print("-" * 75)
    for r in records:
        score = r.get("signal_score", 0)
        score_s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(
            f"{r['code']:<8} {r['name']:<8} {r['signal_type']:<6} {r['status']:<10} "
            f"{r['signal_date']:<10} {r.get('days_elapsed', 0):>4} {score_s:>6} "
            f"{r.get('industry', ''):<10}"
        )


def _cmd_recommend(args):
    limit = args.limit or 20
    from agents.history_tools import query_history

    result = query_history(source="recommendation", limit=limit)
    if result.get("error"):
        print(f"✗ {result['error']}")
        sys.exit(1)
    records = result.get("records", [])
    if not records:
        print(result.get("message", "暂无复盘记录"))
        return
    print(f"威科夫形态复盘 ({result.get('total', 0)} 条)")
    print(
        f"{'代码':<8} {'名称':<8} {'阵营':<8} {'推荐日':<10} {'推荐价':>8} {'现价':>8} {'盈亏%':>7} {'最高%':>7} {'状态':<8}"
    )
    print("-" * 85)
    for r in records:
        code = str(r.get("code", "")).strip().zfill(6)
        rec_p = f"{r['recommend_price']:.2f}" if r.get("recommend_price") else "-"
        cur_p = f"{r['current_price']:.2f}" if r.get("current_price") else "-"
        pnl = f"{r['pnl_pct']:.1f}" if r.get("pnl_pct") is not None else "-"
        max_pnl = f"{r['max_pnl_pct']:.1f}" if r.get("max_pnl_pct") is not None else "-"
        print(
            f"{code:<8} {r['name']:<8} {r.get('camp', ''):<8} {r['recommend_date']:<10} "
            f"{rec_p:>8} {cur_p:>8} {pnl:>7} {max_pnl:>7} {r.get('status', ''):<8}"
        )


# ---------------------------------------------------------------------------
# wyckoff screen — 漏斗筛选
# ---------------------------------------------------------------------------


def _cmd_screen(args):
    from integrations.local_db import init_db
    from utils.tool_result_preview import tool_result_brief_lines

    init_db()
    board = args.board or "all"
    limit_note = "全量" if args.limit == 0 else f"前{args.limit}只" if args.limit is not None else "默认范围"
    style_note = f", style={args.style}" if args.style else ""
    print(f"正在执行全市场漏斗筛选 (board={board}, {limit_note}{style_note}) ...")
    from agents.screen_tools import screen_stocks

    result = screen_stocks(
        board=board,
        limit=args.limit,
        style=args.style or None,
        financial_metrics=args.financial_metrics,
    )
    if result.get("error"):
        print(f"✗ 筛选失败: {result['error']}")
        sys.exit(1)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    total_scanned = int(summary.get("total_scanned", 0) or 0)
    report_count = int(summary.get("report_candidates", 0) or 0)
    watch_count = len(result.get("watch_candidates") or [])
    print(f"\n✓ 筛选完成  扫描 {total_scanned} 只，研报候选 {report_count} 只，观察 {watch_count} 只")
    lines = tool_result_brief_lines("screen_stocks", result, max_lines=8)
    for line in lines:
        print(f"  {line}")
    if not lines:
        payload = {"scan_scope": result.get("scan_scope"), "summary": summary}
        print(f"  {json.dumps(payload, ensure_ascii=False, default=str)}")


# ---------------------------------------------------------------------------
# wyckoff backtest — 策略回测
# ---------------------------------------------------------------------------


def _cmd_backtest(args):
    from datetime import date, timedelta

    from integrations.local_db import init_db

    init_db()

    end_dt = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=args.months * 30)
    print(f"正在回测 {start_dt} → {end_dt}  hold_days={args.hold_days} ...")
    from workflows.backtest import BacktestWorkflowRequest, run_backtest_request

    try:
        df, summary = run_backtest_request(
            BacktestWorkflowRequest(
                start_dt=start_dt,
                end_dt=end_dt,
                hold_days=args.hold_days,
                top_n=args.top_n,
                board="all",
                sample_size=0,
                trading_days=60,
                max_workers=4,
                cash_portfolio=True,
                portfolio_styles="confirmation_only",
            )
        )
    except Exception as e:
        print(f"✗ 回测失败: {e}")
        sys.exit(1)
    print(f"\n✓ 回测完成  交易 {len(df)} 笔")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# wyckoff report — AI 研报
# ---------------------------------------------------------------------------


def _cmd_report(args):
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        print("用法: wyckoff report 000001,600519")
        sys.exit(1)
    from integrations.local_db import init_db

    init_db()
    # 需要 LLM
    from cli.auth import load_default_model_id, load_model_configs

    configs = load_model_configs()
    default_id = load_default_model_id()
    if not configs:
        print("✗ 未配置模型，请先 wyckoff model add")
        sys.exit(1)
    cfg = next((c for c in configs if c["id"] == default_id), configs[0])
    env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    for mc in configs:
        ek = env_map.get(mc.get("provider_name", ""))
        if ek and mc.get("api_key"):
            os.environ.setdefault(ek, mc["api_key"])

    from agents.stock_data_helpers import code_to_name

    symbols_info = [
        {"code": c, "name": code_to_name(c), "tag": "", "selection_source": "explicit_report_input"} for c in codes
    ]
    print(f"正在生成 AI 研报 ({len(codes)} 只) ...")
    from workflows.step3_batch_report import run as run_report

    try:
        result = run_report(
            symbols_info=symbols_info,
            webhook_url="",
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            notify=False,
            provider=cfg.get("provider_name", "gemini"),
            llm_base_url=cfg.get("base_url", ""),
        )
        ok, reason, report_text = _report_command_result(result)
        if not ok:
            print(f"✗ 研报生成失败: {reason or 'unknown'}")
            sys.exit(1)
        print("✓ 研报生成完成")
        if report_text:
            print("\n--- 研报正文 ---")
            print(_clip_report_output(report_text))
    except Exception as e:
        print(f"✗ 研报生成失败: {e}")
        sys.exit(1)


def _report_command_result(result) -> tuple[bool, str, str]:
    if isinstance(result, tuple) and len(result) >= 3:
        return bool(result[0]), str(result[1] or ""), str(result[2] or "").strip()
    if isinstance(result, dict):
        return True, "", _legacy_report_dict_text(result)
    return bool(result), "", ""


def _legacy_report_dict_text(result: dict) -> str:
    lines: list[str] = []
    for camp, stocks in result.items():
        if not stocks:
            continue
        lines.append(f"[{camp}]")
        lines.extend(f"- {stock}" for stock in stocks[:5])
    return "\n".join(lines).strip()


def _clip_report_output(text: str, limit: int = 6000) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n\n[研报正文已截断，可在 agent 对话里继续要求展开]"


# ---------------------------------------------------------------------------
# wyckoff mcp — 启动 MCP Server
# ---------------------------------------------------------------------------


def _cmd_mcp(_args):
    print("启动 Wyckoff MCP Server ...")
    print("按 Ctrl+C 停止\n")
    from mcp_server import main as mcp_main

    try:
        mcp_main()
    except KeyboardInterrupt:
        print("\nMCP Server 已停止")


# ---------------------------------------------------------------------------
# wyckoff memory — 查看/清除 Agent 记忆
# ---------------------------------------------------------------------------


def _memory_id_arg(args) -> str:
    return args.memory_id or args.keyword


def _memory_since_arg(value: str) -> str:
    if not value:
        return ""
    from datetime import datetime, timedelta

    raw = value.strip()
    unit = raw[-1:].lower()
    if unit in {"d", "h", "m"} and raw[:-1].isdigit():
        amount = int(raw[:-1])
        delta = {"d": timedelta(days=amount), "h": timedelta(hours=amount), "m": timedelta(minutes=amount)}[unit]
        return (datetime.utcnow() - delta).isoformat()
    return raw


def _memory_level_arg(value: str) -> str:
    level = value.strip().upper()
    return level if level in {"L1", "L2", "L3"} else ""


def _print_memory_rows(memories: list[dict], *, output_format: str = "table") -> None:
    if output_format == "json":
        print(json.dumps(memories, ensure_ascii=False, indent=2, default=str))
        return
    if output_format == "jsonl":
        for row in memories:
            print(json.dumps(row, ensure_ascii=False, default=str))
        return
    _print_memory_table(memories)


def _print_memory_table(memories: list[dict]) -> None:
    print(f"Agent 记忆 ({len(memories)} 条)")
    print(f"{'ID':>5} {'类型':<12} {'层级':<4} {'日期':<20} {'内容'}")
    print("-" * 80)
    for m in memories:
        content = m.get("content", "")
        if len(content) > 60:
            content = content[:60] + "..."
        level = m.get("memory_level", "")
        print(f"{m['id']:>5} {m.get('memory_type', ''):<12} {level:<4} {m.get('created_at', '')[:19]:<20} {content}")


def _delete_memory_by_id(memory_id: str) -> None:
    from integrations.local_db import get_db

    conn = get_db()
    with conn:
        cur = conn.execute("DELETE FROM agent_memory WHERE id=?", (memory_id,))
    if cur.rowcount:
        print(f"✓ 已删除记忆 #{memory_id}")
    else:
        print(f"✗ 记忆 #{memory_id} 不存在")


def _cmd_memory_list(args, level: str, since: str) -> None:
    from integrations.local_db import get_recent_memories

    mtype = args.type or None
    memories = get_recent_memories(memory_type=mtype, memory_level=level, since=since, limit=args.limit)
    if not memories:
        print("暂无记忆记录")
        return
    _print_memory_rows(memories, output_format=args.format)


def _cmd_memory_search(args, level: str, since: str) -> None:
    keyword = args.keyword
    if not keyword:
        print("用法: wyckoff memory search <关键词>")
        sys.exit(1)
    from integrations.local_db import search_memory

    results = search_memory(keyword=keyword, memory_level=level, since=since, limit=args.limit)
    if not results:
        print(f"未找到包含 '{keyword}' 的记忆")
        return
    if args.format != "table":
        _print_memory_rows(results, output_format=args.format)
        return
    for m in results:
        content = m.get("content", "")
        if len(content) > 80:
            content = content[:80] + "..."
        print(f"  [{m['id']}] {m.get('memory_type', '')} | {content}")


def _cmd_memory(args):
    from integrations.local_db import init_db, prune_memories

    init_db()
    sub = args.memory_cmd or "list"
    level = _memory_level_arg(args.level)
    since = _memory_since_arg(args.since)

    if sub == "list":
        _cmd_memory_list(args, level, since)
        return

    if sub == "search":
        _cmd_memory_search(args, level, since)
        return

    if sub == "clear":
        count = prune_memories(keep_days=0)
        print(f"✓ 已清除 {count} 条过期记忆（preference 类型保留）")
        return

    if sub == "delete":
        mid = _memory_id_arg(args)
        if not mid:
            print("用法: wyckoff memory delete <id>")
            sys.exit(1)
        _delete_memory_by_id(mid)
        return

    if sub == "trace":
        mid = _memory_id_arg(args)
        if not mid:
            print("用法: wyckoff memory trace <id>")
            sys.exit(1)
        try:
            _print_memory_trace(int(mid))
        except ValueError:
            print(f"✗ 无效记忆 ID: {mid}")
            sys.exit(1)
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff memory [list|search|clear|delete|trace]")
    sys.exit(1)


def _print_memory_trace(memory_id: int) -> None:
    from integrations.local_db import get_memory_by_id, load_chat_logs

    memory = get_memory_by_id(memory_id)
    if not memory:
        print(f"✗ 记忆 #{memory_id} 不存在")
        return
    print(f"记忆 #{memory_id}")
    print(f"类型: {memory.get('memory_type', '')} / {memory.get('memory_level', '')}")
    print(f"内容: {memory.get('content', '')}")
    source_ref = str(memory.get("source_ref", "") or "")
    print(f"来源: {source_ref or '(无)'}")
    if not source_ref.startswith("chat_log:"):
        return
    rows = load_chat_logs(session_id=source_ref.removeprefix("chat_log:"), limit=8)
    for row in rows:
        content = str(row.get("content", "") or "").replace("\n", " ")
        if len(content) > 100:
            content = content[:100] + "..."
        print(f"- {row.get('created_at', '')[:19]} {row.get('role', '')}: {content}")


def _add_memory_parser(sub) -> None:
    p_mem = sub.add_parser("memory", help="Agent 记忆管理", aliases=["mem"])
    p_mem.add_argument("memory_cmd", nargs="?", default="list", help="list/search/clear/delete/trace")
    p_mem.add_argument("keyword", nargs="?", default="", help="搜索关键词 (search 时)")
    p_mem.add_argument("memory_id", nargs="?", default="", help="记忆 ID (delete 时)")
    p_mem.add_argument(
        "--type", default="", help="过滤类型 (preference/playbook/persona/scenario/decision/market_view)"
    )
    p_mem.add_argument("--level", default="", help="过滤层级 (L1/L2/L3)")
    p_mem.add_argument("--since", default="", help="仅显示该时间之后的记忆，支持 ISO 或 7d/24h/30m")
    p_mem.add_argument("--format", choices=["table", "json", "jsonl"], default="table", help="输出格式")
    p_mem.add_argument("-n", "--limit", type=int, default=20, help="返回条数")


# ---------------------------------------------------------------------------
# wyckoff log — 查看对话日志
# ---------------------------------------------------------------------------


def _cmd_log(args):
    from integrations.local_db import init_db, load_chat_logs

    init_db()
    session_id = args.session or None
    limit = args.limit
    logs = load_chat_logs(session_id=session_id, limit=limit)
    if not logs:
        print("暂无对话日志")
        return
    if session_id:
        print(f"会话 {session_id} 的对话日志 ({len(logs)} 条)")
    else:
        print(f"最近对话日志 ({len(logs)} 条)")
    print()
    for entry in logs[-limit:]:
        role = entry.get("role", "")
        ts = entry.get("created_at", "")[:19]
        content = entry.get("content", "")
        if isinstance(content, str) and len(content) > 120:
            content = content[:120] + "..."
        tag = {"user": "❯", "assistant": "◆", "tool": "⚙"}.get(role, "·")
        sid = entry.get("session_id", "")[:8]
        print(f"  {tag} [{ts}] ({sid}) {content}")


# ---------------------------------------------------------------------------
# wyckoff workflow — 动态 workflow 状态
# ---------------------------------------------------------------------------


def _cmd_workflow(args):
    sub = args.workflow_cmd or "list"
    if sub == "list":
        _cmd_workflow_list(args.limit)
        return
    run_id = args.run_id
    if not run_id:
        print("用法: wyckoff workflow show <run_id>")
        sys.exit(1)
    if sub == "events":
        _cmd_workflow_events(run_id, args.limit)
        return
    if sub == "resume":
        _cmd_workflow_resume(run_id)
        return
    _cmd_workflow_show(run_id)


def _cmd_workflow_list(limit: int) -> None:
    from cli.workflows.store import list_workflow_runs

    runs = list_workflow_runs(limit=limit)
    if not runs:
        print("暂无 workflow 记录")
        return
    print(f"最近 workflow ({len(runs)} 条)")
    for run in runs:
        print(
            f"  {run['run_id']}  {run['status']}  {run['label']}  "
            f"{run.get('updated_at', '')[:19]}  {str(run.get('user_text', ''))[:50]}"
        )


def _cmd_workflow_show(run_id: str) -> None:
    from cli.workflows.store import get_workflow_run

    run = get_workflow_run(run_id)
    if not run:
        print(f"未找到 workflow: {run_id}")
        return
    print(f"{run['run_id']}  {run['status']}  {run['label']}")
    print(f"用户输入: {run.get('user_text', '')}")
    if script_line := _workflow_script_cli_line(run.get("plan", {})):
        print(script_line)
    for idx, step in enumerate(run.get("plan", {}).get("steps", []), start=1):
        print(f"  {idx}. {_workflow_step_cli_line(step)}")


def _workflow_script_cli_line(plan: dict[str, Any]) -> str:
    runtime = _workflow_plan_runtime(plan)
    parts: list[str] = []
    planner = str(runtime.get("planner") or "").strip()
    if planner:
        parts.append(f"source={_workflow_script_source_label(runtime, planner)}")
    if runtime.get("tool_contract_repair") == "model":
        parts.append(_workflow_tool_contract_repair_label(runtime))
    if runtime.get("adaptation") == "model_phase":
        parts.append(_workflow_adaptation_label(runtime))
    if truncated := _workflow_runtime_int(runtime, "truncated_step_count"):
        original = _workflow_runtime_int(runtime, "original_step_count")
        parts.append(f"trimmed={truncated}/{original}")
    return f"脚本: {' '.join(parts)}" if parts else ""


def _workflow_script_source_label(runtime: dict[str, Any], planner: str) -> str:
    if planner == "fallback_script" and runtime.get("fallback_kind") == "stock_selection":
        return "stock_selection_fallback"
    return planner


def _workflow_plan_runtime(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    script = plan.get("script") if isinstance(plan.get("script"), dict) else {}
    runtime = script.get("runtime") if isinstance(script.get("runtime"), dict) else {}
    return runtime


def _workflow_tool_contract_repair_label(runtime: dict[str, Any]) -> str:
    count = _workflow_runtime_int(runtime, "unscoped_step_count_before_repair")
    suffix = f":{count}" if count > 0 else ""
    return f"tool_contract_repair=model{suffix}"


def _workflow_adaptation_label(runtime: dict[str, Any]) -> str:
    count = _workflow_runtime_int(runtime, "adaptation_count")
    parts = [f"adaptation=model:{count}" if count > 0 else "adaptation=model"]
    for field, label in (
        ("adapted_kept_step_count", "kept"),
        ("adapted_removed_step_count", "removed"),
        ("adapted_added_step_count", "added"),
        ("adapted_skipped_step_count", "skipped"),
    ):
        value = _workflow_runtime_int(runtime, field)
        if value > 0:
            parts.append(f"{label}={value}")
    if detail := _workflow_adapted_step_detail(runtime):
        parts.append(detail)
    return ",".join(parts)


def _workflow_adapted_step_detail(runtime: dict[str, Any]) -> str:
    parts: list[str] = []
    for field, label in (
        ("adapted_removed_steps", "removed"),
        ("adapted_added_steps", "added"),
    ):
        titles = _workflow_adapted_step_titles(runtime.get(field))
        if titles:
            parts.append(f"{label}_titles={'|'.join(titles)}")
    return ",".join(parts)


def _workflow_adapted_step_titles(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    titles: list[str] = []
    for item in value[:3]:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("id") or "").strip()
            if title:
                titles.append(_clip_workflow_detail(title)[:32])
    return titles


def _workflow_runtime_int(runtime: dict[str, Any], field: str) -> int:
    try:
        return int(runtime.get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _workflow_step_cli_line(step: dict) -> str:
    status = step.get("status", "")
    title = step.get("title", "")
    agent = step.get("agent", "") or "agent"
    summary = step.get("summary", "")
    tools = _workflow_step_tool_label(step)
    detail = _workflow_step_detail_label(step)
    return f"[{status}] {title}  {agent}{tools}{detail}  {summary}"


def _workflow_step_tool_label(step: dict) -> str:
    tools = [str(item) for item in step.get("tool_scope", []) if str(item)]
    label = "tools"
    if not tools:
        tools = [str(item) for item in step.get("effective_tool_scope", []) if str(item)]
        label = "optional_tools"
    elif str(step.get("tool_scope_source") or "") == "semantic_inference":
        label = "inferred_tools"
    if not tools:
        return ""
    visible = ",".join(tools[:4])
    suffix = f",+{len(tools) - 4}" if len(tools) > 4 else ""
    return f" {label}={visible}{suffix}"


def _workflow_step_detail_label(step: dict) -> str:
    parts = [
        f"goal={_clip_workflow_detail(step.get('rationale'))}",
        f"done={_clip_workflow_detail(step.get('success_criteria'))}",
        f"guard={_clip_workflow_detail(step.get('risk_guard'))}",
        f"args={_clip_workflow_detail(_workflow_step_args_hint(step))}",
    ]
    visible = [part for part in parts if not part.endswith("=")]
    return f" {' '.join(visible)}" if visible else ""


def _workflow_step_args_hint(step: dict) -> str:
    if value := str(step.get("args_hint") or "").strip():
        return value
    text = str(step.get("context") or "")
    marker = "tool args hint:"
    if marker not in text:
        return ""
    value = text.split(marker, 1)[1].strip()
    return value.split("\n\n", 1)[0].strip()


def _clip_workflow_detail(value: object) -> str:
    text = str(value or "").strip()
    return text[:80] if text else ""


def _cmd_workflow_events(run_id: str, limit: int) -> None:
    from cli.workflows.store import load_workflow_events

    rows = load_workflow_events(run_id, limit=limit)
    if not rows:
        print(f"暂无 workflow 事件: {run_id}")
        return
    for row in rows:
        print(f"  {row.get('created_at', '')[:19]}  {row.get('event_type', '')}  {row.get('payload', {})}")


def _cmd_workflow_resume(run_id: str) -> None:
    from cli.workflows.resume import build_resume_prompt
    from cli.workflows.store import get_workflow_run

    run = get_workflow_run(run_id)
    if not run:
        print(f"未找到 workflow: {run_id}")
        return
    print(build_resume_prompt(run))


# ---------------------------------------------------------------------------
# wyckoff session — 会话管理
# ---------------------------------------------------------------------------


def _cmd_session(args):
    from integrations.local_db import init_db, list_chat_sessions

    init_db()
    sub = args.session_cmd or "list"

    if sub == "list":
        sessions = list_chat_sessions(limit=args.limit)
        if not sessions:
            print("暂无会话")
            return
        print(f"最近会话 ({len(sessions)} 条)")
        print(f"{'#':>2} {'session':<12} {'started':<16} {'msg':>4} {'tokens':>11}  preview")
        print("-" * 88)
        for idx, item in enumerate(sessions, start=1):
            first = str(item.get("first_user_msg", "") or "").replace("\n", " ")
            if len(first) > 42:
                first = first[:42] + "..."
            tokens = int(item.get("total_tokens_in") or 0) + int(item.get("total_tokens_out") or 0)
            print(
                f"{idx:>2} {str(item.get('session_id', ''))[:12]:<12} "
                f"{str(item.get('started_at', '') or '')[:16]:<16} {int(item.get('msg_count') or 0):>4} "
                f"{tokens:>11,}  {first}"
            )
        return

    if sub == "export":
        from cli.session_tools import SessionToolError, export_session_transcript

        try:
            result = export_session_transcript(
                session_id=args.session_id,
                output=Path(args.out).expanduser() if args.out else None,
                output_format=args.format,
            )
        except SessionToolError as exc:
            print(f"✗ {exc}")
            sys.exit(1)
        print(f"✓ 会话已导出: {result.path}")
        print(f"  session={result.session_id} messages={result.message_count}")
        return

    if sub == "fork":
        from cli.session_tools import SessionToolError, fork_session

        try:
            result = fork_session(session_id=args.session_id, new_session_id=args.new_id)
        except SessionToolError as exc:
            print(f"✗ {exc}")
            sys.exit(1)
        print(f"✓ 会话已分叉: {result.source_session_id} -> {result.new_session_id}")
        print(f"  messages={result.message_count}")
        return

    print("用法: wyckoff session [list|export|fork]")


# ---------------------------------------------------------------------------
# wyckoff trace — 查看 append-only 运行轨迹
# ---------------------------------------------------------------------------


def _cmd_trace(args):
    from pathlib import Path

    from cli.scratchpad import wyckoff_home

    trace_dir = wyckoff_home() / "scratchpad"
    if args.path:
        print(trace_dir)
        return

    if args.events:
        from cli.event_stream import scratchpad_events_jsonl

        target = Path(args.events).expanduser()
        if not target.is_absolute():
            target = trace_dir / target
        if not target.exists():
            print(f"轨迹文件不存在: {target}")
            sys.exit(1)
        events_text = scratchpad_events_jsonl([target])
        if args.out:
            out = Path(args.out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(events_text, encoding="utf-8")
            print(f"✓ 事件流已导出: {out}")
        else:
            print(events_text, end="")
        return

    if args.show:
        target = Path(args.show).expanduser()
        if not target.is_absolute():
            target = trace_dir / target
        if not target.exists():
            print(f"轨迹文件不存在: {target}")
            sys.exit(1)
        text = target.read_text(encoding="utf-8", errors="replace")
        print(text[: args.chars])
        if len(text) > args.chars:
            print(f"\n... 已截断，完整文件: {target}")
        return

    files = (
        sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if trace_dir.exists() else []
    )
    if not files:
        print("暂无运行轨迹")
        return

    print(f"最近运行轨迹 ({min(args.limit, len(files))} 条)")
    print()
    for path in files[: args.limit]:
        query = ""
        try:
            first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            data = json.loads(first)
            query = str(data.get("content", ""))
        except Exception as exc:
            query = f"(无法读取摘要: {type(exc).__name__})"
        if len(query) > 80:
            query = query[:80] + "..."
        stamp = path.name.split("_", 1)[0]
        print(f"  [{stamp}] {path.name}  {query}")


# ---------------------------------------------------------------------------
# wyckoff prompt — 查看/渲染投研 Prompt 模板
# ---------------------------------------------------------------------------


def _cmd_prompt(args):
    from cli.prompt_templates import load_prompt_templates, render_prompt_template

    templates = load_prompt_templates()
    sub = args.prompt_cmd or "list"

    if sub == "list":
        print("可用 Prompt 模板")
        print()
        for tpl in templates.values():
            hint = f" {tpl.argument_hint}" if tpl.argument_hint else ""
            print(f"  {tpl.name:<14} {tpl.description}{hint}")
        print("\n用法: wyckoff prompt render <name> [补充说明]")
        return

    if sub == "show":
        if not args.name:
            print("用法: wyckoff prompt show <name>")
            return
        tpl = templates.get(args.name)
        if not tpl:
            print(f"未知 Prompt 模板: {args.name}")
            return
        print(f"{tpl.name} — {tpl.description}")
        if tpl.argument_hint:
            print(f"参数: {tpl.argument_hint}")
        print()
        print(tpl.prompt)
        return

    if sub == "render":
        if not args.name:
            print("用法: wyckoff prompt render <name> [补充说明]")
            return
        tpl = templates.get(args.name)
        if not tpl:
            print(f"未知 Prompt 模板: {args.name}")
            return
        user_input = " ".join(args.extra or [])
        print(render_prompt_template(tpl, user_input))
        return

    print("用法: wyckoff prompt [list|show|render]")


# ---------------------------------------------------------------------------
# wyckoff diag — 导出可检查的会话诊断包
# ---------------------------------------------------------------------------


def _cmd_diag(args):
    from cli.diagnostic_export import DiagnosticExportError, export_diagnostic_package

    output = Path(args.out).expanduser() if args.out else None
    try:
        result = export_diagnostic_package(
            session_id=args.session,
            output=output,
            output_format=args.format,
        )
    except DiagnosticExportError as exc:
        print(f"✗ {exc}")
        sys.exit(1)

    print(f"✓ 诊断包已导出: {result.path}")
    print(
        f"  session={result.session_id} messages={result.message_count} "
        f"scratchpads={result.scratchpad_count} tool_results={result.tool_result_count}"
    )


# ---------------------------------------------------------------------------
# wyckoff sync — 手动同步 Supabase → SQLite
# ---------------------------------------------------------------------------


def _cmd_sync(_args=None):
    from integrations.local_db import get_sync_meta, init_db

    init_db()

    if _args and getattr(_args, "sync_cmd", "") == "status":
        for tbl in ("recommendation_tracking", "signal_pending", "market_signal_daily", "portfolio"):
            meta = get_sync_meta(tbl)
            if meta:
                print(f"  {tbl}: {meta['row_count']} rows, last_synced={meta['last_synced_at']}")
            else:
                print(f"  {tbl}: 未同步")
        return

    from integrations.sync import sync_all

    print("正在同步 Supabase → 本地...")
    result = sync_all()
    if not result:
        print("无需同步（数据未过期或 Supabase 未配置）")
        return
    for tbl, count in result.items():
        status = f"{count} rows" if count >= 0 else "failed"
        print(f"  {tbl}: {status}")
    print("同步完成")


def _cmd_cleanup(args):
    from integrations.local_db import cleanup_old_records, init_db

    init_db()
    days = args.days
    print(f"清理 {days} 天前的本地数据...")
    deleted = cleanup_old_records(days)
    for table, count in deleted.items():
        print(f"  {table}: 删除 {count} 条")
    total = sum(deleted.values())
    if total:
        print(f"共清理 {total} 条记录")
    else:
        print("无过期数据")


# ---------------------------------------------------------------------------
# TUI 启动
# ---------------------------------------------------------------------------


def _restore_tui_session(tools) -> bool:
    try:
        from cli.auth import load_session, restore_session

        had_session = load_session() is not None
        session = restore_session()
        if session:
            tools.state.update(
                {
                    "user_id": session["user_id"],
                    "email": session["email"],
                    "access_token": session.get("access_token", ""),
                    "refresh_token": session.get("refresh_token", ""),
                }
            )
        elif had_session:
            return True
    except Exception:
        logger.warning("session restore failed", exc_info=True)
    return False


def _init_tui_local_services() -> None:
    try:
        from integrations.local_db import init_db, prune_memories

        init_db()
        prune_memories()
        from integrations.sync import sync_all_background

        sync_all_background()
    except Exception:
        logger.warning("local db init or sync failed", exc_info=True)


def _load_tui_config_env() -> None:
    from cli.auth import load_config

    cfg = load_config()
    for key, env_key in [("tushare_token", "TUSHARE_TOKEN"), ("tickflow_api_key", "TICKFLOW_API_KEY")]:
        value = str(cfg.get(key, "") or "").strip()
        if value:
            os.environ.setdefault(env_key, value)


def _seed_model_env(configs: list[dict[str, Any]]) -> None:
    env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    for cfg in configs:
        env_key = env_map.get(cfg.get("provider_name", ""))
        if env_key and cfg.get("api_key"):
            os.environ.setdefault(env_key, cfg["api_key"])


def _build_tui_provider_state() -> dict[str, Any]:
    state: dict[str, Any] = {"provider": None, "provider_name": "", "model": "", "api_key": "", "base_url": ""}
    try:
        from cli.auth import load_default_model_id, load_model_configs

        configs = load_model_configs()
        default_id = load_default_model_id()
        if configs and default_id:
            _seed_model_env(configs)
            default_cfg = next((c for c in configs if c["id"] == default_id), configs[0])
            if len(configs) == 1:
                provider, err = create_provider(**provider_config_kwargs(default_cfg))
                if not err:
                    state.update(default_cfg)
                    state["provider"] = provider
            else:
                from cli.auth import load_fallback_model_id
                from cli.providers.fallback import FallbackProvider

                state.update(default_cfg)
                state["provider"] = FallbackProvider(configs, default_id, fallback_id=load_fallback_model_id())
    except Exception:
        logger.warning("model provider init failed", exc_info=True)
    return state


def _start_tui_dashboard() -> None:
    import threading
    import webbrowser

    def _dash_bg():
        try:
            from cli.dashboard import start_dashboard_background

            start_dashboard_background()
        except Exception:
            logger.debug("dashboard background start failed", exc_info=True)

    threading.Thread(target=_dash_bg, daemon=True).start()
    webbrowser.open("http://127.0.0.1:8765")


def _cleanup_tui_runtime() -> None:
    try:
        import baostock as bs

        bs.logout()
    except (Exception, KeyboardInterrupt):
        logger.debug("baostock logout failed", exc_info=True)
    try:
        _devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull, 1)
        os.dup2(_devnull, 2)
        os.close(_devnull)
    except Exception:
        logger.debug("devnull redirect failed", exc_info=True)


def _run_tui_app(tools, state: dict[str, Any], system_prompt: str, session_expired: bool) -> None:
    from cli.tui import WyckoffTUI

    app = WyckoffTUI(
        provider=state["provider"],
        tools=tools,
        state=state,
        system_prompt=system_prompt,
        session_expired=session_expired,
    )
    try:
        app.run()
    except KeyboardInterrupt:
        logger.debug("TUI interrupted by user", exc_info=True)
    finally:
        _cleanup_tui_runtime()


def _cmd_tui(_args=None):
    _check_update_async()

    from cli.tools import ToolRegistry
    from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

    tools = ToolRegistry()
    session_expired = _restore_tui_session(tools)
    _init_tui_local_services()
    try:
        _load_tui_config_env()
    except Exception:
        logger.debug("config env vars load failed", exc_info=True)
    state = _build_tui_provider_state()
    if state["provider"]:
        tools.set_provider(state["provider"])
    _start_tui_dashboard()
    _run_tui_app(tools, state, CHAT_AGENT_SYSTEM_PROMPT, session_expired)


# ---------------------------------------------------------------------------
# Argparse 主入口
# ---------------------------------------------------------------------------


def _dispatch_command(args) -> None:
    if args.cmd == "update":
        _cmd_update(args)
    elif args.cmd == "auth":
        _cmd_auth(args)
    elif args.cmd == "model":
        _cmd_model(args)
    elif args.cmd == "config":
        _cmd_config(args)
    elif args.cmd in ("portfolio", "pf"):
        _cmd_portfolio(args)
    elif args.cmd == "signal":
        _cmd_signal(args)
    elif args.cmd in ("recommend", "rec"):
        _cmd_recommend(args)
    elif args.cmd in ("dashboard", "dash"):
        from cli.dashboard import start_dashboard

        start_dashboard(port=args.port)
    elif args.cmd == "screen":
        _cmd_screen(args)
    elif args.cmd in ("backtest", "bt"):
        _cmd_backtest(args)
    elif args.cmd == "report":
        _cmd_report(args)
    elif args.cmd == "mcp":
        _cmd_mcp(args)
    elif args.cmd in ("memory", "mem"):
        _cmd_memory(args)
    elif args.cmd == "log":
        _cmd_log(args)
    elif args.cmd in ("workflow", "wf"):
        _cmd_workflow(args)
    elif args.cmd in ("session", "sess"):
        _cmd_session(args)
    elif args.cmd == "trace":
        _cmd_trace(args)
    elif args.cmd == "prompt":
        _cmd_prompt(args)
    elif args.cmd in ("diag", "diagnostic"):
        _cmd_diag(args)
    elif args.cmd == "sync":
        _cmd_sync(args)
    elif args.cmd == "cleanup":
        _cmd_cleanup(args)
    else:
        _cmd_tui(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wyckoff",
        description="威科夫终端读盘室 — Wyckoff 量价分析 Agent",
    )
    parser.add_argument("-v", "--version", action="version", version=f"wyckoff {_get_version()}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("update", help="升级到最新版")
    _add_auth_model_config_parsers(sub)
    _add_portfolio_history_parsers(sub)
    _add_analysis_job_parsers(sub)
    _add_session_debug_parsers(sub)
    _add_maintenance_parsers(sub)
    return parser


def _add_auth_model_config_parsers(sub) -> None:
    p_auth = sub.add_parser("auth", help="登录/登出/状态")
    p_auth.add_argument("auth_cmd", help="email 或 logout/status")
    p_auth.add_argument("password", nargs="?", default="", help="密码（可省略，交互输入）")

    p_model = sub.add_parser("model", help="模型管理")
    p_model.add_argument("model_cmd", nargs="?", default="list", help="list/add/set/rm/default/fallback/cost/usage")
    p_model.add_argument("model_id", nargs="?", default="", help="模型 ID")
    p_model.add_argument("provider", nargs="?", default="", help="供应商 (set 时)")
    p_model.add_argument("api_key", nargs="?", default="", help="API Key (set 时)")
    p_model.add_argument("--model", dest="model_name", default="", help="模型名")
    p_model.add_argument("--base-url", dest="base_url", default="", help="Base URL")
    p_model.add_argument("--input-per-1m", type=float, default=None, help="输入 token 每百万成本（USD，cost 时）")
    p_model.add_argument("--output-per-1m", type=float, default=None, help="输出 token 每百万成本（USD，cost 时）")
    p_model.add_argument("--context-window", type=int, default=None, help="上下文窗口 token 数（cost 时）")
    p_model.add_argument("--days", type=int, default=7, help="usage 统计天数")

    # wyckoff config
    p_config = sub.add_parser("config", help="本地配置（数据源/超时）")
    p_config.add_argument(
        "config_cmd",
        nargs="?",
        default="",
        help="tushare/tickflow/stream_timeout/tool_timeout",
    )
    p_config.add_argument("value", nargs="?", default="", help="值（可省略，交互输入）")


def _add_portfolio_history_parsers(sub) -> None:
    p_port = sub.add_parser("portfolio", help="持仓管理", aliases=["pf"])
    p_port.add_argument("portfolio_cmd", nargs="?", default="list", help="list/add/rm/cash/fill")
    p_port.add_argument("code", nargs="?", default="", help="股票代码 (add/rm/fill 时)")
    p_port.add_argument("--name", default="", help="股票名称")
    p_port.add_argument("--shares", type=int, default=0, help="持仓数量 / 成交股数")
    p_port.add_argument("--cost", type=float, default=0, help="成本价")
    p_port.add_argument("--buy-dt", dest="buy_dt", default="", help="买入日期 YYYYMMDD")
    p_port.add_argument("--amount", type=float, default=None, help="可用资金金额 (cash 时)")
    p_port.add_argument("--side", default="", choices=["", "buy", "sell"], help="成交方向 (fill 时)")
    p_port.add_argument("--price", type=float, default=0, help="成交价 (fill 时)")
    p_port.add_argument("--date", default="", help="成交日期 YYYYMMDD，默认今天 (fill 时)")

    # wyckoff signal
    p_signal = sub.add_parser("signal", help="信号确认池")
    p_signal.add_argument("status", nargs="?", default="all", help="all/pending/survived/confirmed/expired")
    p_signal.add_argument("-n", "--limit", type=int, default=30, help="返回条数")

    # wyckoff recommend
    p_rec = sub.add_parser("recommend", help="威科夫形态复盘", aliases=["rec"])
    p_rec.add_argument("-n", "--limit", type=int, default=20, help="返回条数")


def _add_analysis_job_parsers(sub) -> None:
    p_dash = sub.add_parser("dashboard", help="启动本地可视化面板", aliases=["dash"])
    p_dash.add_argument("--port", type=int, default=8765, help="HTTP 端口 (默认 8765)")

    # wyckoff screen
    p_screen = sub.add_parser("screen", help="全市场漏斗筛选")
    p_screen.add_argument("--board", default="all", help="板块 (all/main/chinext/gem/star/bse)")
    p_screen.add_argument("--limit", type=int, default=None, help="扫描数量上限；0 表示全量")
    p_screen.add_argument("--style", default="", help="候选风格偏好 (trend/pullback/quality，或中文强势/低吸/稳健)")
    financial_group = p_screen.add_mutually_exclusive_group()
    financial_group.add_argument(
        "--financial-metrics",
        dest="financial_metrics",
        action="store_true",
        default=None,
        help="启用财务指标过滤",
    )
    financial_group.add_argument(
        "--no-financial-metrics",
        dest="financial_metrics",
        action="store_false",
        help="跳过财务指标过滤以加快扫描",
    )

    # wyckoff backtest
    p_bt = sub.add_parser("backtest", help="策略历史回测", aliases=["bt"])
    p_bt.add_argument("--hold-days", type=int, default=10, help="持有天数 (默认 10)")
    p_bt.add_argument("--months", type=int, default=18, help="回测月数 (默认 18)")
    p_bt.add_argument("--top-n", type=int, default=4, help="每批取前N只 (默认 4)")

    # wyckoff report
    p_report = sub.add_parser("report", help="AI 深度研报")
    p_report.add_argument("codes", help="股票代码，逗号分隔 (如 000001,600519)")

    # wyckoff mcp
    sub.add_parser("mcp", help="启动 MCP Server")


def _add_session_debug_parsers(sub) -> None:
    _add_memory_parser(sub)

    # wyckoff log
    p_log = sub.add_parser("log", help="查看对话日志")
    p_log.add_argument("--session", default="", help="指定会话 ID")
    p_log.add_argument("-n", "--limit", type=int, default=30, help="返回条数")

    # wyckoff workflow
    p_wf = sub.add_parser("workflow", help="查看动态 workflow", aliases=["wf"])
    p_wf.add_argument("workflow_cmd", nargs="?", default="list", help="list/show/events/resume")
    p_wf.add_argument("run_id", nargs="?", default="", help="workflow run id")
    p_wf.add_argument("-n", "--limit", type=int, default=20, help="返回条数")

    # wyckoff session
    p_session = sub.add_parser("session", help="会话列表 / 导出 / 分叉", aliases=["sess"])
    p_session.add_argument("session_cmd", nargs="?", default="list", help="list/export/fork")
    p_session.add_argument("session_id", nargs="?", default="", help="会话 ID；留空时使用最近会话")
    p_session.add_argument("-n", "--limit", type=int, default=20, help="list 返回条数")
    p_session.add_argument("--out", default="", help="export 输出路径；默认 ~/.wyckoff/sessions/exports/")
    p_session.add_argument("--format", choices=["md", "json"], default="md", help="export 输出格式")
    p_session.add_argument("--new-id", default="", help="fork 新会话 ID；默认自动生成")

    # wyckoff trace
    p_trace = sub.add_parser("trace", help="查看 JSONL 运行轨迹")
    p_trace.add_argument("-n", "--limit", type=int, default=10, help="返回条数")
    p_trace.add_argument("--show", default="", help="显示指定轨迹文件（文件名或绝对路径）")
    p_trace.add_argument("--chars", type=int, default=12000, help="--show 输出字符数上限")
    p_trace.add_argument("--path", action="store_true", help="只打印轨迹目录")
    p_trace.add_argument("--events", default="", help="将指定 scratchpad JSONL 转成标准事件流")
    p_trace.add_argument("--out", default="", help="--events 输出路径；留空则打印到 stdout")

    # wyckoff prompt
    p_prompt = sub.add_parser("prompt", help="查看/渲染 Prompt 模板")
    p_prompt.add_argument("prompt_cmd", nargs="?", default="list", help="list/show/render")
    p_prompt.add_argument("name", nargs="?", default="", help="模板名")
    p_prompt.add_argument("extra", nargs=argparse.REMAINDER, help="render 时传入的补充说明")

    # wyckoff diag
    p_diag = sub.add_parser("diag", help="导出会话诊断包", aliases=["diagnostic"])
    p_diag.add_argument("--session", default="", help="指定会话 ID；默认最近一个会话")
    p_diag.add_argument("--out", default="", help="输出路径；默认 ~/.wyckoff/diagnostics/")
    p_diag.add_argument("--format", choices=["zip", "json"], default="zip", help="输出格式")


def _add_maintenance_parsers(sub) -> None:
    # wyckoff sync
    p_sync = sub.add_parser("sync", help="同步 Supabase → 本地 SQLite")
    p_sync.add_argument("sync_cmd", nargs="?", default="", help="status: 查看同步状态")

    # wyckoff cleanup
    p_cleanup = sub.add_parser("cleanup", help="清理过期本地数据")
    p_cleanup.add_argument("--days", type=int, default=30, help="保留天数 (默认 30)")


def main():
    from dotenv import load_dotenv

    load_dotenv()
    _set_terminal_title("Wyckoff-Analysis")
    parser = _build_parser()
    args = parser.parse_args()
    _dispatch_command(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.debug("main interrupted by user", exc_info=True)
