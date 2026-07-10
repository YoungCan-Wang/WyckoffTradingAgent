from __future__ import annotations

from cli.__main__ import _build_parser, _cmd_screen


def test_screen_parser_accepts_agent_screen_args() -> None:
    parser = _build_parser()

    args = parser.parse_args(["screen", "--board", "gem", "--limit", "25", "--style", "低吸", "--no-financial-metrics"])
    full_args = parser.parse_args(["screen", "--financial-metrics"])

    assert args.cmd == "screen"
    assert args.board == "gem"
    assert args.limit == 25
    assert args.style == "低吸"
    assert args.financial_metrics is False
    assert full_args.financial_metrics is True


def test_cmd_screen_delegates_to_agent_screen_tool(monkeypatch, capsys) -> None:
    from agents import screen_tools
    from integrations import local_db

    captured: dict = {}
    monkeypatch.setattr(local_db, "init_db", lambda: None)

    def fake_screen_stocks(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "board": "chinext",
            "scan_scope": {
                "scope": "bounded",
                "board": "chinext",
                "limit": 25,
                "total_scanned": 25,
                "financial_metrics": "skipped_quick_scan",
                "financial_metrics_count": 0,
            },
            "summary": {"total_scanned": 25, "report_candidates": 1},
            "selection_brief": {"headline": "本轮首选可进入 AI 研报复核: 300750 宁德时代"},
            "watch_candidates": [{"code": "000001", "name": "平安银行"}],
        }

    monkeypatch.setattr(screen_tools, "screen_stocks", fake_screen_stocks)

    args = _build_parser().parse_args(
        ["screen", "--board", "chinext", "--limit", "25", "--style", "trend", "--no-financial-metrics"]
    )
    _cmd_screen(args)

    out = capsys.readouterr().out
    assert captured == {
        "board": "chinext",
        "limit": 25,
        "style": "trend",
        "financial_metrics": False,
    }
    assert "扫描 25 只，研报候选 1 只，观察 1 只" in out
    assert "快扫: chinext 前25只，实际扫描25只，财务过滤: 快扫跳过" in out
    assert "本轮首选可进入 AI 研报复核: 300750 宁德时代" in out
