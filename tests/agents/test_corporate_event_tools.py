from core.corporate_event_scan import XINGSHUAIER_TELEGRAPH


def test_agent_scan_is_observation_only(monkeypatch) -> None:
    from agents.corporate_event_tools import scan_corporate_events
    from core.corporate_event_scan import scan_corporate_events as classify
    from workflows.corporate_event_scan_runtime import CorporateEventScanResult

    hits = classify([{"title": XINGSHUAIER_TELEGRAPH, "source": "财联社", "published_at": "2026-09-21 19:20:00"}])

    def fake_run(**kwargs):
        assert kwargs.get("persist") is False
        return CorporateEventScanResult("2026-09-21 19:40", hits, "report", persist_path(), persist_path(), True)

    monkeypatch.setattr("agents.corporate_event_tools.run_corporate_event_scan", fake_run)
    payload = scan_corporate_events(limit=5)
    assert payload["hits"][0]["code"] == "002860"
    assert "不是实盘" in payload["note"]
    assert payload["source_ok"] is True


def persist_path():
    from pathlib import Path

    return Path()
