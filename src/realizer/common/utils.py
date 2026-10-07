"""Deterministic helpers: UTC timestamps (written and compared), decimals and money strings, and JSON file I/O.

Moved unchanged from the single-file Realizer, plus ``Deferred``. No domain logic: configuration normalization and
hashing belong to the analyzer.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Generic, Optional, TypeVar

from realizer.contracts.realization import canonical_decimal


def parse_utc(value: Any) -> dt.datetime:
    """The aware UTC time of *value* (a ``datetime`` or an ISO-8601 text; a naive one is read as UTC).

    Times are compared and ordered through this, never as text: ``utc_iso`` writes ``...T12:00:00Z`` without and
    ``...T12:00:00.500000Z`` with microseconds, and ``.`` sorts before ``Z``, so the text of a later time can sort first.
    A missing value raises ``ValueError``, as does a text that is not a time.
    """
    if isinstance(value, dt.datetime):
        d = value
    else:
        if value is None:
            raise ValueError("timestamp is required")
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def utc_iso(value: Any) -> str:
    return parse_utc(value).isoformat().replace("+00:00", "Z")


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


T = TypeVar("T")


@dataclass(frozen=True)
class Deferred(Generic[T]):
    """The outcome of a computation made early, delivered where the result is needed.

    ``Deferred.of(fn, *args, **kwargs)`` calls ``fn`` now and keeps either its value or the ``Exception`` it raised;
    ``get()`` returns the value or raises that exception. A pure check that used to run only on one branch can thus be
    evaluated by an upstream capability without changing behavior: a malformed input still raises, with the same
    exception, at the branch that needs the result, and never when no branch does. ``fn`` must be pure (no I/O, no
    logging), because it runs even if its result is never read.
    """
    value: Optional[T] = None
    error: Optional[Exception] = None

    @staticmethod
    def of(fn: Callable[..., T], *args: Any, **kwargs: Any) -> "Deferred[T]":
        try:
            return Deferred(value=fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - kept, re-raised by get()
            return Deferred(error=exc)

    def get(self) -> T:
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]
