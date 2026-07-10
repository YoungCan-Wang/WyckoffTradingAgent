from __future__ import annotations

import pytest

from core.holding_diagnostic import HoldingDiagnostic
from workflows import diagnose_holdings_cli as workflow


def test_parse_inline_holdings_accepts_optional_names() -> None:
    holdings = workflow._parse_inline_holdings("300750,600519", "200.5,1500", "宁德时代")

    assert holdings == [("300750", "宁德时代", 200.5), ("600519", "--", 1500.0)]


def test_parse_inline_holdings_rejects_mismatched_costs() -> None:
    with pytest.raises(SystemExit):
        workflow._parse_inline_holdings("300750,600519", "200.5")


def test_format_text_includes_health_summary() -> None:
    diagnostics = [
        HoldingDiagnostic(
            code="300750", name="宁德时代", cost=100.0, latest_close=110.0, pnl_pct=10.0, health="🟢健康"
        ),
        HoldingDiagnostic(code="600519", name="贵州茅台", cost=100.0, latest_close=96.0, pnl_pct=-4.0, health="🟡警戒"),
        HoldingDiagnostic(
            code="000001", name="平安银行", cost=100.0, latest_close=90.0, pnl_pct=-10.0, health="🔴危险"
        ),
    ]

    text = workflow._format_text(diagnostics)

    assert "健康 1" in text
    assert "警戒 1" in text
    assert "危险 1" in text
    assert "平均盈亏: -1.33%" in text
