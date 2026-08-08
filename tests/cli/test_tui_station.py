from __future__ import annotations

from cli.tui import _tool_result_view
from cli.tui_station import (
    format_confirm_summary,
    is_confirm_timeout_error,
    parse_portfolio_edit_line,
    portfolio_action_title,
    portfolio_diagnosis_station_lines,
    short_model_label,
    status_hotkey_legend,
    thinking_preview_line,
    tool_branch_lines,
    tool_done_header,
    tool_running_line,
    user_echo_prefix,
    welcome_brief_lines,
)


def test_short_model_label_strips_provider_path_and_suffix() -> None:
    assert short_model_label("openai", "nvidia/nemotron-3-ultra-558b-a55b:free") == "nemotron-3-ultra-558b-a55b"
    assert short_model_label("deepseek", "deepseek-v4-flash") == "deepseek-v4-flash"


def test_portfolio_confirm_title_and_summary_use_human_labels() -> None:
    args = {
        "action": "add",
        "code": "06881.HK",
        "name": "中国银河",
        "shares": 1000,
        "cost_price": 7.68,
        "buy_dt": "20260807",
    }
    assert portfolio_action_title(args) == "新增持仓 · 中国银河 06881.HK"
    summary = format_confirm_summary("update_portfolio", args)
    assert "股数  1000" in summary
    assert "HK$7.68" in summary
    assert "20260807" in summary


def test_parse_portfolio_edit_line_supports_name_and_buy_dt() -> None:
    base = {"action": "add"}
    parsed = parse_portfolio_edit_line("06881.HK 1000 7.68 中国银河 2026-08-07", base)
    assert parsed["code"] == "06881.HK"
    assert parsed["shares"] == 1000
    assert parsed["cost_price"] == 7.68
    assert parsed["name"] == "中国银河"
    assert parsed["buy_dt"] == "20260807"


def test_thinking_preview_and_timeout_helpers() -> None:
    line = thinking_preview_line("The user is telling me they bought HK stock " + ("x" * 100))
    assert line.startswith("思考中 · ")
    assert len(line) < 90
    assert is_confirm_timeout_error("操作 [update_portfolio] 确认弹窗等待超时——这不是拒绝。")


def test_welcome_brief_and_diagnosis_station_card() -> None:
    lines = welcome_brief_lines(version="0.9.254", position_count=7, free_cash=40000, model_label="deepseek-v4-flash")
    assert lines[0].startswith("Wyckoff Station")
    assert "持仓 7 只" in lines[1]
    assert "06881.HK" in lines[3]

    card = portfolio_diagnosis_station_lines(
        {
            "position_count": 2,
            "total_assets": 106597,
            "total_market_value": 100000,
            "diagnostics": [
                {
                    "code": "06881.HK",
                    "name": "中国银河",
                    "health": "⚠警戒",
                    "pnl_pct": 1.0,
                    "market_value": 7780,
                    "weight_pct": 7.3,
                    "l2_channel": "主升通道",
                    "diagnosis_brief": {"next_step": "持有观察"},
                },
                {
                    "code": "002648",
                    "name": "卫星化学",
                    "health": "●健康",
                    "pnl_pct": 2.5,
                    "market_value": 20000,
                    "weight_pct": 20,
                },
            ],
        }
    )
    assert card[0].startswith("持仓体检")
    assert any("06881.HK" in line and "警戒" in line for line in card)
    assert any(line.startswith("  ") and "主升通道" in line for line in card)


def test_interaction_grammar_helpers() -> None:
    assert user_echo_prefix() == "你"
    assert tool_running_line("读持仓") == "◇ 读持仓…"
    assert tool_running_line("调仓操作", pending_confirm=True) == "◆ 调仓操作 待确认"
    assert tool_done_header("读持仓", 1.2).startswith("◆ 读持仓")
    assert tool_branch_lines(["持仓 7 只", "下一步: 体检"]) == [
        "  ├ 持仓 7 只",
        "  └ 下一步: 体检",
    ]
    assert "esc 中断" in status_hotkey_legend()


def test_tool_result_view_softens_confirm_timeout() -> None:
    summary, renderable = _tool_result_view(
        {
            "name": "update_portfolio",
            "args": {"action": "add"},
            "elapsed_ms": 120000,
            "result": {"error": "操作 [update_portfolio] 确认弹窗等待超时——这不是拒绝。"},
        },
        tools=None,
    )
    assert summary["status"] == "error"
    plain = renderable.plain if hasattr(renderable, "plain") else str(renderable)
    assert "确认超时" in plain
    assert "✗" not in plain


def test_tool_result_view_renders_portfolio_diagnosis_station_card() -> None:
    summary, renderable = _tool_result_view(
        {
            "name": "portfolio",
            "args": {"mode": "diagnose"},
            "elapsed_ms": 1200,
            "result": {
                "position_count": 1,
                "total_assets": 10000,
                "diagnostics": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "health": "●健康",
                        "pnl_pct": 1.2,
                        "market_value": 8000,
                        "weight_pct": 80,
                    }
                ],
            },
        },
        tools=None,
    )
    plain = renderable.plain if hasattr(renderable, "plain") else str(renderable)
    assert "◆" in plain
    assert "持仓体检" in plain
    assert "600519" in plain
    assert "└" in plain or "├" in plain
    assert summary.get("brief")
