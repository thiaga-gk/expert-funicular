"""Backtest evidence format 1.0.0 and backtest report format 1.0.0: past auto-stop changes measured from system tables.

A backtest (``realizer --backtest``) reads every past auto-stop change of the selected warehouses and keeps what it read
in this record before it computes anything, so ``--from-evidence`` reproduces the report offline from numbers alone. Per
warehouse it holds the configuration epochs, the billed daily cost and the settled billing day; per measured change the
pre- and post-change windows (running-idle totals and the replay rows of the one auto-stop value each replays), the
canary guard rows of both windows and the AWS price model. The row shapes are the measurement evidence's
(``realizer.contracts.measurement``), read by the same bridge functions. Never SQL text, owner emails or tags. The
format is checked against ``schema/realization-backtest-evidence.schema.json`` (built by ``backtest_evidence_schema``)
when it is written and when it is loaded.
"""
from __future__ import annotations

from typing import Any, Dict

from realizer.contracts.measurement import SOURCES, measurement_evidence_schema, window_schema

BACKTEST_EVIDENCE_FORMAT_VERSION = "1.0.0"
BACKTEST_REPORT_FORMAT_VERSION = "1.0.0"
# Why an epoch pair whose auto-stop differs is not measured.
SKIP_REASONS = ("UNSUPPORTED_WAREHOUSE_TYPE", "OTHER_KNOB_CHANGED", "EXECUTION_WAREHOUSE", "NOT_SETTLED", "BEFORE_HISTORY")

_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_TIMESTAMP = {"type": "string", "format": "date-time", "minLength": 1}
_IDENTIFIER = {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$"}


def _obj(properties: Dict[str, Any], *, nullable: bool = False) -> Dict[str, Any]:
    return {"type": ["object", "null"] if nullable else "object", "additionalProperties": False,
            "required": sorted(properties), "properties": properties}


def backtest_evidence_schema() -> Dict[str, Any]:
    """The Draft 2020-12 JSON Schema of backtest evidence format 1.0.0 (rows shaped as in the measurement evidence)."""
    m = measurement_evidence_schema()["properties"]
    window = window_schema(levels=False)
    change = _obj({
        "change_epoch_start": _TIMESTAMP,
        "windows": _obj({"pre": window, "post": window}),
        "guard": m["guard"],
        "aws": m["aws"]["properties"]["post"],
    })
    warehouse = _obj({
        "workspace_id": _IDENTIFIER, "warehouse_id": _IDENTIFIER, "billing": m["billing"],
        "epochs": m["epochs"], "daily_cost": m["daily_cost"], "changes": {"type": "array", "items": change},
    })
    properties = {
        "backtest_evidence_format_version": {"const": BACKTEST_EVIDENCE_FORMAT_VERSION},
        "source": {"enum": list(SOURCES)},
        "as_of_utc": _TIMESTAMP,
        "history_first_day": _DATE,
        "source_region": {"type": ["string", "null"]},
        "selectors": {"type": "array", "items": {"type": "string", "pattern": r"^([A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+$"}},
        "execution_warehouse_id": {"type": ["string", "null"], "pattern": r"^[A-Za-z0-9_-]+$"},
        "warehouses_read": {"type": "integer", "minimum": 0},
        "warehouses": {"type": "array", "items": warehouse},
        "assumptions": m["assumptions"],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationBacktestEvidence",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }
