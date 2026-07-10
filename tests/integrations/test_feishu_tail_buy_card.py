from __future__ import annotations

from utils import feishu


def _sample_tail_buy_report() -> str:
    lines = [
        "⏰ Tail Buy 2026-04-27 16:05:42",
        "",
        "- 候选来源: signal_pending + recommendation_tracking (signal_date/recommend_date=2026-04-24; rec_only=0)",
        "- 扫描数量: 80",
        "- 分层结果: BUY=6 / WATCH=13 / SKIP=61",
        "- AI 二判: 14/19",
        "- 归因调权: lps×0.50↓（远端, 报告=2026-07-04, 周期=h5, 范围=尾盘+漏斗shadow）",
        "- 总耗时: 57.9s",
        "",
        "⚠️ 风险提醒: UNKNOWN/NORMAL（常态） | 风险提示文案",
        "",
        "## 持仓动作建议（硬止损/结构减仓/洗盘观察）",
        "- 持仓来源: portfolio=USER_LIVE:demo, state_sig=abc",
        "- 持仓数量: 3",
        "- 动作分布: ADD=0 / TRIM（硬止损/确认破位）=2 / 洗盘观察=0 / 弱势待确认=0 / HOLD=1",
        "",
        "### ADD（可考虑加仓）",
        "- 无",
        "",
        "### TRIM（硬止损/确认破位，优先处理）",
        "- 300613 富瀚微 | 持仓=100股 | 现价=60.21 | 浮盈=-6.2%",
        "",
        "### HOLD（结构中性持有观察）",
        "- 300590 移为通信 | 持仓=2100股 | 现价=14.02 | 浮盈=-4.5%",
        "",
        "## BUY（可执行买入）",
        "- 603060 国检集团 | priority=112.0 | rule=BUY(100.0)",
        "",
        "## BUY（观察买入：高位动能默认不买）",
        "- 600378 昊华科技 | priority=103.2 | rule=BUY(96.0) | 高位动能票，仅观察买入，默认不买",
        "",
        "## WATCH（观察买入）",
        "- 600985 淮北矿业 | priority=84.6 | rule=BUY(81.6)",
        "",
        "## SKIP（禁止新仓/暂不买）",
    ]
    for i in range(15):
        lines.append(f"- 600{i:03d} 示例{i} | priority=1.{i} | rule=SKIP(2.{i})")
    lines.extend(["", "说明：本任务仅输出尾盘扫描建议，不生成订单，不写入交易表。"])
    return "\n".join(lines)


def test_send_tail_buy_card_uses_rich_card_and_keeps_full_items_by_default(monkeypatch):
    captured = {}

    def fake_post_rich_card(webhook_url: str, title: str, elements: list, template: str = "blue"):
        captured["webhook_url"] = webhook_url
        captured["title"] = title
        captured["elements"] = elements
        captured["template"] = template
        return True, "ok"

    monkeypatch.setattr(feishu, "_post_rich_card", fake_post_rich_card)

    ok = feishu.send_tail_buy_card(
        webhook_url="https://example.com/hook",
        title="⏰ Tail Buy 2026-04-27",
        content=_sample_tail_buy_report(),
    )
    assert ok is True
    assert captured["template"] == "blue"

    body_text = "\n".join(
        str(el.get("text", {}).get("content", "")) for el in captured["elements"] if isinstance(el, dict)
    )
    assert "持仓动作建议（硬止损/结构减仓/洗盘观察）" in body_text
    assert "BUY（可执行买入）" in body_text
    assert "归因调权：lps×0.50↓（远端, 报告=2026-07-04, 周期=h5, 范围=尾盘+漏斗shadow）" in body_text
    assert "BUY（观察买入：高位动能默认不买）" in body_text
    assert "600378 昊华科技" in body_text
    assert "WATCH（观察买入）" in body_text
    assert "SKIP（禁止新仓/暂不买）" in body_text
    assert "600014 示例14" in body_text
    assert "其余" not in body_text


def test_tail_buy_card_supports_post_close_review_sections(monkeypatch):
    captured = {}

    def fake_post_rich_card(webhook_url: str, title: str, elements: list, template: str = "blue"):
        captured["elements"] = elements
        return True, "ok"

    monkeypatch.setattr(feishu, "_post_rich_card", fake_post_rich_card)

    content = "\n".join(
        [
            "📋 盘后尾盘复核 2026-07-04 16:05:42",
            "",
            "- 候选来源: signal_pending（signal_date=2026-07-04）",
            "- 扫描数量: 3",
            "- 分层结果: BUY=1 / WATCH=1 / SKIP=1",
            "- AI 二判: 0/0",
            "- 归因调权: lps×0.50↓（远端, 范围=尾盘+漏斗shadow）",
            "- 总耗时: 12.0s",
            "",
            "## BUY（明日观察买入）",
            "- 603713 密尔克卫 | priority=100.0 | rule=BUY(100.0)",
            "",
            "## WATCH（明日观察）",
            "- 600611 大众交通 | priority=88.9 | rule=WATCH(76.9)",
            "",
            "## SKIP（明日放弃）",
            "- 300956 英力股份 | priority=20.0 | rule=SKIP(20.0)",
            "",
            "说明：BUY=明日观察买入；WATCH=继续观察；SKIP=明日放弃。本任务不生成订单，不写入交易表。",
        ]
    )

    ok = feishu.send_tail_buy_card("https://example.com/hook", "盘后复核", content)

    assert ok is True
    body_text = "\n".join(
        str(el.get("text", {}).get("content", "")) for el in captured["elements"] if isinstance(el, dict)
    )
    assert "📋 盘后尾盘复核 2026-07-04 16:05:42" in body_text
    assert "BUY（明日观察买入）" in body_text
    assert "WATCH（明日观察）" in body_text
    assert "SKIP（明日放弃）" in body_text
    assert "BUY（可执行买入）" not in body_text
    assert "归因调权：lps×0.50↓（远端, 范围=尾盘+漏斗shadow）" in body_text
