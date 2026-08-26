from __future__ import annotations

from utils.tool_result_preview import tool_result_brief_lines


def _isolate_local_db(monkeypatch, tmp_path):
    from integrations import local_db

    local_db.reset_connection()
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "portfolio.db")
    local_db.init_db()


def test_update_portfolio_batch_updates_multiple_local_positions(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from integrations import local_db

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(
        portfolio_tools,
        "code_to_name",
        lambda code: {"002648": "卫星化学", "300628": "亿联网络"}.get(code, code),
    )

    added = portfolio_tools.update_portfolio(
        action="add",
        items=[
            {"code": "002648", "name": "卫星化学", "shares": 500, "cost_price": 17.0, "buy_dt": "2026-07-01"},
            {"code": "300628", "name": "亿联网络", "shares": 80, "cost_price": 28.0, "buy_dt": "20260702"},
        ],
    )
    assert added.get("success") is True

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
    dates = {row["code"]: row["buy_dt"] for row in local_db.load_portfolio("LOCAL")["positions"]}
    assert dates["002648"] == "2026-07-01"
    assert dates["300628"] == "20260702"


def test_update_portfolio_batch_does_not_create_missing_positions(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from core.buy_dt import POSITION_MISSING_ERROR
    from integrations import local_db

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)

    result = portfolio_tools.update_portfolio(
        action="update",
        items=[
            {"code": "002648", "name": "卫星化学", "shares": 600, "cost_price": 17.9},
            {"code": "300628", "name": "亿联网络", "shares": 100, "cost_price": 29.47},
        ],
    )

    assert result.get("success") is not True
    assert POSITION_MISSING_ERROR in result["error"]
    assert result["updated_count"] == 0
    state = local_db.load_portfolio("LOCAL")
    assert not state or not state.get("positions")


def test_update_portfolio_batch_reports_partial_failures(monkeypatch, tmp_path):
    from agents import portfolio_tools

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)

    added = portfolio_tools.update_portfolio(
        action="add",
        items=[{"code": "600018", "name": "上港集团", "shares": 1000, "cost_price": 5.0, "buy_dt": "2026-07-01"}],
    )
    assert added.get("success") is True

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


def test_update_portfolio_batch_add_rejects_invalid_buy_dt(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from core.buy_dt import INVALID_BUY_DT_ERROR
    from integrations import local_db

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)

    result = portfolio_tools.update_portfolio(
        action="add",
        items=[{"code": "600018", "name": "上港集团", "shares": 1100, "cost_price": 5.5, "buy_dt": "not-a-date"}],
    )
    assert result.get("success") is not True
    assert result["error"] == INVALID_BUY_DT_ERROR
    state = local_db.load_portfolio("LOCAL")
    assert not state or not state.get("positions")


def test_update_portfolio_batch_rejects_empty_items():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="update", items=[])
    assert "非空" in result["error"]


def test_update_portfolio_batch_remove_needs_only_code(monkeypatch, tmp_path):
    """remove 按 code 清仓，股数/成本无意义；要求它们会让批量清仓整批失败。"""
    from agents import portfolio_tools
    from integrations import local_db

    _isolate_local_db(monkeypatch, tmp_path)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)

    portfolio_tools.update_portfolio(
        action="add",
        items=[{"code": "600519", "name": "贵州茅台", "shares": 100, "cost_price": 1500, "buy_dt": "2026-07-01"}],
    )
    removed = portfolio_tools.update_portfolio(action="remove", items=[{"code": "600519"}])

    assert removed.get("success") is True, removed
    assert local_db.load_portfolio("LOCAL")["positions"] == []


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
