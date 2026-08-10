from __future__ import annotations

from utils.tool_result_preview import tool_result_brief_lines


def _isolate_local_db(monkeypatch, tmp_path):
    from integrations import local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "portfolio.db")
    local_db.init_db()


def test_update_portfolio_batch_updates_multiple_local_positions(monkeypatch, tmp_path):
    from agents import portfolio_tools

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(
        portfolio_tools,
        "code_to_name",
        lambda code: {"002648": "卫星化学", "300628": "亿联网络"}.get(code, code),
    )

    result = portfolio_tools.update_portfolio(
        action="update",
        items=[
            {"code": "002648", "name": "卫星化学", "shares": 600, "cost_price": 17.9},
            {"code": "300628", "name": "亿联网络", "shares": 100, "cost_price": 29.47},
        ],
    )

    assert result.get("success") is True
    assert result["updated_count"] == 2
    assert result["failed_count"] == 0
    assert result["position_count"] == 2
    assert any("002648" in line and "17.9" in line for line in result["positions_summary"])


def test_update_portfolio_batch_reports_partial_failures(monkeypatch, tmp_path):
    from agents import portfolio_tools

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)

    result = portfolio_tools.update_portfolio(
        action="update",
        items=[
            {"code": "600018", "name": "上港集团", "shares": 1100, "cost_price": 5.5},
            {"code": "123", "name": "无效", "shares": 1, "cost_price": 1},
        ],
    )

    assert result.get("success") is True
    assert result["updated_count"] == 1
    assert result["failed_count"] == 1
    assert result["failures"][0]["code"] == "123"


def test_update_portfolio_batch_rejects_empty_items():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="update", items=[])
    assert "非空" in result["error"]


def test_update_portfolio_brief_lines_for_batch():
    lines = tool_result_brief_lines(
        "update_portfolio",
        {
            "success": True,
            "message": "批量update成功 2 只",
            "updated_count": 2,
            "failed_count": 0,
            "position_count": 7,
            "free_cash": 43442.31,
        },
    )
    assert lines[0].startswith("批量调仓: 成功2只")
    assert "持仓7只" in lines[1]
