from __future__ import annotations

from core.signal_event_position import event_matches, position_intent, to_signal


def test_compose_spring_and_not_utad():
    signals = [
        to_signal("spring", "000001", "2026-08-21"),
        to_signal("lps", "000001", "2026-08-21"),
    ]
    assert event_matches(signals, all_of=("spring",), any_of=("lps", "sos"), not_of=("utad",))
    assert not event_matches(signals, all_of=("sos",))
    assert position_intent(True, "accum") == "PROBE"
    assert position_intent(True, "distrib") == "EXIT"
    assert position_intent(False, "trend") == "FLAT"
