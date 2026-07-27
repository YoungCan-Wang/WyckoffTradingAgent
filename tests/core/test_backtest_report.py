from __future__ import annotations

from core.backtest_report import build_summary_md, generate_strategy_advice


def test_build_summary_md_renders_atr_cash_and_regime_advice() -> None:
    summary = {
        "start": "2026-01-01",
        "end": "2026-06-20",
        "hold_days": 15,
        "ai_top_n_cap": 4,
        "top_n": 4,
        "ai_selection_mode": "llm",
        "board": "all",
        "sample_size": 5000,
        "eval_days": 100,
        "signal_days": 20,
        "exit_mode": "atr",
        "atr_period": 14,
        "atr_multiplier": 2.5,
        "atr_hard_stop_pct": -8,
        "atr_max_hold_days": 20,
        "trailing_stop_pct": 0,
        "sltp_priority": "stop_first",
        "buy_friction_pct": 0.1,
        "sell_friction_pct": 0.1,
        "use_current_meta": False,
        "metadata_source": "disabled",
        "pending_mode": "confirmation",
        "regime_filter": True,
        "regime_filter_note": "deprecated_live_aligned_noop",
        "execution_regime_gate": "live",
        "regime_blocked_signal_days": 3,
        "regime_blocked_candidates": 7,
        "entry_price_mode": "open",
        "cash_portfolio_enabled": True,
        "cash_portfolio_style": "confirmation_only",
        "cash_portfolio_initial_cash": 100000,
        "cash_portfolio_max_positions": 4,
        "cash_portfolio_final_cash": 120000,
        "cash_portfolio_total_return_pct": 20,
        "cash_portfolio_max_drawdown_pct": -8,
        "cash_portfolio_trades": 30,
        "cash_portfolio_win_rate_pct": 45,
        "cash_portfolio_avg_profit_pct": 10,
        "cash_portfolio_avg_loss_pct": -5,
        "cash_portfolio_buy_friction_pct": 0.1,
        "cash_portfolio_sell_friction_pct": 0.1,
        "cash_portfolio_friction_total": 80,
        "cash_portfolio_trade_cost_total": 120,
        "cash_portfolio_commission_rate": 0.0003,
        "cash_portfolio_min_commission": 5,
        "cash_portfolio_stamp_duty_rate": 0.0005,
        "cash_portfolio_transfer_fee_rate": 0.00001,
        "signal_weight_map": {"lps|regime=RISK_ON|lane=trend_pullback": 0.5},
        "signal_weight_meta": {
            "source": "远端",
            "report_date": "2026-07-04",
            "horizon": "5",
            "age_days": 0,
            "execution_policy": "on",
            "active_scope": "尾盘+正式漏斗",
        },
        "trades": 30,
        "win_rate_pct": 42,
        "avg_ret_pct": 1.2,
        "median_ret_pct": 0.5,
        "q25_ret_pct": -3,
        "q75_ret_pct": 4,
        "sharpe_ratio": 0.8,
        "calmar_ratio": 1.5,
        "max_drawdown_pct": -12,
        "portfolio_ann_ret_pct": 30,
        "portfolio_total_ret_pct": 20,
        "portfolio_avg_positions": 3,
        "var95_ret_pct": -7,
        "cvar95_ret_pct": -8,
        "max_consecutive_losses": 4,
        "stratified": {
            "by_regime": {
                "RISK_OFF": {"avg_ret_pct": -2.0, "trades": 12},
            }
        },
    }

    md = build_summary_md(summary)

    assert "- 最大持有天数: 20（安全网）" in md
    assert "- 大盘水温仓控: 关闭（旧回测开关已废弃，跟随实盘漏斗候选口径）" in md
    assert "- 执行水温闸门: 实盘一致；拦截 3 个信号日 / 7 个候选" in md
    assert "## 真实现金账户模拟" in md
    assert "- 成交摩擦: 买入 0.100% / 卖出 0.100%；合计 80" in md
    assert "成交价已纳入买入 0.100% / 卖出 0.100% 摩擦" in md
    assert "RISK_OFF 环境下平均收益 -2.00%" in md
    assert "lps[regime=RISK_ON, lane=trend_pullback]×0.50↓" in md
    assert "- 元数据口径: disabled_current_snapshot_filters (bias-reduced)" in md
    assert "（远端, 报告=2026-07-04, 周期=h5, 距今=0天, 策略=正式调权(on), 范围=尾盘+正式漏斗）" in md


def test_build_summary_md_notes_close_entry_price_mode() -> None:
    md = build_summary_md({"entry_price_mode": "close"})

    assert "- 入场口径：信号日收盘后出信号，T+1 收盘价买入（跳过一字涨停日）。" in md


def test_build_summary_md_discloses_snapshot_metadata_source() -> None:
    md = build_summary_md({"metadata_source": "snapshot", "use_current_meta": True})

    assert "- 元数据口径: current_snapshot (⚠️ look-ahead bias)" in md
    assert "市值/行业映射采用当前截面" in md


def test_build_summary_md_renders_crash_probe_proxy_stats() -> None:
    md = build_summary_md(
        {
            "crash_probe_watch_candidates": 8,
            "crash_probe_proxy_qualified": 3,
            "crash_probe_staged_entries": 2,
            "crash_probe_confirmed_next_day": 1,
            "crash_probe_confirmation_rate_pct": 50.0,
            "crash_probe_probe_2pct_capital_return_pct": -0.12,
            "crash_probe_confirmed_add_3pct_capital_return_pct": 0.45,
            "crash_probe_staged_2_to_5pct_capital_return_pct": 0.33,
        }
    )

    assert "观察 8 / 硬条件 3 / Top1 入场 2 / 次日确认 1 (50.00%)" in md
    assert "权重收益和（研究诊断，非组合收益）" in md
    assert "仅2%试错 -0.120% / 确认加3% 0.450% / 合计 0.330%" in md


def test_generate_strategy_advice_returns_default_when_no_warning() -> None:
    assert generate_strategy_advice({}) == ["🟢 当前参数组合表现尚可，暂无强烈调整建议"]


def test_build_summary_md_renders_inactive_policy_meta() -> None:
    md = build_summary_md(
        {
            "signal_weight_map": {},
            "signal_weight_meta": {
                "source": "远端",
                "report_date": "2026-07-04",
                "horizon": "5",
                "execution_policy": "shadow",
                "active_scope": "尾盘+漏斗shadow",
                "formal_dynamic_allowed": False,
                "formal_dynamic_block_reason": "auto_apply=false",
            },
        }
    )

    assert (
        "- 策略治理调权: 未启用（远端, 报告=2026-07-04, 周期=h5, 策略=shadow 对照(shadow), 范围=尾盘+漏斗shadow, 正式dynamic=未进正式漏斗(未启用自动晋级)）"
        in md
    )
