"""Timestamp parsing and UTC ISO-8601 formatting."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional


def parse_datetime(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def iso(value: Any) -> Optional[str]:
    parsed = parse_datetime(value)
    return parsed.astimezone(dt.timezone.utc).isoformat() if parsed else None


def _iso_z(value: Any) -> str:
    """Normalize an observed timestamp to an offset-independent UTC ISO-8601 string."""
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Expected ISO-8601 timestamp, got {value!r}")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
