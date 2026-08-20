"""港美漏斗都应推送飞书；此前只给港股发是历史遗留。"""

from __future__ import annotations

import pytest

from workflows import market_funnel_job as mfj
from workflows.market_funnel_report import render_market_funnel_report


def _result(market: str, label: str) -> dict:
    return {
        "ok": True,
        "market": market,
        "label": label,
        "metrics": {"total_hits": 2},
        "top_candidates": [{"symbol": "AAPL.US", "name": "Apple", "score": 88.5, "triggers": ["sos"]}],
    }


def test_us_report_renders_without_hk_only_section() -> None:
    """港股专属的风险剔除章节在美股 result 上必须整段消失，而不是渲染成空表。"""
    report = render_market_funnel_report(_result("us", "美股"))

    assert "美股" in report
    assert "港股风险剔除" not in report
    assert "AAPL.US" in report


def test_hk_report_keeps_risk_block() -> None:
    payload = _result("hk", "港股")
    payload["metrics"]["hk_risk_blocked"] = {"08888.HK": "仙股"}

    report = render_market_funnel_report(payload)

    assert "港股风险剔除" in report
    assert "08888.HK" in report


@pytest.mark.parametrize("market", ["hk", "us"])
def test_run_market_funnel_notifies_both_markets(monkeypatch: pytest.MonkeyPatch, market: str) -> None:
    """回归：美股此前被 `if runtime.spec.key == "hk"` 挡住，跑完不推送。

    走真实的 run_market_funnel 调用路径，把取数与落盘都打桩——只验证"推送这一步会发生"，
    否则直接调 send_* 等于只测了 mock 自己。
    """
    sent: list[dict] = []
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(mfj, "_require_tickflow_client", lambda: object())
    monkeypatch.setattr(mfj, "load_market_symbols", lambda _p: ["AAPL.US"])
    monkeypatch.setattr(
        mfj,
        "fetch_market_inputs",
        lambda *_a, **_k: ({"AAPL.US": {}}, [{"symbol": "AAPL.US", "name": "Apple"}], None, "", {}, {}),
    )
    monkeypatch.setattr(mfj, "run_funnel_for_ranked", lambda *_a, **_k: ({"total_hits": 1}, []))
    monkeypatch.setattr(mfj, "write_market_funnel_output", lambda *_a, **_k: None)
    monkeypatch.setattr(mfj, "write_market_funnel_report", lambda *_a, **_k: None)
    monkeypatch.setattr(mfj, "write_tracking_candidates_if_enabled", lambda *_a, **_k: None)
    monkeypatch.setattr(mfj, "send_market_funnel_notification", lambda _url, result: sent.append(result) or True)

    mfj.run_market_funnel(market)

    assert len(sent) == 1, f"{market} 未触发飞书推送"
    assert sent[0]["market"] == market


def test_missing_webhook_is_skipped_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    """未配置 webhook 时只跳过，不能让漏斗任务整体失败。"""
    from workflows.market_funnel_report import send_market_funnel_notification

    assert send_market_funnel_notification("", _result("us", "美股")) is False
    assert "跳过飞书通知" in capsys.readouterr().out
