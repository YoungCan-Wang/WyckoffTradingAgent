from utils.feishu_report_card import build_report_card_elements, report_card_template


def test_generic_report_card_builds_intro_sections_and_warning_note():
    elements = build_report_card_elements(
        "行情 2026-07-11  扫描 1230 只\n\n"
        "**主线定位**\n"
        "- 光模块 / 分歧机会 / 主线核心\n\n"
        "**WATCH（观察）**\n"
        "- 300308 中际旭创\n\n"
        "⚠️ 风险提醒: confirmed 不等于 BUY"
    )

    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "grey"
    headings = [item for item in elements if item.get("tag") == "div" and "**" in item["text"]["content"]]
    assert headings[0]["text"]["content"] == "📊 **主线定位**"
    assert headings[1]["text"]["content"] == "🟡 **WATCH（观察）**"
    assert elements[-1]["tag"] == "note"


def test_generic_report_card_selects_semantic_header_color():
    assert report_card_template("盘前风控", "RISK_OFF 禁止新开") == "red"
    assert report_card_template("任务跳过", "非交易日") == "orange"
    assert report_card_template("回测完成", "全部通过") == "green"
    assert report_card_template("AI 研报", "市场复盘") == "purple"
    assert report_card_template("定时任务", "正常运行") == "blue"


def test_post_card_uses_wide_semantic_rich_layout(monkeypatch):
    from utils import feishu

    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"code": 0}

    def fake_post(_url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(feishu.requests, "post", fake_post)

    ok, _ = feishu._post_card("https://example.test/hook", "盘前风控", "**风险提醒**\nRISK_OFF 禁止新开")

    assert ok is True
    card = captured["card"]
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "red"
    assert len(card["elements"]) > 1


def test_card_chunk_falls_back_to_legacy_layout(monkeypatch):
    from utils import feishu

    calls = []
    monkeypatch.setattr(feishu.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(feishu, "_post_card", lambda *_args: calls.append("rich") or (False, "bad_card"))
    monkeypatch.setattr(feishu, "_post_legacy_card", lambda *_args: calls.append("legacy") or (True, "ok"))

    assert feishu._send_card_chunk("hook", "title", "body", 1, 1) is True
    assert calls == ["rich", "rich", "rich", "legacy"]
