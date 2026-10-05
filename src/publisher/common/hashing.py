"""Canonical JSON used for deterministic hashing."""
from __future__ import annotations

import json
from typing import Any

from publisher.common.utils import as_serializable


def canonical_json(value: Any) -> str:
    return json.dumps(as_serializable(value), sort_keys=True, separators=(",", ":"), default=str)
