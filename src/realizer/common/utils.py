"""Deterministic helpers: UTC timestamps, decimals and money strings, and JSON file I/O.

Moved unchanged from the single-file Realizer. No domain logic: configuration normalization and hashing belong to
the analyzer.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from realizer.contracts.realization import canonical_decimal


def utc_iso(value: Any) -> str:
    if isinstance(value, dt.datetime):
        d = value
    else:
        if value is None:
            raise ValueError("timestamp is required")
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite decimal")
    return d


def money(value: Any) -> Optional[str]:
    d = decimal(value)
    if d is None:
        return None
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def ratio_string(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else canonical_decimal(value)


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
