from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from agents import diagnosis_tools, portfolio_tools
from agents.tool_context import ToolContext


def _diagnostic(**overrides):
    defaults = {
        "code": "002326",
        "name": "永太科技",
        "cost": 20.0,
        "health": "🟢健康",
        "pnl_pct": 0.0,
        "latest_close": 25.62,
        "ma5": 24.0,
        "ma20": 23.0,
        "ma50": 21.0,
        "ma200": 18.0,
        "ma200_bias_pct": 42.3,
        "ma_pattern": "多头排列",
        "l2_channel": "主升通道",
        "track": "Trend",
        "accum_stage": "",
        "l4_triggers": [],
        "candidate_lane": "wyckoff_structure",
        "candidate_entry_type": "SOS",
        "candidate_score": 83.04,
        "exit_signal": "",
        "exit_price": None,
        "exit_reason": "",
        "stop_loss_7pct": 18.6,
        "stop_loss_status": "",
        "take_profit_18pct": 23.6,
        "take_profit_status": "",
        "target_conservative": None,
        "target_aggressive": None,
        "vol_ratio_20_60": 1.26,
        "range_60d_pct": 39.0,
        "ret_10d_pct": 10.4,
        "ret_20d_pct": 16.7,
        "from_year_high_pct": -22.4,
        "from_year_low_pct": 124.7,
        "day_change_pct": 0.0,
        "limit_move_desc": "",
        "intraday_path": "",
        "intraday_path_desc": "",
        "health_reasons": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_analyze_stock_price_returns_price_records(monkeypatch) -> None:
    rows = pd.DataFrame(
        [
            {
                "日期": "2026-06-18",
                "开盘": 10.0,
                "最高": 10.5,
                "最低": 9.9,
                "收盘": 10.2,
                "成交量": 1000,
                "涨跌幅": 2.0,
            }
        ]
    )
    rows.attrs["tickflow_limit_hint"] = "TickFlow fallback"

    def fake_get_stock_hist(code: str, start_date: date, end_date: date):
        assert code == "000001"
        assert start_date <= end_date
        return rows

    monkeypatch.setattr(diagnosis_tools, "ensure_tushare_token", lambda _ctx: None)
    monkeypatch.setattr("integrations.stock_hist_repository.get_stock_hist", fake_get_stock_hist)

    result = diagnosis_tools.analyze_stock("000001", mode="price", days=1)

    assert result["data_status"] == "ok"
    assert result["latest_close"] == 10.2
    assert result["data"][0]["close"] == 10.2
    assert result["tickflow_limit_hint"] == "TickFlow fallback"


def test_analyze_stock_price_sanitizes_bad_ohlcv(monkeypatch) -> None:
    rows = pd.DataFrame(
        [
            {
                "日期": "2026-06-18",
                "开盘": "bad",
                "最高": float("inf"),
                "最低": float("-inf"),
                "收盘": float("nan"),
                "成交量": "bad",
                "涨跌幅": float("nan"),
            }
        ]
    )

    monkeypatch.setattr(diagnosis_tools, "ensure_tushare_token", lambda _ctx: None)
    monkeypatch.setattr("integrations.stock_hist_repository.get_stock_hist", lambda *_args, **_kwargs: rows)

    result = diagnosis_tools.analyze_stock("000001", mode="price", days=1)

    assert result["latest_close"] is None
    assert result["data"][0] == {
        "date": "2026-06-18",
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": 0,
        "pct_chg": None,
    }


def test_analyze_stock_rejects_unknown_mode(monkeypatch) -> None:
    monkeypatch.setattr(diagnosis_tools, "ensure_tushare_token", lambda _ctx: None)

    result = diagnosis_tools.analyze_stock("000001", mode="x")

    assert "mode 参数无效" in result["error"]


def test_diagnostic_payload_marks_high_score_trend_as_priority_watch() -> None:
    result = diagnosis_tools._diagnostic_payload(
        _diagnostic(health_reasons=["多头排列", "L2通道:主升通道"]),
        "formatted",
        "2026-07-03",
        {},
    )

    brief = result["diagnosis_brief"]

    assert brief["status"] == "priority_watch"
    assert brief["label"] == "重点观察"
    assert brief["headline"] == "重点观察: 002326 永太科技"
    assert brief["direct_buy_allowed"] is False
    assert "多头排列" in brief["strengths"]
    assert "L2通道: 主升通道" in brief["strengths"]
    assert "候选车道: SOS(83.0)" in brief["strengths"]
    assert brief["risks"] == []
    assert "市场闸门" in brief["next_step"]
    assert result["next_action"] == brief["next_step"]
    assert result["next_tool"] == {
        "tool": "generate_ai_report",
        "args": {"stock_codes": ["002326"]},
        "reason": "个股诊断进入重点/触发观察，可生成 AI 研报复核；不直接触发买入",
    }


def test_diagnostic_payload_marks_stop_loss_as_avoid() -> None:
    result = diagnosis_tools._diagnostic_payload(
        _diagnostic(
            code="002628",
            name="成都路桥",
            health="🔴危险",
            l4_triggers=["EVR"],
            exit_signal="stop_loss",
            health_reasons=["结构止损（从高点回撤>10%）"],
        ),
        "formatted",
        "2026-07-03",
        {},
    )

    brief = result["diagnosis_brief"]

    assert brief["status"] == "avoid"
    assert brief["label"] == "回避"
    assert brief["headline"] == "回避: 002628 成都路桥"
    assert "L4触发: EVR" in brief["strengths"]
    assert brief["risks"] == ["结构止损（从高点回撤>10%）", "退出信号: stop_loss"]
    assert brief["next_step"].startswith("回避新增")
    assert result["next_tool"] == {}


def test_remember_stock_diagnosis_stores_compact_handoff() -> None:
    context = ToolContext()
    result = diagnosis_tools._diagnostic_payload(
        _diagnostic(health_reasons=["多头排列"], candidate_score=83.04),
        "formatted",
        "2026-07-03",
        {},
    )

    diagnosis_tools.remember_stock_diagnosis(context, result)

    handoff = context.state["last_stock_diagnosis"]
    latest = handoff["latest"]
    assert latest["code"] == "002326"
    assert latest["name"] == "永太科技"
    assert latest["action_status"] == "priority_watch"
    assert latest["status_label"] == "重点观察"
    assert latest["candidate_score"] == 83.04
    assert latest["new_buy_allowed"] is False
    assert "risk_factors" not in latest
    assert handoff["diagnosed_symbols"][0]["next_step"].startswith("加入重点观察")


def test_portfolio_diagnostic_payload_reuses_action_brief() -> None:
    result = portfolio_tools._diagnostic_payload(
        _diagnostic(
            code="002081",
            name="金螳螂",
            health="🔴危险",
            exit_signal="stop_loss",
            health_reasons=["结构止损（从高点回撤>10%）"],
        ),
        "2026-07-03",
        {"hist_rows": 250},
    )

    brief = result["diagnosis_brief"]

    assert result["ma_pattern"] == "多头排列"
    assert result["track"] == "Trend"
    assert result["exit_signal"] == "stop_loss"
    assert result["hist_rows"] == 250
    assert brief["status"] == "avoid"
    assert brief["headline"] == "回避: 002081 金螳螂"
    assert brief["direct_buy_allowed"] is False
    assert brief["risks"] == ["结构止损（从高点回撤>10%）", "退出信号: stop_loss"]
