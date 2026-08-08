"""财务指标 map 的主键契约：供应商后缀只存在于请求/响应边界，map 一律六位代码。"""

from __future__ import annotations

import pandas as pd
import pytest

from core.mainline_engine import _lookup_financial
from workflows.funnel_data import _load_financial_metrics
from workflows.funnel_data_quality import build_funnel_data_quality
from workflows.step3_candidates import _fetch_tickflow_financial_map


class _StubClient:
    """按 TickFlow 真实行为返回带后缀主键。"""

    def __init__(self, api_key: str = "test-key") -> None:
        self.api_key = api_key
        self.requested: list[str] = []

    def get_financial_metrics(self, symbols: list[str], *, latest: bool = True) -> dict[str, list[dict]]:
        self.requested = list(symbols)
        return {
            "300502.SZ": [{"roe": 14.52, "debt_to_asset_ratio": 31.04}],
            "601138.SH": [{"roe": 8.10, "debt_to_asset_ratio": 55.20}],
            "920001.BJ": [{"roe": 6.30, "debt_to_asset_ratio": 40.00}],
        }


@pytest.fixture
def _stub_tickflow(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    stub = _StubClient()
    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", lambda **_: stub)
    monkeypatch.setenv("TICKFLOW_API_KEY", "test-key")
    monkeypatch.delenv("FUNNEL_SKIP_FINANCIAL_METRICS", raising=False)
    return stub


def test_funnel_loader_keys_are_six_digit_codes(_stub_tickflow: _StubClient) -> None:
    result = _load_financial_metrics(["300502", "601138", "920001"])

    assert sorted(result) == ["300502", "601138", "920001"]
    assert result["300502"]["roe"] == 14.52
    assert result["920001"]["roe"] == 6.30


def test_step3_loader_keys_are_six_digit_codes(_stub_tickflow: _StubClient) -> None:
    items = [{"code": "300502"}, {"code": "601138"}, {"code": "920001"}]

    result = _fetch_tickflow_financial_map(items, "test-key")

    assert sorted(result) == ["300502", "601138", "920001"]


def test_coverage_counts_suffixed_vendor_response(_stub_tickflow: _StubClient) -> None:
    """回归：带后缀主键曾让覆盖率恒为 0，从而静默跳过财务闸门。"""
    symbols = ["300502", "601138"]
    financial_map = _load_financial_metrics(symbols)

    quality = build_funnel_data_quality(
        symbols,
        {sym: pd.DataFrame({"close": [10.0]}) for sym in symbols},
        {sym: 100.0 for sym in symbols},
        financial_map,
        financial_requested=True,
    )

    assert quality["counts"]["financial"] == 2
    assert quality["coverage"]["financial"] == 1.0


@pytest.mark.parametrize("code", ["300502", "601138", "920001", "830799", "430139", "688001"])
def test_mainline_lookup_resolves_every_board(code: str) -> None:
    """北交所 4/8/9 开头此前被猜成 .SH/.SZ，双键兼容对其全部失效。"""
    assert _lookup_financial({code: {"roe": 1.0}}, code) == {"roe": 1.0}
