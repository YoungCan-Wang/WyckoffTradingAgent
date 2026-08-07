"""Markdown report rendering for backtest summaries."""

from __future__ import annotations

from core.backtest_metrics import fmt_metric
from core.cash_portfolio import STYLE_LABELS
from core.strategy_policy_display import format_policy_meta_text, format_policy_weight_text


def build_summary_md(summary: dict) -> str:
    lines = [
        "# Wyckoff Funnel Daily Backtest",
        "",
        *_overview_lines(summary),
        "",
        "## 收益统计",
        *_return_stats_lines(summary),
        "",
        "## 组合风险指标（单利口径 · 基于每日净值曲线）",
        *_portfolio_risk_lines(summary),
        "",
        *_cash_portfolio_lines(summary),
        *_build_cash_style_table(summary),
        *_wbt_lines(summary),
        "## 逐笔风险统计",
        *_trade_risk_lines(summary),
    ]
    _append_stratified_sections(lines, summary.get("stratified", {}))
    advice = generate_strategy_advice(summary)
    if advice:
        lines.extend(["", "## 策略调整建议", *[f"{idx}. {item}" for idx, item in enumerate(advice, 1)]])
    lines.extend(["", "## 说明", *_notes(summary)])
    return "\n".join(lines)


def generate_strategy_advice(summary: dict) -> list[str]:
    advice = []
    _append_regime_advice(advice, summary.get("stratified", {}).get("by_regime", {}))
    _append_track_advice(advice, summary.get("stratified", {}).get("by_track", {}))
    _append_global_advice(advice, summary)
    return advice or ["🟢 当前参数组合表现尚可，暂无强烈调整建议"]


def _overview_lines(summary: dict) -> list[str]:
    return [
        f"- 区间: {summary.get('start')} ~ {summary.get('end')}",
        f"- 持有周期: {summary.get('hold_days')} 交易日",
        _top_n_line(summary),
        f"- AI 候选模式: {summary.get('ai_selection_mode')}",
        f"- 策略消融组: {summary.get('strategy_variant', 'live')}",
        _signal_weight_line(summary),
        f"- 股票池: {summary.get('board')} (sample={summary.get('sample_size')})",
        f"- 评估交易日: {summary.get('eval_days')}",
        f"- 触发交易日: {summary.get('signal_days')}",
        f"- 离场模式: {summary.get('exit_mode')}",
        *_exit_lines(summary),
        _trailing_line(summary),
        f"- 日内触发优先级: {summary.get('sltp_priority')}",
        f"- 买入摩擦成本: {fmt_metric(summary.get('buy_friction_pct'), 3)}%",
        f"- 卖出摩擦成本: {fmt_metric(summary.get('sell_friction_pct'), 3)}%",
        f"- 元数据口径: {_meta_mode(summary)}",
        f"- 信号确认模式: {summary.get('pending_mode')}",
        _regime_filter_line(summary),
        _execution_regime_gate_line(summary),
        _entry_price_mode_line(summary),
        f"- 交易风格: {_style_text(summary)}",
        _metrics_engine_line(summary),
        f"- 成交样本: {summary.get('trades')}",
        *_crash_probe_lines(summary),
    ]


def _crash_probe_lines(summary: dict) -> list[str]:
    watched = int(summary.get("crash_probe_watch_candidates") or 0)
    if watched <= 0:
        return ["- CRASH 左侧试错回放: 区间内无抗跌观察样本"]
    return [
        "- CRASH 左侧试错回放（日线代理）: "
        f"观察 {watched} / 硬条件 {int(summary.get('crash_probe_proxy_qualified') or 0)} / "
        f"Top1 入场 {int(summary.get('crash_probe_staged_entries') or 0)} / "
        f"次日确认 {int(summary.get('crash_probe_confirmed_next_day') or 0)} "
        f"({fmt_metric(summary.get('crash_probe_confirmation_rate_pct'), 2)}%)",
        "- CRASH 分段仓位权重收益和（研究诊断，非组合收益）: "
        f"仅2%试错 {fmt_metric(summary.get('crash_probe_probe_2pct_capital_return_pct'), 3)}% / "
        f"确认加3% {fmt_metric(summary.get('crash_probe_confirmed_add_3pct_capital_return_pct'), 3)}% / "
        f"合计 {fmt_metric(summary.get('crash_probe_staged_2_to_5pct_capital_return_pct'), 3)}%",
    ]


def _signal_weight_line(summary: dict) -> str:
    weights = summary.get("signal_weight_map") or {}
    meta_text = format_policy_meta_text(summary.get("signal_weight_meta"))
    if not weights:
        return f"- 策略治理调权: 未启用{meta_text}"
    weight_text = format_policy_weight_text(weights, limit=12, delimiter="；")
    return f"- 策略治理调权: {weight_text}{meta_text}"


def _return_stats_lines(summary: dict) -> list[str]:
    return [
        f"- 胜率: {fmt_metric(summary.get('win_rate_pct'), 2)}%",
        f"- 平均收益: {fmt_metric(summary.get('avg_ret_pct'), 3)}%",
        f"- 中位收益: {fmt_metric(summary.get('median_ret_pct'), 3)}%",
        f"- 25%分位: {fmt_metric(summary.get('q25_ret_pct'), 3)}%",
        f"- 75%分位: {fmt_metric(summary.get('q75_ret_pct'), 3)}%",
    ]


def _regime_filter_line(summary: dict) -> str:
    if summary.get("regime_filter_note") == "deprecated_live_aligned_noop":
        return "- 大盘水温仓控: 关闭（旧回测开关已废弃，跟随实盘漏斗候选口径）"
    return f"- 大盘水温仓控: {'开启' if summary.get('regime_filter') else '关闭'}"


def _execution_regime_gate_line(summary: dict) -> str:
    mode = str(summary.get("execution_regime_gate") or "live")
    labels = {"live": "实盘一致", "off": "关闭（研究对照）", "neutral_only": "仅 NEUTRAL 可开仓"}
    blocked_days = int(summary.get("regime_blocked_signal_days") or 0)
    blocked_candidates = int(summary.get("regime_blocked_candidates") or 0)
    return f"- 执行水温闸门: {labels.get(mode, mode)}；拦截 {blocked_days} 个信号日 / {blocked_candidates} 个候选"


def _portfolio_risk_lines(summary: dict) -> list[str]:
    return [
        f"- 夏普比 (Sharpe Ratio): {fmt_metric(summary.get('sharpe_ratio'), 3)}",
        f"- 卡玛比 (Calmar Ratio): {fmt_metric(summary.get('calmar_ratio'), 3)}",
        f"- 最大回撤: {fmt_metric(summary.get('max_drawdown_pct'), 2)}%",
        f"- 组合年化收益: {fmt_metric(summary.get('portfolio_ann_ret_pct'), 2)}%",
        f"- 组合总收益: {fmt_metric(summary.get('portfolio_total_ret_pct'), 2)}%",
        f"- 平均持仓数: {fmt_metric(summary.get('portfolio_avg_positions'), 1)}",
    ]


def _cash_portfolio_lines(summary: dict) -> list[str]:
    if not summary.get("cash_portfolio_enabled"):
        return []
    return [
        "## 真实现金账户模拟",
        f"- 主风格: {_style_display(summary)} ({summary.get('cash_portfolio_style')})",
        f"- 初始现金: {fmt_metric(summary.get('cash_portfolio_initial_cash'), 2)}",
        f"- 最多持仓: {fmt_metric(summary.get('cash_portfolio_max_positions'), 0)}",
        f"- 最终现金: {fmt_metric(summary.get('cash_portfolio_final_cash'), 2)}",
        f"- 总收益: {fmt_metric(summary.get('cash_portfolio_total_return_pct'), 2)}%",
        f"- 现金最大回撤: {fmt_metric(summary.get('cash_portfolio_max_drawdown_pct'), 2)}%",
        f"- 成交笔数: {fmt_metric(summary.get('cash_portfolio_trades'), 0)}",
        f"- 胜率: {fmt_metric(summary.get('cash_portfolio_win_rate_pct'), 2)}%",
        f"- 平均盈利: {fmt_metric(summary.get('cash_portfolio_avg_profit_pct'), 3)}%",
        f"- 平均亏损: {fmt_metric(summary.get('cash_portfolio_avg_loss_pct'), 3)}%",
        "- 成交摩擦: 买入 "
        f"{fmt_metric(summary.get('cash_portfolio_buy_friction_pct'), 3)}% / 卖出 "
        f"{fmt_metric(summary.get('cash_portfolio_sell_friction_pct'), 3)}%；"
        f"合计 {fmt_metric(summary.get('cash_portfolio_friction_total'), 2)}",
        f"- 佣金/税费合计: {fmt_metric(summary.get('cash_portfolio_trade_cost_total'), 2)}",
        "",
    ]


def _build_cash_style_table(summary: dict) -> list[str]:
    rows = _cash_style_summaries(summary)
    if len(rows) <= 1:
        return []
    lines = [
        "## 交易风格对比",
        "",
        "| 风格ID | 风格 | 最终现金 | 总收益 | 现金回撤 | 成交 | 胜率 | 平均盈利 | 平均亏损 | 加仓 | 换股 | 观察未确认 | 跳过 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(_cash_style_row(row) for row in rows)
    return lines + [""]


def _cash_style_row(row: dict) -> str:
    fields = [
        str(row.get("cash_portfolio_style") or "-"),
        _style_display(row),
        fmt_metric(row.get("cash_portfolio_final_cash"), 2),
        f"{fmt_metric(row.get('cash_portfolio_total_return_pct'), 2)}%",
        f"{fmt_metric(row.get('cash_portfolio_max_drawdown_pct'), 2)}%",
        fmt_metric(row.get("cash_portfolio_trades"), 0),
        f"{fmt_metric(row.get('cash_portfolio_win_rate_pct'), 2)}%",
        f"{fmt_metric(row.get('cash_portfolio_avg_profit_pct'), 3)}%",
        f"{fmt_metric(row.get('cash_portfolio_avg_loss_pct'), 3)}%",
        fmt_metric(row.get("cash_portfolio_add_entries"), 0),
        fmt_metric(row.get("cash_portfolio_swap_exits"), 0),
        fmt_metric(row.get("cash_portfolio_unconfirmed"), 0),
        str(_cash_style_skipped(row)),
    ]
    return "| " + " | ".join(fields) + " |"


def _wbt_lines(summary: dict) -> list[str]:
    if summary.get("wbt_available") is True:
        return [
            "## wbt 权重回测辅助指标",
            f"- wbt 年化收益: {fmt_metric(summary.get('wbt_ann_return_pct'), 2)}%",
            f"- wbt 绝对收益: {fmt_metric(summary.get('wbt_abs_return_pct'), 2)}%",
            f"- wbt 夏普比: {fmt_metric(summary.get('wbt_sharpe_ratio'), 3)}",
            f"- wbt 卡玛比: {fmt_metric(summary.get('wbt_calmar_ratio'), 3)}",
            f"- wbt 最大回撤: {fmt_metric(summary.get('wbt_max_drawdown_pct'), 2)}%",
            f"- wbt 日胜率: {fmt_metric(summary.get('wbt_daily_win_rate_pct'), 2)}%",
            "",
        ]
    if summary.get("wbt_requested"):
        return ["## wbt 权重回测辅助指标", f"- 状态: 不可用（{summary.get('wbt_error') or '未安装 wbt'}）", ""]
    return []


def _trade_risk_lines(summary: dict) -> list[str]:
    return [
        f"- VaR95(单笔收益): {fmt_metric(summary.get('var95_ret_pct'), 3)}%",
        f"- CVaR95(最差5%均值): {fmt_metric(summary.get('cvar95_ret_pct'), 3)}%",
        f"- 最长连续亏损笔数: {fmt_metric(summary.get('max_consecutive_losses'), 0)}",
    ]


def _append_stratified_sections(lines: list[str], stratified: dict) -> None:
    _append_track_table(lines, stratified.get("by_track", {}))
    _append_regime_table(lines, stratified.get("by_regime", {}))
    _append_diagnostic_table(lines, "分层诊断：按触发信号", stratified.get("by_trigger", {}))
    _append_diagnostic_table(lines, "分层诊断：按退出原因", stratified.get("by_exit_reason", {}))
    _append_diagnostic_table(lines, "分层诊断：按入场价格来源", stratified.get("by_entry_price_source", {}))


def _append_track_table(lines: list[str], by_track: dict) -> None:
    if not by_track:
        return
    lines.extend(["", "## 分层统计：Trend vs Accum", "", "| 指标 | Trend | Accum |", "|------|-------|-------|"])
    labels = [
        ("trades", "成交笔数", 0),
        ("win_rate_pct", "胜率(%)", 2),
        ("avg_ret_pct", "平均收益(%)", 3),
        ("median_ret_pct", "中位收益(%)", 3),
        ("max_drawdown_pct", "最大回撤(%)", 3),
        ("sharpe_ratio", "夏普比", 3),
        ("calmar_ratio", "卡玛比", 3),
        ("max_consecutive_losses", "最长连亏", 0),
    ]
    for key, label, nd in labels:
        lines.append(
            f"| {label} | {fmt_metric(by_track.get('Trend', {}).get(key), nd)} | {fmt_metric(by_track.get('Accum', {}).get(key), nd)} |"
        )


def _append_regime_table(lines: list[str], by_regime: dict) -> None:
    if not by_regime:
        return
    regime_keys = sorted(by_regime.keys())
    lines.extend(["", "## 分层统计：按大盘水温", ""])
    lines.extend(
        ["| 指标 | " + " | ".join(regime_keys) + " |", "|------|" + "|".join(["-------"] * len(regime_keys)) + "|"]
    )
    for key, label, nd in [
        ("trades", "成交笔数", 0),
        ("win_rate_pct", "胜率(%)", 2),
        ("avg_ret_pct", "平均收益(%)", 3),
        ("sharpe_ratio", "夏普比", 3),
    ]:
        vals = [fmt_metric(by_regime[rk].get(key), nd) for rk in regime_keys]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")


def _append_diagnostic_table(lines: list[str], title: str, groups: dict[str, dict], *, limit: int = 12) -> None:
    if not groups:
        return
    ranked = sorted(groups.items(), key=lambda kv: (-int(kv[1].get("trades") or 0), kv[0]))[:limit]
    lines.extend(["", f"## {title}", "", "| 分组 | 笔数 | 胜率(%) | 均收(%) | 止损率(%) | 平均MFE(%) | 平均MAE(%) |"])
    lines.append("|------|---:|---:|---:|---:|---:|---:|")
    for key, stat in ranked:
        vals = [
            key,
            *_stat_values(
                stat, ("trades", "win_rate_pct", "avg_ret_pct", "stop_exit_rate_pct", "avg_mfe_pct", "avg_mae_pct")
            ),
        ]
        lines.append("| " + " | ".join(vals) + " |")


def _append_regime_advice(advice: list[str], by_regime: dict) -> None:
    for regime, stats in sorted(by_regime.items()):
        avg = stats.get("avg_ret_pct")
        trades = stats.get("trades", 0)
        if avg is not None and trades >= 10 and avg < -1.5:
            advice.append(f"🔴 {regime} 环境下平均收益 {avg:+.2f}%（{trades}笔），建议该水温下暂停开仓或大幅降仓")
        elif avg is not None and trades >= 10 and avg < -0.5:
            advice.append(f"🟡 {regime} 环境下平均收益 {avg:+.2f}%（{trades}笔），建议降低仓位至 30% 以下")
        elif avg is not None and trades >= 10 and avg > 1.0:
            advice.append(f"🟢 {regime} 环境下表现较好（均收 {avg:+.2f}%），可加大仓位")


def _append_track_advice(advice: list[str], by_track: dict) -> None:
    trend, accum = by_track.get("Trend", {}), by_track.get("Accum", {})
    t_sharpe, a_sharpe = trend.get("sharpe_ratio"), accum.get("sharpe_ratio")
    if t_sharpe is None or a_sharpe is None or abs((t_sharpe or 0) - (a_sharpe or 0)) <= 0.5:
        return
    better = "Accum" if (a_sharpe or 0) > (t_sharpe or 0) else "Trend"
    worse = "Trend" if better == "Accum" else "Accum"
    advice.append(
        f"🟡 {better}（夏普 {by_track[better].get('sharpe_ratio', 0):.3f}）"
        f"明显优于 {worse}（夏普 {by_track[worse].get('sharpe_ratio', 0):.3f}），考虑侧重 {better} 信号"
    )


def _append_global_advice(advice: list[str], summary: dict) -> None:
    win_rate = summary.get("win_rate_pct")
    mdd = summary.get("max_drawdown_pct")
    sharpe = summary.get("sharpe_ratio")
    max_consec = summary.get("max_consecutive_losses", 0)
    if win_rate is not None and win_rate < 35:
        advice.append(f"🔴 整体胜率仅 {win_rate:.1f}%，低于 35% 警戒线，建议收紧入场筛选条件或增加信号确认环节")
    elif win_rate is not None and win_rate < 45:
        advice.append(f"🟡 胜率 {win_rate:.1f}%，偏低，考虑提高信号分数门槛")
    _append_risk_advice(advice, mdd, max_consec, summary.get("portfolio_avg_positions"))
    _append_profit_advice(advice, summary.get("take_profit_pct", 0), sharpe)


def _append_risk_advice(advice: list[str], mdd: float | None, max_consec: int, avg_pos: float | None) -> None:
    if mdd is not None and mdd < -25:
        advice.append(f"🔴 最大回撤 {mdd:.1f}%，建议收紧止损线或降低每日候选数 TopN")
    elif mdd is not None and mdd < -15:
        advice.append(f"🟡 最大回撤 {mdd:.1f}%，关注风控参数是否偏松")
    if max_consec and int(max_consec) >= 8:
        advice.append(f"🔴 最长连续亏损 {int(max_consec)} 笔，建议增加信号确认机制或缩短持有期")
    elif max_consec and int(max_consec) >= 5:
        advice.append(f"🟡 最长连续亏损 {int(max_consec)} 笔，关注是否需要加入熔断机制")
    if avg_pos is not None and avg_pos < 0.5:
        advice.append("🟡 大部分交易日无持仓，信号触发过少，考虑放宽筛选条件或扩大股票池")


def _append_profit_advice(advice: list[str], take_profit: float, sharpe: float | None) -> None:
    if take_profit and take_profit > 0 and sharpe is not None and sharpe < -0.3:
        advice.append(f"🟡 开启 TP{take_profit:.0f}% 后夏普仍为 {sharpe:.3f}，止盈可能过早截断盈利单，建议尝试关闭止盈")
    if sharpe is not None and sharpe > 0.5:
        advice.append(f"🟢 组合夏普 {sharpe:.3f}，策略表现良好")
    elif sharpe is not None and sharpe < -0.5:
        advice.append(f"🔴 组合夏普 {sharpe:.3f}，策略整体亏损，需要全面复盘信号源质量")


def _exit_lines(summary: dict) -> list[str]:
    if summary.get("exit_mode") == "atr":
        return [
            f"- ATR 周期: {summary.get('atr_period')}",
            f"- ATR 乘数: {summary.get('atr_multiplier')}",
            f"- ATR 极限止损: {fmt_metric(summary.get('atr_hard_stop_pct'), 1)}%",
            f"- 最大持有天数: {summary.get('atr_max_hold_days') or 120}（安全网）",
        ]
    return [
        f"- 止损线: {fmt_metric(summary.get('stop_loss_pct'), 1)}%",
        f"- 止盈线: {fmt_metric(summary.get('take_profit_pct'), 1)}%",
    ]


def _notes(summary: dict) -> list[str]:
    notes = [
        "- 该回测使用日线数据（qfq），含 T+1 与涨跌停成交约束（一字板不可成交）。",
        _entry_price_note(summary),
        _cost_note(summary),
        "- ⚠️ 仍存在幸存者偏差：股票池来自当前在市样本，未包含历史退市股票。",
        _meta_note(summary),
    ]
    if summary.get("wbt_requested"):
        notes.extend(
            [
                "- wbt 为 MIT License 的可选权重回测后端；当前实现不 vendoring 其源码，仅在本机/CI 已安装 wbt 时导入使用。",
                "- wbt 辅助指标基于 legacy NAV 的合成权重序列，主要用于高性能统计与报告交叉校验；交易执行真值仍以本回测器的 T+1/止损/止盈/涨跌停回放为准。",
            ]
        )
    return notes


def _entry_price_note(summary: dict) -> str:
    mode = str(summary.get("entry_price_mode") or "open")
    if mode == "close":
        return "- 入场口径：信号日收盘后出信号，T+1 收盘价买入（跳过一字涨停日）。"
    if mode != "tail_1455":
        return "- 入场口径：信号日收盘后出信号，T+1 开盘价买入（跳过一字涨停日）。"
    counts = summary.get("entry_price_source_counts") or {}
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    skipped = int(summary.get("entry_price_missing_skipped") or 0)
    if skipped:
        parts.append(f"missing_skip={skipped}")
    source_text = "；实际来源：" + "，".join(parts) if parts else ""
    fallback = str(summary.get("entry_price_fallback") or "close")
    return f"- 入场口径：信号日收盘后出信号，T+1 14:55 分钟线价格买入（跳过一字涨停日，fallback={fallback}{source_text}）。"


def _cost_note(summary: dict) -> str:
    if not summary.get("cash_portfolio_enabled"):
        return (
            "- 已纳入成交摩擦成本（买入 "
            f"{fmt_metric(summary.get('buy_friction_pct'), 3)}% / 卖出 "
            f"{fmt_metric(summary.get('sell_friction_pct'), 3)}%）；累计收益走单利（cumsum）口径。"
        )
    return (
        "- 现金账户口径：成交价已纳入买入 "
        f"{fmt_metric(summary.get('cash_portfolio_buy_friction_pct'), 3)}% / 卖出 "
        f"{fmt_metric(summary.get('cash_portfolio_sell_friction_pct'), 3)}% 摩擦，另计双边佣金率 "
        f"{fmt_metric(float(summary.get('cash_portfolio_commission_rate') or 0) * 10000, 2)} / 万"
        f"（最低 {fmt_metric(summary.get('cash_portfolio_min_commission'), 2)} 元）、双边过户费 "
        f"{fmt_metric(float(summary.get('cash_portfolio_transfer_fee_rate') or 0) * 10000, 3)} / 万，"
        f"卖出单边印花税 {fmt_metric(float(summary.get('cash_portfolio_stamp_duty_rate') or 0) * 10000, 2)} / 万。"
    )


def _cash_style_summaries(summary: dict) -> list[dict]:
    rows = summary.get("cash_portfolio_style_summaries")
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    return [summary] if summary.get("cash_portfolio_enabled") else []


def _style_display(row: dict) -> str:
    style = str(row.get("cash_portfolio_style") or "slot_equal_4")
    return str(row.get("cash_portfolio_style_label") or STYLE_LABELS.get(style, style))


def _cash_style_skipped(row: dict) -> int:
    keys = (
        "cash_portfolio_skipped_full",
        "cash_portfolio_skipped_cash",
        "cash_portfolio_skipped_duplicate",
        "cash_portfolio_skipped_weight_cap",
        "cash_portfolio_skipped_not_stronger",
    )
    return sum(int(row.get(key) or 0) for key in keys)


def _stat_values(stat: dict, keys: tuple[str, ...]) -> list[str]:
    digits = {
        "trades": 0,
        "win_rate_pct": 2,
        "avg_ret_pct": 3,
        "stop_exit_rate_pct": 2,
        "avg_mfe_pct": 3,
        "avg_mae_pct": 3,
    }
    return [fmt_metric(stat.get(key), digits.get(key, 3)) for key in keys]


def _top_n_line(summary: dict) -> str:
    return (
        f"- 每日候选上限: Top {summary.get('top_n')}"
        if summary.get("ai_top_n_cap") is not None
        else "- 每日候选上限: 不限（回测全量 AI 输入）"
    )


def _meta_mode(summary: dict) -> str:
    source = _metadata_source(summary)
    if source == "snapshot":
        return "current_snapshot (⚠️ look-ahead bias)"
    if source == "current":
        return "current_network (⚠️ look-ahead bias)"
    if source.endswith("_no_heat"):
        return "static_meta_no_concept_heat (bias-reduced)"
    return "disabled_current_snapshot_filters (bias-reduced)"


def _meta_note(summary: dict) -> str:
    source = _metadata_source(summary)
    if source in {"snapshot", "current"}:
        return (
            "- ⚠️ 市值/行业映射采用当前截面，会引入 look-ahead bias （市值穿越与行业漂移）；该结果仅用于参数方向验证。"
        )
    if source.endswith("_no_heat"):
        return (
            "- 保留市值/行业/概念归属（缓变属性，轻微偏差）但已剔除 concept_heat 题材热度快照，"
            "避免主线引擎预知未来热点；仍存在当前股票池幸存者偏差。"
        )
    return "- 本次按正式回测默认口径关闭当前截面市值/行业/概念映射，降低前视偏差；仍存在当前股票池幸存者偏差。"


def _metadata_source(summary: dict) -> str:
    source = str(summary.get("metadata_source") or "").strip().lower()
    if source:
        return source
    return "current" if summary.get("use_current_meta") else "disabled"


def _trailing_line(summary: dict) -> str:
    if summary.get("trailing_stop_pct", 0) >= 0:
        return "- 移动止盈: 关闭"
    return f"- 移动止盈: {fmt_metric(summary.get('trailing_stop_pct'), 1)}%（从最高点回撤，浮盈≥{fmt_metric(summary.get('trailing_activate_pct'), 1)}%后激活）"


def _entry_price_mode_line(summary: dict) -> str:
    return f"- 入场价格模式: {summary.get('entry_price_mode')}" + (
        f" @ {summary.get('entry_price_time')}" if summary.get("entry_price_time") else ""
    )


def _metrics_engine_line(summary: dict) -> str:
    suffix = (
        "（wbt 可用）"
        if summary.get("wbt_available") is True
        else ("（wbt 未启用）" if not summary.get("wbt_requested") else "（wbt 不可用，已保留 legacy 指标）")
    )
    return f"- 绩效引擎: {summary.get('metrics_engine', 'legacy')}{suffix}"


def _style_text(summary: dict) -> str:
    return (
        "、".join(f"{_style_display(row)}({row.get('cash_portfolio_style')})" for row in _cash_style_summaries(summary))
        or "-"
    )
