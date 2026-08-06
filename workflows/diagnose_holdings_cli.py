"""Runtime helpers for the standalone holding diagnosis CLI."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime

import pandas as pd

from core.holding_diagnostic import (
    HoldingDiagnostic,
    HoldingInput,
    diagnose_holdings,
    format_diagnostic_text,
)
from core.wyckoff_engine import FunnelConfig, normalize_hist_from_fetch
from integrations.fetch_a_share_csv import fetch_hist, resolve_trading_window
from integrations.index_data_source import fetch_index_hist
from utils.trading_clock import resolve_end_calendar_day

TRADING_DAYS = 320


def _fetch_stock_data(code: str, window) -> tuple[str, pd.DataFrame | None]:
    """拉取单只股票 OHLCV 数据，返回 (code, df_or_None)。"""

    symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    try:
        raw = fetch_hist(symbol, window, adjust="qfq")
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return code, None
        df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
        return code, df
    except Exception as e:
        print(f"  ⚠ {code} 数据拉取失败: {e}", file=sys.stderr)
        return code, None


def _fetch_benchmark(window) -> pd.DataFrame | None:
    """拉取上证指数作为基准。"""

    try:
        bench_raw = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        if bench_raw is None or bench_raw.empty:
            return None
        bench_df = normalize_hist_from_fetch(bench_raw).sort_values("date").reset_index(drop=True)
        return bench_df
    except Exception as e:
        print(f"  ⚠ 基准指数拉取失败: {e}", file=sys.stderr)
        return None


def _load_from_supabase(portfolio_id: str) -> list[HoldingInput]:
    """从 Supabase 读取实盘持仓。"""
    try:
        from integrations.supabase_portfolio import is_supabase_configured, load_portfolio_state

        if not is_supabase_configured():
            print("  ✘ Supabase 未配置（缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）", file=sys.stderr)
            sys.exit(1)

        state = load_portfolio_state(portfolio_id)
        if state is None:
            print(f"  ✘ 未找到组合 {portfolio_id}", file=sys.stderr)
            sys.exit(1)

        positions = state.get("positions", [])
        if not positions:
            print(f"  ✘ 组合 {portfolio_id} 无持仓", file=sys.stderr)
            sys.exit(1)

        holdings = []
        for pos in positions:
            holding = HoldingInput.from_position(pos)
            if holding.code and holding.cost > 0:
                holdings.append(holding)
        return holdings
    except ImportError as e:
        print(f"  ✘ 依赖缺失: {e}", file=sys.stderr)
        sys.exit(1)


def _format_json(diagnostics: list[HoldingDiagnostic]) -> str:
    """输出 JSON 格式的诊断结果。"""
    results = []
    for d in diagnostics:
        data = asdict(d)
        # 确保浮点数可读
        for k, v in data.items():
            if isinstance(v, float):
                data[k] = round(v, 4)
        results.append(data)
    return json.dumps(results, ensure_ascii=False, indent=2)


def _format_markdown(diagnostics: list[HoldingDiagnostic]) -> str:
    """输出 Markdown 格式的诊断结果。"""
    lines = ["# 持仓健康诊断报告", ""]
    lines.append(f"诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 总览表格
    lines.append("## 总览")
    lines.append("")
    lines.append("| 代码 | 名称 | 健康 | 盈亏% | 通道 | 均线 | 止损状态 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    for d in diagnostics:
        lines.append(
            f"| {d.code} | {d.name} | {d.health} | {d.pnl_pct:+.2f}% "
            f"| {d.l2_channel} | {d.ma_pattern} | {d.stop_loss_status} |"
        )
    lines.append("")

    # 详细诊断
    lines.append("## 详细诊断")
    lines.append("")
    for d in diagnostics:
        lines.append(f"### {d.code} {d.name}")
        lines.append("```")
        lines.append(format_diagnostic_text(d))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def _format_text(diagnostics: list[HoldingDiagnostic]) -> str:
    separator = "─" * 60
    parts = [
        f"\n{'═' * 60}",
        f"  持仓健康诊断报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"{'═' * 60}\n",
    ]
    for diagnostic in diagnostics:
        parts.append(format_diagnostic_text(diagnostic))
        parts.append(separator)

    healthy = sum(1 for d in diagnostics if "健康" in d.health)
    warning = sum(1 for d in diagnostics if "警戒" in d.health)
    danger = sum(1 for d in diagnostics if "危险" in d.health)
    avg_pnl = sum(d.pnl_pct for d in diagnostics) / len(diagnostics) if diagnostics else 0
    parts.append(f"\n📊 总览: 🟢健康 {healthy} | 🟡警戒 {warning} | 🔴危险 {danger}")
    parts.append(f"📈 平均盈亏: {avg_pnl:+.2f}%")
    parts.append("")
    return "\n".join(parts)


def _parse_inline_holdings(
    codes_text: str, costs_text: str, names_text: str = "", buy_dts_text: str = ""
) -> list[HoldingInput]:
    codes = [c.strip() for c in codes_text.split(",") if c.strip()]
    costs_raw = [c.strip() for c in costs_text.split(",") if c.strip()]
    names_raw = [n.strip() for n in names_text.split(",") if n.strip()] if names_text else []
    buy_dts_raw = [d.strip() for d in buy_dts_text.split(",") if d.strip()] if buy_dts_text else []
    if len(codes) != len(costs_raw):
        print(f"  ✘ --codes ({len(codes)}个) 与 --costs ({len(costs_raw)}个) 数量不匹配", file=sys.stderr)
        sys.exit(1)
    if buy_dts_raw and len(buy_dts_raw) != len(codes):
        print(f"  ✘ --codes ({len(codes)}个) 与 --buy-dts ({len(buy_dts_raw)}个) 数量不匹配", file=sys.stderr)
        sys.exit(1)

    holdings: list[HoldingInput] = []
    for idx, (code, cost_str) in enumerate(zip(codes, costs_raw)):
        try:
            cost = float(cost_str)
        except ValueError:
            print(f"  ✘ 成本价格式错误: {cost_str}", file=sys.stderr)
            sys.exit(1)
        holdings.append(
            HoldingInput(
                code=code,
                name=names_raw[idx] if idx < len(names_raw) else "--",
                cost=cost,
                buy_dt=buy_dts_raw[idx] if idx < len(buy_dts_raw) else "",
            )
        )
    return holdings


def _resolve_holdings(args) -> list[HoldingInput]:
    if args.from_portfolio:
        print(f"📂 从 Supabase 读取持仓: {args.from_portfolio}")
        return _load_from_supabase(args.from_portfolio)
    if args.codes:
        return _parse_inline_holdings(args.codes, args.costs, args.names, args.buy_dts)
    raise ValueError("请提供 --codes 或 --from-portfolio")


def _fetch_holding_frames(holdings: list[HoldingInput], window) -> dict[str, pd.DataFrame]:
    df_map: dict[str, pd.DataFrame] = {}
    for holding in holdings:
        print(f"  拉取 {holding.code} {holding.name}...")
        _, df = _fetch_stock_data(holding.code, window)
        if df is not None:
            df_map[holding.code] = df
    return df_map


def _warn_missing_buy_dt(holdings: list[HoldingInput]) -> None:
    missing = [f"{h.code} {h.name}" for h in holdings if not h.buy_dt]
    if not missing:
        return
    print(
        f"  ⚠ {len(missing)} 只持仓缺少建仓日，其退出信号基于全历史，建仓前的暴跌可能被误判为破位: "
        f"{'、'.join(missing)}",
        file=sys.stderr,
    )


def _diagnose(holdings: list[HoldingInput]) -> list[HoldingDiagnostic]:
    if not holdings:
        print("  ✘ 无有效持仓可诊断", file=sys.stderr)
        sys.exit(1)

    print(f"\n🔍 开始诊断 {len(holdings)} 只持仓...")
    _warn_missing_buy_dt(holdings)

    # ── 准备数据窗口 ──
    end_day = resolve_end_calendar_day()
    window = resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    print(f"  数据窗口: {window.start_trade_date} → {window.end_trade_date}")

    print("  拉取基准指数 (上证指数)...")
    bench_df = _fetch_benchmark(window)
    df_map = _fetch_holding_frames(holdings, window)

    # ── 执行诊断 ──
    print("\n⚙ 执行 Wyckoff 健康诊断...\n")
    cfg = FunnelConfig()
    return diagnose_holdings(holdings, df_map, bench_df, cfg)


def _format_output(diagnostics: list[HoldingDiagnostic], output_format: str) -> str:
    if output_format == "json":
        return _format_json(diagnostics)
    if output_format == "markdown":
        return _format_markdown(diagnostics)
    return _format_text(diagnostics)


def _emit_output(output: str, output_path: str) -> None:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✅ 结果已写入 {output_path}")
    else:
        print(output)


def run_diagnose_holdings_cli(args) -> None:
    holdings = _resolve_holdings(args)
    diagnostics = _diagnose(holdings)
    _emit_output(_format_output(diagnostics, args.format), args.output)
