from __future__ import annotations

import json

import pytest

from workflows.backtest_notification import (
    BacktestNotifyRequest,
    build_card,
    load_cells,
    run_backtest_notification,
    write_report,
)

_MARKET_TITLE = {"hk": "HK Backtest Grid 港股回测完成", "us": "US Backtest Grid 美股回测完成"}
_MARKET_HEADING = {"hk": "# HK Backtest Strategy Comparison", "us": "# US Backtest Strategy Comparison"}


def test_load_cells_reads_summary_json(tmp_path) -> None:
    summary_dir = tmp_path / "period" / "strategy"
    summary_dir.mkdir(parents=True)
    (summary_dir / "summary.json").write_text(
        json.dumps(
            {
                "period_key": "recent",
                "period_label": "最近",
                "start": "2026-01-01",
                "end": "2026-02-01",
                "strategy_id": "s1",
                "strategy_name": "策略1",
                "strategy_desc": "开盘买入",
                "trades": 5,
                "win_rate_pct": 60,
                "avg_ret_pct": 2.5,
                "max_drawdown_pct": -4,
                "sharpe_ratio": 0.8,
                "portfolio_total_ret_pct": 12,
            }
        ),
        encoding="utf-8",
    )

    cells = load_cells(tmp_path)

    assert len(cells) == 1
    assert cells[0].strategy_name == "策略1"
    assert cells[0].sharpe == 0.8


@pytest.mark.parametrize("market", ["hk", "us"])
def test_build_card_includes_best_strategy(market) -> None:
    cells = load_cells(_summary_fixture())

    card = build_card(cells, market=market, run_url="https://github.example/run", top_n="2")

    content = json.dumps(card, ensure_ascii=False)
    assert _MARKET_TITLE[market] in content
    assert "最优策略" in content
    assert "策略2" in content


@pytest.mark.parametrize("market", ["hk", "us"])
def test_write_report_outputs_strategy_table(market, tmp_path) -> None:
    cells = load_cells(_summary_fixture())
    output = tmp_path / "report.md"

    write_report(output, cells, market=market, run_url="run", top_n="2")

    report = output.read_text(encoding="utf-8")
    assert _MARKET_HEADING[market] in report
    assert "| 策略 | 说明 | 夏普 | 胜率 | 均收 | 回撤 | 样本 |" in report
    assert "策略2" in report


@pytest.mark.parametrize("market", ["hk", "us"])
def test_run_backtest_notification_writes_report_and_sends_card(market, monkeypatch, tmp_path) -> None:
    sent: list[dict] = []
    output = tmp_path / "report.md"
    monkeypatch.setattr(
        "workflows.backtest_notification.send_feishu",
        lambda webhook, payload: sent.append({"webhook": webhook, "payload": payload}),
    )

    result = run_backtest_notification(
        BacktestNotifyRequest(
            market=market,
            artifacts_dir=str(_summary_fixture()),
            output=str(output),
            run_url="run",
            top_n="2",
            webhook_url="https://feishu.example",
        )
    )

    assert result == 0
    assert output.exists()
    assert sent[0]["webhook"] == "https://feishu.example"
    assert _MARKET_TITLE[market] in json.dumps(sent[0]["payload"], ensure_ascii=False)


def _summary_fixture():
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    for name, sharpe in [("策略1", 0.2), ("策略2", 0.8)]:
        strategy_dir = root / "recent" / name
        strategy_dir.mkdir(parents=True)
        (strategy_dir / "summary.json").write_text(
            json.dumps(
                {
                    "period_key": "recent",
                    "period_label": "最近",
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                    "strategy_id": name,
                    "strategy_name": name,
                    "strategy_desc": "说明",
                    "trades": 10,
                    "win_rate_pct": 50,
                    "avg_ret_pct": 1.2,
                    "max_drawdown_pct": -3,
                    "sharpe_ratio": sharpe,
                }
            ),
            encoding="utf-8",
        )
    return root
