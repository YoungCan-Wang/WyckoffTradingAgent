from __future__ import annotations

import agents.slow_bull_tools as slow_bull_tools
from agents.tool_context import ToolContext
from cli.tools import TOOL_SCHEMAS, TOOL_SPECS


def test_scan_slow_bull_registered_in_schemas_and_specs() -> None:
    schema = next((item for item in TOOL_SCHEMAS if item["name"] == "scan_slow_bull"), None)
    assert schema is not None
    spec = TOOL_SPECS.get("scan_slow_bull")
    assert spec is not None
    assert spec.requires_approval is False
    assert spec.background is True


def test_scan_slow_bull_normalizes_args_and_records_handoff(monkeypatch) -> None:
    captured = {}

    def _fake_run(request):
        captured["request"] = request
        return {
            "ok": True,
            "job_kind": "slow_bull_scan",
            "mode": "targeted_diagnosis",
            "trade_date": "2026-11-25",
            "research_only": True,
            "new_buy_allowed": False,
            "counts": {"emerge_count": 0, "pullback_count": 1, "watch_count": 0},
            "climate": {"tech_rotation_warning": False},
            "result_summary": "慢牛扫描(2026-11-25): 阶段B回踩=1(600887)",
            "emerge_candidates": [],
            "pullback_candidates": [{"code": "600887"}],
            "watch_candidates": [],
        }

    import workflows.slow_bull_scan as workflow_mod

    monkeypatch.setattr(workflow_mod, "run_slow_bull_scan", _fake_run)
    monkeypatch.setattr(slow_bull_tools, "ensure_tushare_token", lambda *_a, **_k: None)

    ctx = ToolContext(state={})
    result = slow_bull_tools.scan_slow_bull(
        codes="600887.SH, 300498",
        board="主板",
        as_of_date="2026-11-25",
        allow_cycle_cashflow_bypass=True,
        tool_context=ctx,
    )

    assert result["ok"] is True
    assert captured["request"].codes == ("600887", "300498")
    assert captured["request"].board == "main"
    assert str(captured["request"].as_of_date) == "2026-11-25"
    assert captured["request"].allow_cycle_cashflow_bypass is True
    assert ctx.state["last_slow_bull_scan"]["pullback_codes"] == ["600887"]
