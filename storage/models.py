"""Shared persistence validation without an ORM dependency."""

from __future__ import annotations

import math
from typing import Any

REVIEW_STATUSES = ("pending", "approved", "rejected")


def finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def validate_range(start: Any, end: Any, duration: Any) -> tuple[float, float]:
    start = finite_number(start, "start")
    end = finite_number(end, "end")
    duration = finite_number(duration, "duration")
    if not 0 <= start < end <= duration:
        raise ValueError(f"Invalid clip range: require 0 <= start < end <= {duration:g}")
    return start, end
