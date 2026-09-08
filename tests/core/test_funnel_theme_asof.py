from __future__ import annotations

import pandas as pd
import pytest

from core.funnel_theme import empty_theme_snapshot, select_linked_theme_radar, theme_snapshot_age_days


def _snapshot(day: str, theme: str = "光模块") -> dict:
    return {"trade_date": day, "themes": [{"theme": theme, "score": 0.8}]}


def _select(current: dict, persisted: dict | None, *, link_enabled: bool = True) -> tuple[dict, str]:
    return select_linked_theme_radar(
        current,
        persisted,
        "2026-09-07",
        enabled=True,
        link_enabled=link_enabled,
        max_age_days=3,
    )


@pytest.mark.parametrize("persisted_day", ["2026-09-04", "2026-09-07", "2026-09-08"])
def test_current_day_radar_wins_over_history_or_future(persisted_day: str) -> None:
    current = _snapshot("2026-09-07")
    assert _select(current, _snapshot(persisted_day, "大金融")) == (current, "current")


@pytest.mark.parametrize("persisted_day", ["2026-09-04", "2026-09-07"])
def test_valid_persisted_radar_is_explicit_fallback_only(persisted_day: str) -> None:
    persisted = _snapshot(persisted_day)
    assert _select(empty_theme_snapshot("2026-09-07"), persisted) == (persisted, "persisted_fallback")


@pytest.mark.parametrize("invalid_day", ["2026-09-08", "2026-09-03", "invalid", "", "NaT"])
def test_linked_radar_rejects_future_stale_or_undated_snapshot(invalid_day: str) -> None:
    current = empty_theme_snapshot("2026-09-07")
    assert _select(current, _snapshot(invalid_day)) == (current, "current")


@pytest.mark.parametrize("link_enabled", [False, True])
def test_future_current_snapshot_is_not_a_fallback(link_enabled: bool) -> None:
    snapshot, source = _select(_snapshot("2026-09-08"), None, link_enabled=link_enabled)
    assert snapshot == empty_theme_snapshot("2026-09-07")
    assert source == "current"


def test_snapshot_age_never_turns_future_into_recent_history() -> None:
    assert theme_snapshot_age_days(_snapshot("2026-09-08"), "2026-09-07", 3) == 4
    assert theme_snapshot_age_days(_snapshot("2026-09-04"), "2026-09-07", 3) == 3


@pytest.mark.parametrize(
    "snapshot_day,kept",
    [("2026-09-07", True), ("2026-09-04", True), ("2026-09-08", False), ("2026-08-01", False), ("", False)],
)
def test_core_replay_uses_same_asof_guard(snapshot_day: str, kept: bool) -> None:
    from core.wyckoff_engine import _mainline_theme_radar_asof

    frames = {"000001": pd.DataFrame({"date": ["2026-09-07", "2026-09-04"]})}
    snapshot = _snapshot(snapshot_day)

    result = _mainline_theme_radar_asof(frames, snapshot)

    assert bool(result.get("themes")) is kept
    assert snapshot == _snapshot(snapshot_day)


def test_core_replay_does_not_accept_radar_without_observation_date() -> None:
    from core.wyckoff_engine import _mainline_theme_radar_asof

    assert _mainline_theme_radar_asof({}, _snapshot("2026-09-07")) == {}
