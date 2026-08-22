from __future__ import annotations

from core.signal_decay import (
    classify_decay_lifecycle,
    decay_fields_from_returns,
    information_coefficient,
    registry_status_for_decay,
    sharpe_ratio,
)


def test_ic_and_sharpe_detect_aligned_returns():
    scores = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0]
    forwards = [2.0, 1.5, -1.0, -0.8, 1.2, 0.9, -0.7, -1.1]
    ic = information_coefficient(scores, forwards)
    sharpe = sharpe_ratio(forwards)
    assert ic is not None and ic > 0.8
    assert sharpe is not None


def test_negative_ic_and_sharpe_disable():
    lifecycle, _reason = classify_decay_lifecycle(ic=-0.2, sharpe=-0.4, sample_count=40)
    assert lifecycle == "disabled"


def test_decay_fields_and_shadow_registry(monkeypatch):
    monkeypatch.delenv("SIGNAL_DECAY_APPLY", raising=False)
    fields = decay_fields_from_returns([1.0, 1.2, -0.3, 0.4, 0.8, -0.1, 0.2, 0.5, 0.6, 0.1] * 4)
    assert "decay_lifecycle" in fields
    assert registry_status_for_decay("disabled", "ACTIVE") == "ACTIVE"
    monkeypatch.setenv("SIGNAL_DECAY_APPLY", "1")
    assert registry_status_for_decay("disabled", "ACTIVE") == "RETIRED"
