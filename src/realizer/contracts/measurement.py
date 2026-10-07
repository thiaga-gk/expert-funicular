"""Measurement evidence format 2.0.0: what one live collection read for one warehouse, kept so a run can be replayed.

A realize or backtest run that reads system tables writes this record before it projects an observation, and
``--from-evidence`` replays it, so a live result can be reproduced offline from numbers alone. It holds identifiers,
configurations, billed daily cost, running-idle window totals, the running minutes by cluster count of each window (for a
cluster-count change), the size runtime rows (for a size change), the canary guard rows and the AWS price model; never
SQL text, statement sources, owner emails or tags. Values the source could not establish are ``null``. The format is checked against
``schema/realization-measurement-evidence.schema.json`` (built by ``measurement_evidence_schema``) when it is written
and when it is loaded.
"""
from __future__ import annotations

from typing import Any, Dict

MEASUREMENT_EVIDENCE_FORMAT_VERSION = "2.0.0"

# The decision knobs of a configuration epoch, in the Publisher's spelling.
EPOCH_KNOBS = ("warehouse_type", "warehouse_channel", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes")
# Running-idle window totals and per-value replay fields (the Publisher's running-idle collector).
WINDOW_FIELDS = ("queries", "running_segment_count", "observed_running_cluster_minutes", "observed_running_wall_minutes",
                 "kept_alive_wall_minutes", "kept_alive_gap_count", "restarts_for_queries")
REPLAY_FIELDS = ("removable_running_wall_minutes", "removable_running_cluster_minutes", "additional_starts", "avoided_starts",
                 "added_idle_wall_minutes", "added_idle_cluster_minutes")
# Canary guard columns (the Publisher's canary guard collector, over explicit periods).
GUARD_FIELDS = ("queries", "failure_cancel_pct", "queued_queries", "queued_query_pct", "decision_queries", "spill_queries",
                "spill_query_pct", "shared_source_queries", "p95_execution_ms_shared_sources")
# Size runtime fields (the Publisher's size runtime collector over the pre- and post-change days).
SIZE_RUNTIME_FIELDS = ("post_executed_queries", "matched_sources", "matched_post_queries", "post_ms_at_post_median", "post_ms_at_pre_median",
                       "pre_busy_wall_minutes", "post_busy_wall_minutes")
# Publisher policy values a measurement relies on, recorded with the evidence so a replay uses the same ones.
ASSUMPTION_FIELDS = ("restart_cost_cluster_minutes_pro_classic", "restart_cost_cluster_minutes_serverless", "billing_settle_hours",
                     "failure_cancel_pct_downsize_safe", "spill_query_pct_downsize_safe", "queue_query_pct_safe", "canary_p95_regression_pct",
                     "canary_p95_min_queries", "min_query_sample_size", "severity_min_affected_queries", "cold_start_added_share_max_pct")
SOURCES = ("LIVE_SYSTEM_TABLES", "DEMO_SIMULATION")

_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_NULLABLE_DATE = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_TIMESTAMP = {"type": "string", "format": "date-time", "minLength": 1}
_NULLABLE_TIMESTAMP = {"type": ["string", "null"], "format": "date-time"}
_NUMBER = {"type": "number"}
_NULLABLE_NUMBER = {"type": ["number", "null"]}


def _obj(properties: Dict[str, Any], *, nullable: bool = False) -> Dict[str, Any]:
    return {"type": ["object", "null"] if nullable else "object", "additionalProperties": False,
            "required": sorted(properties), "properties": properties}


def window_schema(*, levels: bool = True) -> Dict[str, Any]:
    """The schema of one collected window (nullable): its epoch, days, running-idle totals and replay rows, and with
    *levels* its running minutes by cluster count (``null`` when not collected). The backtest's windows have no levels."""
    replay_row = _obj({"target_min": {"type": "integer", "minimum": 1}, **{f: _NUMBER for f in REPLAY_FIELDS}})
    properties = {
        "label": {"enum": ["pre", "post"]}, "epoch_start": _TIMESTAMP, "first_day": _DATE, "last_day": _DATE,
        "window_start": _NULLABLE_TIMESTAMP, "window_end": _NULLABLE_TIMESTAMP,
        "running": _obj({f: _NUMBER for f in WINDOW_FIELDS}, nullable=True),
        "replay": {"type": "array", "items": replay_row},
    }
    if levels:
        level = _obj({"cluster_count": {"type": "integer", "minimum": 1}, "running_minutes": _NUMBER})
        properties["levels"] = {"type": ["array", "null"], "items": level}
    return _obj(properties, nullable=True)


def measurement_evidence_schema() -> Dict[str, Any]:
    """The Draft 2020-12 JSON Schema of measurement evidence format 2.0.0."""
    epoch = _obj({
        "epoch_start": _TIMESTAMP, "epoch_end": _NULLABLE_TIMESTAMP,
        "warehouse_type": {"type": "string", "minLength": 1}, "warehouse_channel": {"type": "string", "minLength": 1},
        "warehouse_size": {"type": "string", "minLength": 1}, "min_clusters": {"type": "integer", "minimum": 0},
        "max_clusters": {"type": "integer", "minimum": 1}, "auto_stop_minutes": {"type": "integer", "minimum": 0},
    })
    daily = _obj({
        "usage_date": _DATE, "epoch_start": _TIMESTAMP, "currency_code": {"type": ["string", "null"]},
        "dbu_quantity": _NUMBER, "unpriced_dbu_quantity": _NUMBER, "list_cost": _NULLABLE_NUMBER,
        "retracted_dbu_quantity": _NUMBER, "restated_dbu_quantity": _NUMBER,
    })
    window = window_schema()
    _count = {"type": "integer", "minimum": 0}
    size_runtime = _obj({
        "min_source_queries": {"type": "integer", "minimum": 1},
        **{f: _count for f in ("post_executed_queries", "matched_sources", "matched_post_queries")},
        **{f: _NULLABLE_NUMBER for f in ("post_ms_at_post_median", "post_ms_at_pre_median")},
        **{f: _NUMBER for f in ("pre_busy_wall_minutes", "post_busy_wall_minutes")},
    }, nullable=True)
    guard_period = _obj({f: _NULLABLE_NUMBER for f in GUARD_FIELDS}, nullable=True)
    aws = _obj({
        "status": {"type": "string", "minLength": 1}, "cluster_hourly_price": _NULLABLE_NUMBER,
        "currency": {"type": ["string", "null"]}, "region": {"type": ["string", "null"]}, "blocker": {"type": ["string", "null"]},
    }, nullable=True)
    properties = {
        "measurement_evidence_format_version": {"const": MEASUREMENT_EVIDENCE_FORMAT_VERSION},
        "source": {"enum": list(SOURCES)},
        "workspace_id": {"type": "string", "minLength": 1},
        "warehouse_id": {"type": "string", "minLength": 1},
        "as_of_utc": _TIMESTAMP,
        "source_region": {"type": ["string", "null"]},
        "billing": _obj({"settled_through": _NULLABLE_DATE, "watermark_source": {"type": "string", "minLength": 1},
                         "billing_watermark": _NULLABLE_TIMESTAMP}),
        "epochs": {"type": "array", "items": epoch},
        "daily_cost": {"type": "array", "items": daily},
        "windows": _obj({"pre": window, "post": window}),
        "guard": _obj({"CURRENT": guard_period, "PREVIOUS": guard_period}, nullable=True),
        "size_runtime": size_runtime,
        "aws": _obj({"pre": aws, "post": aws}),
        "assumptions": _obj({f: _NUMBER for f in ASSUMPTION_FIELDS}),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationMeasurementEvidence",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }
