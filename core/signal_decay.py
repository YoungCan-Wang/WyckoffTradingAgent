"""IC / Sharpe decay lifecycle for signal health (shadow unless applied)."""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

from utils.env import env_bool
from utils.safe import safe_float

LIFECYCLE = ("active", "monitoring", "decayed", "disabled")


def decay_apply_enabled() -> bool:
    return env_bool("SIGNAL_DECAY_APPLY", False)


def information_coefficient(signed_scores: list[float], forward_returns: list[float]) -> float | None:
    if len(signed_scores) != len(forward_returns) or len(signed_scores) < 8:
        return None
    return _pearson(signed_scores, forward_returns)


def sharpe_ratio(returns: list[float]) -> float | None:
    if len(returns) < 8:
        return None
    avg = mean(returns)
    sigma = pstdev(returns)
    if sigma <= 0:
        return None
    return avg / sigma


def classify_decay_lifecycle(
    *,
    ic: float | None,
    sharpe: float | None,
    sample_count: int,
    min_samples: int = 30,
) -> tuple[str, str]:
    if sample_count < min_samples or ic is None or sharpe is None:
        return "monitoring", f"samples={sample_count}<{min_samples} or ic/sharpe missing"
    if ic < 0 and sharpe < 0:
        return "disabled", f"ic={ic:.3f}, sharpe={sharpe:.3f}"
    if ic < 0.02 or sharpe < 0:
        return "decayed", f"ic={ic:.3f}, sharpe={sharpe:.3f}"
    if ic < 0.05:
        return "monitoring", f"ic={ic:.3f}, sharpe={sharpe:.3f}"
    return "active", f"ic={ic:.3f}, sharpe={sharpe:.3f}"


def decay_fields_from_returns(returns: list[float]) -> dict[str, Any]:
    scores = [1.0 if value > 0 else -1.0 for value in returns]
    ic = information_coefficient(scores, returns)
    sharpe = sharpe_ratio(returns)
    lifecycle, reason = classify_decay_lifecycle(ic=ic, sharpe=sharpe, sample_count=len(returns))
    return {
        "ic": ic,
        "sharpe": sharpe,
        "decay_lifecycle": lifecycle,
        "decay_reason": reason,
    }


def registry_status_for_decay(lifecycle: str, current_status: str) -> str:
    if not decay_apply_enabled():
        return current_status
    if current_status == "RETIRED":
        return "RETIRED"
    if lifecycle == "disabled":
        return "RETIRED"
    if lifecycle == "decayed":
        return "WATCH"
    return current_status


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    clean_x = [safe_float(x) for x in xs]
    clean_y = [safe_float(y) for y in ys]
    if None in clean_x or None in clean_y:
        return None
    mean_x = mean(clean_x)
    mean_y = mean(clean_y)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(clean_x, clean_y, strict=True))
    den_x = sum((x - mean_x) ** 2 for x in clean_x) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in clean_y) ** 0.5
    if den_x <= 0 or den_y <= 0:
        return None
    return num / (den_x * den_y)
