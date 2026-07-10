from __future__ import annotations

from utils import feishu


def test_send_backtest_card_builds_rich_card(monkeypatch, tmp_path):
    captured = {}
    summary = tmp_path / "summary.md"
    summary.write_text(
        "\n".join(
            [
                "## 参数",
                "- 区间: 2026-02-01 ~ 2026-06-20",
                "- 每日候选上限: 4",
                "- 股票池: all",
                "- 持有周期: 5日",
                "- 止损线: -6%",
                "- 止盈线: +12%",
                "- 移动止盈: on",
                "- 胜率: 55.5%",
                "- 平均收益: 2.34%",
                "- 夏普比: 1.234",
                "- 最大回撤: 8.9%",
                "- 成交样本: 42",
                "",
                "## Trend vs Accum",
                "| 指标 | Trend | Accum |",
                "|--|--|--|",
                "| 成交笔数 | 10 | 32 |",
                "| 胜率(%) | 60.0 | 54.1 |",
                "| 平均收益(%) | 3.1 | 1.8 |",
                "| 夏普比 | 1.5 | 1.1 |",
                "| 最长连亏 | 2 | 3 |",
                "",
                "## 按大盘水温",
                "| 指标 | RISK_ON | RISK_OFF |",
                "|--|--|--|",
                "| 成交笔数 | 20 | 22 |",
                "| 胜率(%) | 65.0 | 45.0 |",
                "| 平均收益(%) | 4.2 | -0.8 |",
            ]
        ),
        encoding="utf-8",
    )

    def fake_post_rich_card(webhook_url: str, title: str, elements: list, template: str = "blue"):
        captured["webhook_url"] = webhook_url
        captured["title"] = title
        captured["elements"] = elements
        captured["template"] = template
        return True, "ok"

    monkeypatch.setattr(feishu, "_post_rich_card", fake_post_rich_card)

    ok = feishu.send_backtest_card("https://example.com/hook", str(summary))

    assert ok is True
    assert captured["title"] == "📊 Backtest 回测报告"
    assert captured["template"] == "blue"
    body = str(captured["elements"])
    assert "2026-02-01 ~ 2026-06-20" in body
    assert "Trend" in body
    assert "RISK_ON" in body
    assert "1.234" in body
