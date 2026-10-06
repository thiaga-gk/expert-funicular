"""Generic value coercion and serialization helpers. Business rules never live here."""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Mapping, Optional


def optional_float(value: Any) -> Optional[float]:
    """Float or None when the source did not report a usable value; unknown evidence must not read as zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def safe_float(value: Any, default: float = 0.0) -> float:
    """Float, or *default* for None or an unparseable value. Unlike optional_float it substitutes a number, so use it only
    where zero or the default is a real fallback, not for evidence that may be unknown."""
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Int, or *default* for None or an unparseable value (see safe_float on when a default is acceptable)."""
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def round_or_none(value: Optional[float], digits: int = 0) -> Optional[float]:
    """Round, keeping None as None (zero digits rounds to an int)."""
    return None if value is None else (round(value) if digits == 0 else round(value, digits))


def clamp(value: float, lo: float, hi: float) -> float:
    """*value* limited to the closed range [lo, hi]."""
    return max(lo, min(hi, value))


def _map_pairs(value: Any) -> Optional[Dict[str, str]]:
    """A MAP column as a mapping or, as some connectors return it, a list of [key, value] pairs or key/value entries."""
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out: Dict[str, str] = {}
        for item in value:
            if isinstance(item, Mapping) and "key" in item and "value" in item:
                out[str(item["key"])] = str(item["value"])
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = str(item[1])
            else:
                return None
        return out
    return None


def normalize_map(value: Any) -> Dict[str, str]:
    """A tag/map column as a string-to-string dict: a mapping, pairs, or a JSON string of either. None is empty; a value
    that cannot be decoded is kept whole under the key "raw" rather than dropped."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            obj = json.loads(value)
        except Exception:
            return {"raw": value}
        decoded = _map_pairs(obj)
        return decoded if decoded is not None else {"raw": value}
    decoded = _map_pairs(value)
    return decoded if decoded is not None else {"raw": str(value)}


def as_serializable(value: Any) -> Any:
    """Recursively convert dataclasses, mappings, sequences and dates into JSON-compatible plain values (dates as ISO text)."""
    if is_dataclass(value):
        return as_serializable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): as_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_serializable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def as_string_list(value: Any) -> Optional[List[str]]:
    """A collector array column as sorted distinct strings: a list, a JSON array string, or a Spark array rendered as
    text ("[a, b]"). None stays None (not collected)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            text = value.strip()
            value = [v.strip().strip('"') for v in text[1:-1].split(",")] if text.startswith("[") and text.endswith("]") else [text]
        if not isinstance(value, list):
            value = [value]
    return sorted({str(x) for x in value if x not in (None, "")})
