"""Shared parsers and statistics for reports built from backtest ``summary_*.md`` / ``trades_*.csv``."""

from __future__ import annotations

import math
import random
import re
from statistics import mean
from typing import Any

BOOTSTRAP_RESAMPLES = 2000


def line_value(content: str, label: str) -> str | None:
    pattern = re.compile(rf"^\s*-\s*{label}\s*:\s*(.+?)\s*$")
    return next((match.group(1) for line in content.splitlines() if (match := pattern.match(line))), None)


def metric(content: str, label: str) -> float | None:
    raw = line_value(content, label)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw or "")
    return float(match.group(0)) if match else None


def cash_section(content: str) -> str:
    marker = "## 真实现金账户模拟"
    if marker not in content:
        return content
    section = content.split(marker, 1)[1]
    return section.split("\n## ", 1)[0]


def cash_metric(content: str, label: str) -> float | None:
    return metric(cash_section(content), label)


def cash_int_metric(content: str, label: str) -> int | None:
    value = cash_metric(content, label)
    return int(value) if value is not None else None


def date_range(content: str) -> tuple[str, str]:
    raw = line_value(content, "区间") or ""
    parts = [part.strip() for part in raw.split("~", 1)]
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


def optional_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: object, suffix: str = "") -> str:
    return "-" if value is None else f"{float(value):+.2f}{suffix}"


def return_stats(values: list[float | None]) -> dict[str, Any]:
    returns = [value for value in values if value is not None]
    return {
        "trades": len(values),
        "avg_return": mean(returns) if returns else None,
        "win_rate": sum(value > 0 for value in returns) / len(returns) * 100.0 if returns else None,
    }


def bootstrap_mean_ci(values: list[float], seed: int = 0) -> tuple[float, float] | None:
    """Percentile bootstrap 95% CI of the mean; seeded so CI artifacts are reproducible."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    size = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(size)) / size for _ in range(BOOTSTRAP_RESAMPLES))
    return means[int(BOOTSTRAP_RESAMPLES * 0.025)], means[int(BOOTSTRAP_RESAMPLES * 0.975) - 1]
