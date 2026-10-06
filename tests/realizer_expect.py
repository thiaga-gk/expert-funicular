"""Stored expectations for the Realizer tests.

The single-file oracle is retired (docs/proposals/realizer/repo-reorg-parity.md, Phase 11), so a test that used to
compare a result with the oracle's now compares it with a value stored under ``tests/fixtures/realizer/expectations/``.
The values were generated once from the oracle (the git tag ``realizer-reorg-oracle-1.0.0``) and are rebuilt
deliberately, from the current engine, by ``python scripts/update_realizer_goldens.py --expectations --approved-change
"<why>"``, which runs the tests in record mode.

Usage in a test::

    expect.check(self, "grid", results)                    # key: <test class>.<test method>/grid
    expect.check(self, "grid", results, spot={"count": n})  # spot: a small readable summary, always stored in full

``value`` is anything JSON-like (dicts, lists, tuples, sets, Decimal, Enum, dataclasses, objects with ``to_dict``).
A value whose canonical JSON is short is stored whole, so a mismatch shows a readable diff; a longer one (a grid of
thousands of results) is stored as its SHA-256 digest and length, and ``spot`` carries the handful of values worth
reading. Set ``REALIZER_EXPECTATIONS=record`` to write instead of compare.
"""
from __future__ import annotations

import atexit
import dataclasses
import enum
import hashlib
import json
import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
EXPECTATIONS = Path(os.environ.get("REALIZER_EXPECTATIONS_DIR") or ROOT / "tests" / "fixtures" / "realizer" / "expectations")
INLINE_LIMIT = 2000   # characters of canonical JSON stored whole; longer values are stored as a digest

_lock = threading.Lock()
_recorded: Dict[str, Dict[str, Any]] = {}
_loaded: Dict[str, Dict[str, Any]] = {}


def recording() -> bool:
    return os.environ.get("REALIZER_EXPECTATIONS") == "record"


def _default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: json.dumps(item, sort_keys=True, default=_default))
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"cannot store {type(value).__name__} in an expectation")


def canonical(value: Any) -> str:
    """The canonical JSON text of *value* (sorted keys, no spaces, non-finite floats allowed)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def normalized(value: Any) -> Any:
    """*value* as plain JSON data (what the stored form decodes to)."""
    return json.loads(canonical(value))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def module_name(test: Any) -> str:
    name = type(test).__module__
    return name[len("tests."):] if name.startswith("tests.") else name


def key_for(test: Any, label: str) -> str:
    return f"{type(test).__name__}.{test._testMethodName}" + (f"/{label}" if label else "")


def _path(module: str) -> Path:
    return EXPECTATIONS / f"{module}.json"


def _load(module: str) -> Dict[str, Any]:
    if module not in _loaded:
        path = _path(module)
        _loaded[module] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return _loaded[module]


def entry(value: Any, spot: Optional[Any] = None) -> Dict[str, Any]:
    """The stored form of *value*: whole when short, else its digest and length; plus the spot values."""
    text = canonical(value)
    stored: Dict[str, Any] = {"value": json.loads(text)} if len(text) <= INLINE_LIMIT else {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}
    if spot is not None:
        stored["spot"] = normalized(spot)
    return stored


def check(test: Any, label: str, value: Any, *, spot: Optional[Any] = None) -> None:
    """Assert (on *test*) that *value* equals the stored expectation, or record it in record mode."""
    module, key = module_name(test), key_for(test, label)
    stored_entry = entry(value, spot)
    if recording():
        with _lock:
            _recorded.setdefault(module, {})[key] = stored_entry
        return
    expected = _load(module).get(key)
    test.assertIsNotNone(expected, f"no stored expectation {module}: {key}; record it with scripts/update_realizer_goldens.py --expectations")
    if "spot" in expected or spot is not None:
        test.assertEqual(stored_entry.get("spot"), expected.get("spot"), f"{key}: spot values")
    if "value" in expected:
        test.assertEqual(stored_entry.get("value"), expected["value"], key)
    else:
        test.assertEqual((stored_entry.get("sha256"), stored_entry.get("length")), (expected.get("sha256"), expected.get("length")),
                         f"{key}: digest of {stored_entry.get('length', 'a short')} characters of canonical JSON")


def _flush() -> None:
    for module, entries in _recorded.items():
        path = _path(module)
        merged = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        merged.update(entries)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


if recording():
    atexit.register(_flush)
