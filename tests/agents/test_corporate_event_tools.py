from core.corporate_event_scan import XINGSHUAIER_TELEGRAPH
from integrations.public_mcp.contracts import TOOL_BY_NAME
from integrations.public_mcp.runtime import Runtime
from workflows.corporate_event_scan_runtime import run_corporate_event_scan


def test_agent_scan_is_observation_only(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    result = run_corporate_event_scan(
        as_of="2026-09-21 19:40",
        extra_items=[{"title": XINGSHUAIER_TELEGRAPH, "published_at": "2026-09-21 19:20:00"}],
        fetch_news=lambda *_a, **_k: [],
        fetch_cls=lambda: [],
        persist=False,
    )

    def fake_run(**kwargs):
        assert kwargs["persist"] is False
        return result

    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", fake_run)
    payload = scan_corporate_events(limit=5)
    assert payload["hits"][0]["code"] == "002860"
    assert payload["source_ok"] is True
    assert "不是实盘" in payload["note"]
    assert TOOL_BY_NAME["scan_corporate_events"].read_only


def test_all_sources_failed_is_also_an_mcp_tool_error(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    def boom(*_a, **_k):
        raise TimeoutError("private URL")

    result = run_corporate_event_scan(fetch_news=boom, fetch_cls=boom, persist=False)
    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", lambda **_: result)
    outcome = Runtime(lambda _spec, args: scan_corporate_events(**args)).call("scan_corporate_events")
    assert outcome.is_error
    assert outcome.data["source_status"] == "unavailable"
    assert "本次检索窗口未命中" not in outcome.data["report"]
    assert "private URL" not in str(outcome.data)


def test_invalid_limit_is_rejected_before_fetching(monkeypatch):
    from agents.corporate_event_tools import scan_corporate_events

    def forbidden(**_):
        raise AssertionError("must not fetch")

    monkeypatch.setattr("workflows.corporate_event_scan_runtime.run_corporate_event_scan", forbidden)
    for value in (0, 51, -1, True, "20", 1.5):
        assert "error" in scan_corporate_events(limit=value)
