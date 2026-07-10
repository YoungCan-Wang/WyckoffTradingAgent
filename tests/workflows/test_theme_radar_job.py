from __future__ import annotations


def test_run_theme_radar_uses_full_concept_heat(monkeypatch) -> None:
    from workflows import theme_radar_runtime as mod

    captured: dict = {}
    metrics = {
        "end_trade_date": "2026-05-27",
        "concept_heat": [{"name": "top20"}],
        "concept_heat_full": [{"name": "full"}],
        "all_df_map": {},
        "_debug": {},
    }
    monkeypatch.setattr(mod, "run_funnel_job", lambda include_debug_context: ({}, metrics))
    monkeypatch.setattr(mod, "_load_concept_history", lambda: {})
    monkeypatch.setattr(mod, "build_theme_radar_snapshot", lambda **kwargs: captured.update(kwargs) or {})

    mod.run_theme_radar(with_news=False, persist=False)

    assert captured["concept_heat"] == [{"name": "full"}]


def test_render_theme_radar_report_groups_candidates_by_theme() -> None:
    from workflows.theme_radar_report import render_theme_radar_report

    report = render_theme_radar_report(_snapshot())

    assert "## 按主题展开" in report
    assert "### 光模块 · 主线确认" in report
    assert "### 芯片半导体 · 萌芽观察" in report
    assert "000001" in report
    assert report.index("### 光模块") < report.index("000001")
    assert "## 战略观察池" not in report


def test_render_theme_radar_html_outputs_grouped_cards() -> None:
    from workflows.theme_radar_report import render_theme_radar_html

    html = render_theme_radar_html(_snapshot())

    assert "<html" in html
    assert "theme-card" in html
    assert "中长线主线雷达" in html
    assert "000001" in html
    assert "光模块龙头" in html


def test_notify_report_sends_feishu(monkeypatch) -> None:
    from workflows import theme_radar_runtime as mod

    captured: dict[str, str] = {}
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setattr(
        mod,
        "send_feishu_notification",
        lambda webhook, title, content: (
            captured.update({"webhook": webhook, "title": title, "content": content}) or True
        ),
    )

    mod.notify_theme_radar_report({"trade_date": "2026-06-12"}, "# report")

    assert captured == {
        "webhook": "https://example.invalid/webhook",
        "title": "主线雷达周报 2026-06-12",
        "content": "# report",
    }


def _snapshot() -> dict:
    return {
        "trade_date": "2026-05-27",
        "themes": [
            _theme("光模块", "confirmed", 0.72),
            _theme("芯片半导体", "observe", 0.58),
        ],
        "strategic_candidates": [
            _candidate("000001", "光模块龙头", "光模块", 1, 0.82),
            _candidate("000002", "封测龙头", "芯片半导体", 1, 0.66),
        ],
    }


def _theme(theme: str, state: str, score: float) -> dict:
    return {
        "theme": theme,
        "state": state,
        "score": score,
        "leader_score": score - 0.05,
        "heat_score": 0.50,
        "structure_score": 0.64,
        "breadth_score": 0.61,
        "persistence_score": 0.55,
        "catalyst_score": 0.30,
        "crowding_score": 0.20,
        "member_count": 12,
        "leader_count": 3,
        "evidence": ["heat:CPO", "streak:4"],
    }


def _candidate(code: str, name: str, theme: str, rank: int, score: float) -> dict:
    return {
        "code": code,
        "name": name,
        "theme": theme,
        "theme_rank": rank,
        "stock_score": score,
        "leader_score": score - 0.04,
        "ret60": 42.0,
        "ret120": 118.0,
        "ret250": 205.0,
        "near_high_120d": True,
        "theme_score": 0.70,
        "state": "confirmed",
        "reasons": ["RPS60/120/250=0.90/0.95/0.98"],
    }
