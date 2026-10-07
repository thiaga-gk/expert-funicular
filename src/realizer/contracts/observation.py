"""Realizer observation format 3.1.0: the normalized post-change observation one realize run consumes.

An observation is a complete statement about one warehouse at one time: its identity, the full observed configuration,
the measurement window, the readiness and performance evidence, the financial-quality evidence and, when measured, the
economics and how they were measured (``measurement``: the measured knob, the windows before and after the change, the
replay's quantities for an auto-stop change, the size runtime counterfactual for a size change (``size``), the running
minutes by cluster count for a cluster-count change (``levels``), and the billed change before and after it; ``null``
for a caller-supplied observation). Every key is required; a value the source could not establish is ``null``, never omitted, and a ``null`` gate
input never passes its gate. The format is checked against ``schema/realization-observation.schema.json`` (built by
``observation_schema``) before a run writes anything (``realizer.config.loader.validate_observation``).
"""
from __future__ import annotations

from typing import Any, Dict

from realizer.contracts.analysis import EvidenceQuality, IntentState, OriginAttribution

OBSERVATION_FORMAT_VERSION = "3.1.0"

# Counterfactual methods (Realizer Policy Contract, counterfactual section): the four that can support a claim, and
# the two that are recorded for diagnosis only.
CLAIM_ELIGIBLE_COUNTERFACTUAL_METHODS = ("PAIRED_CANARY", "CALIBRATED_PRIOR_STATE", "MATCHED_HISTORICAL", "CONTROLLED_PRE_POST")
DIAGNOSTIC_COUNTERFACTUAL_METHODS = ("CORRELATED_ONLY", "NO_DEFENSIBLE_COUNTERFACTUAL")
ECONOMIC_BASES = ("ECONOMIC_COST", "POSTED_DOLLAR", "CONSTANT_PRICE")
AWS_EVIDENCE_TYPES = ("MODELED", "CUR_ACTUAL")
EFFECTIVE_TIME_QUALITIES = ("EXACT", "BOUNDED", "OBSERVED_AT_ONLY")
CORRECTION_TYPES = ("RESTATEMENT", "RETRACTION")
DECISION_KNOBS = ("warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes", "warehouse_channel")
MIGRATION_METHOD = "MIGRATION_BILLED_OBSERVED"
MEASUREMENT_METHODS = ("AUTO_STOP_REPLAY", "SIZE_RUNTIME", "FLOOR_LEVELS", MIGRATION_METHOD)
# The hard disqualifier of a warehouse-type migration: its observed delta has no reverse (new to old) model, so it is never claimed.
NO_REVERSE_MIGRATION_MODEL = "NO_REVERSE_MIGRATION_MODEL"
COST_BASES = ("DBU_AND_MODELED_AWS", "DBU_ONLY")
NOT_MEASURED_REASONS = ("NOT_APPLIED", "NOT_SETTLED", "KNOB_NOT_REPLAYED", "SIZE_RUNTIME_UNMEASURED", "SOURCE_RATE_UNAVAILABLE",
                        "BUSY_SHARE_UNKNOWN", "LEVELS_UNAVAILABLE")
BILLED_CHANGE_BASES = ("BILLED_PRE_POST_AT_PRE_MIX",)
SIZE_BOUNDS = ("ELASTIC", "INELASTIC")
SIZE_DIRECTIONS = ("DOWNSIZE", "UPSIZE")
SIZE_RUNTIME_MULTIPLIER_SOURCES = ("CANDIDATE", "PUBLISHER_THRESHOLDS")
CROSS_CHECK_OUTCOMES = ("AGREES", "DISAGREES", "INCONCLUSIVE")
CROSS_CHECK_REASONS = ("NO_PRE_WINDOW", "NOISE_UNKNOWN", "OTHER_KNOB_CHANGED", "VOLUME_SHIFT", "BILLING_INCOMPLETE", "BELOW_NOISE")
GUARD_METRICS = ("queries", "failure_cancel_pct", "queued_queries", "queued_query_pct", "decision_queries", "spill_queries",
                 "spill_query_pct", "shared_source_queries", "p95_execution_ms_shared_sources")

def is_migration_observed(measurement: Any) -> bool:
    """Whether an observation's ``measurement`` states a warehouse-type migration's observed delta."""
    return bool(measurement) and measurement.get("method") == MIGRATION_METHOD


_DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"


def _decimal(nullable: bool = True) -> Dict[str, Any]:
    """A money or percentage amount: a decimal string or a JSON number."""
    return {"anyOf": [{"type": "string", "pattern": _DECIMAL_PATTERN}, {"type": "number"}] + ([{"type": "null"}] if nullable else [])}


def _obj(properties: Dict[str, Any], *, nullable: bool = False) -> Dict[str, Any]:
    return {"type": ["object", "null"] if nullable else "object", "additionalProperties": False,
            "required": sorted(properties), "properties": properties}


TIMESTAMP = {"type": "string", "format": "date-time", "minLength": 1}
NULLABLE_COUNT = {"type": ["integer", "null"], "minimum": 0}
DAY = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
NULLABLE_TIMESTAMP = {"type": ["string", "null"], "format": "date-time"}
PERCENT = {"anyOf": [{"type": "number", "minimum": 0, "maximum": 100}, {"type": "string", "pattern": _DECIMAL_PATTERN}, {"type": "null"}]}


def size_schema() -> Dict[str, Any]:
    """``measurement.size``: the size runtime counterfactual of a size change (``null`` for any other knob, or without
    size runtime evidence): the runtime ratio and its matched queries, the Publisher's runtime multiplier range, the old
    and new DBU cost per cluster-minute with their ratio against the published one, the busy share, the AWS prices of both sizes and both bounds (``null`` when not
    measured), with the one claimed."""
    count = {"type": "integer", "minimum": 0}
    bound = _obj({
        "running_cluster_minutes": _decimal(nullable=False), "dbu_cost": _decimal(nullable=False), "aws_cost": _decimal(),
        "counterfactual_cost": _decimal(nullable=False), "realized": _decimal(nullable=False),
    }, nullable=True)
    return _obj({
        "runtime_ratio": _decimal(), "matched_sources": count, "matched_post_queries": count, "post_executed_queries": count,
        "matched_query_coverage": _decimal(),
        "published_runtime_multiplier": _obj({
            "source": {"enum": list(SIZE_RUNTIME_MULTIPLIER_SOURCES)}, "direction": {"enum": list(SIZE_DIRECTIONS)},
            "optimistic": _decimal(nullable=False), "base": _decimal(nullable=False), "conservative": _decimal(nullable=False),
        }),
        "runtime_ratio_in_published_range": {"type": ["boolean", "null"]},
        "old_dbu_cost_per_cluster_minute": _decimal(), "new_dbu_cost_per_cluster_minute": _decimal(),
        "measured_rate_ratio": _decimal(), "published_rate_ratio": _decimal(), "rate_ratio_within_published_band": {"type": ["boolean", "null"]},
        "busy_share": _decimal(), "pre_busy_wall_minutes": _decimal(nullable=False), "post_busy_wall_minutes": _decimal(nullable=False),
        "observed_running_cluster_minutes": _decimal(nullable=False),
        "old_aws_cluster_hourly_price": _decimal(), "new_aws_cluster_hourly_price": _decimal(),
        "elastic": bound, "inelastic": bound, "bound_used": {"enum": [None, *SIZE_BOUNDS]},
    }, nullable=True)


def levels_schema() -> Dict[str, Any]:
    """``measurement.levels``: the running minutes by cluster count of a cluster-count change and what they show
    (``null`` for any other knob)."""
    return {"type": ["object", "null"]}


def observation_schema() -> Dict[str, Any]:
    """The Draft 2020-12 JSON Schema of observation format 3.1.0."""
    config = _obj({
        "warehouse_type": {"type": "string", "minLength": 1},
        "warehouse_size": {"type": "string", "minLength": 1},
        "min_clusters": {"type": "integer", "minimum": 0},
        "max_clusters": {"type": "integer", "minimum": 1},
        "auto_stop_minutes": {"type": "integer", "minimum": 0},
        "warehouse_channel": {"type": "string", "minLength": 1},
        "enable_photon": {"type": ["boolean", "null"]},
    })
    guard_period = _obj({m: _decimal() for m in GUARD_METRICS}, nullable=True)
    guards = _obj({
        "pre": guard_period, "post": guard_period,
        "cold_start": _obj({"pre_restarts": NULLABLE_COUNT, "pre_queries": NULLABLE_COUNT, "post_restarts": NULLABLE_COUNT,
                            "post_queries": NULLABLE_COUNT}, nullable=True),
    }, nullable=True)
    performance = _obj({
        "normalized_p95_runtime_regression_pct": _decimal(),
        "reliability_pass": {"type": ["boolean", "null"]},
        "correctness_pass": {"type": ["boolean", "null"]},
        "guards": guards,
    })
    financial_quality = _obj({
        "attributable_spend_pct": PERCENT,
        "price_coverage_pct": PERCENT,
        "currency_consistency_pct": PERCENT,
        "correction_chain_complete": {"type": ["boolean", "null"]},
        "material_cost_component_available": {"type": ["boolean", "null"]},
    })
    economics = _obj({
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "counterfactual_old_cost": _decimal(),
        "observed_new_cost": _decimal(),
        "period_days": {"type": "integer", "minimum": 1},
        "counterfactual_method": {"enum": list(CLAIM_ELIGIBLE_COUNTERFACTUAL_METHODS + DIAGNOSTIC_COUNTERFACTUAL_METHODS)},
        "economic_basis": {"enum": list(ECONOMIC_BASES)},
        "expected_annual_savings_actual_scope": _decimal(),
        "dbu_realized": _decimal(),
        "aws_economic_realized": _decimal(),
        "aws_cash_realized": _decimal(),
        "aws_commitment_freed": _decimal(),
        "aws_evidence_type": {"enum": [None, *AWS_EVIDENCE_TYPES]},
    }, nullable=True)
    correction = _obj({
        "key": {"type": "string", "minLength": 1},
        "type": {"enum": list(CORRECTION_TYPES)},
        "version": {"type": "integer", "minimum": 1},
        "reason": {"type": ["string", "null"]},
        "corrects_entry_id": {"type": ["string", "null"], "pattern": r"^RLE-[0-9A-F]{24}$"},
    }, nullable=True)
    window = _obj({
        "first_day": DAY, "last_day": DAY, "days": {"type": "integer", "minimum": 1}, "queries": {"type": "integer", "minimum": 0},
        "running_cluster_minutes": _decimal(nullable=False), "restarts": {"type": "integer", "minimum": 0},
        "billed_dbu": _decimal(nullable=False), "billed_cost": _decimal(),
    }, nullable=True)
    knob_value = {"type": ["integer", "string", "null"]}
    measurement = _obj({
        "method": {"enum": [None, *MEASUREMENT_METHODS]},
        "knob": {"enum": list(DECISION_KNOBS)},
        "source_value": knob_value, "applied_value": knob_value,
        "change_epoch_start": NULLABLE_TIMESTAMP, "observed_epoch_start": TIMESTAMP, "previous_epoch_start": NULLABLE_TIMESTAMP,
        "not_measured_reason": {"enum": [None, *NOT_MEASURED_REASONS]},
        "post_window": window, "pre_window": window, "volume_ratio": _decimal(),
        "other_knob_changes": {"type": "array", "items": {"enum": list(DECISION_KNOBS)}},
        "holds_other_knobs": {"type": "boolean"},
        "cost_basis": {"enum": [None, *COST_BASES]},
        "replay": _obj({
            "replayed_target_minutes": {"type": "integer", "minimum": 1},
            "added_idle_cluster_minutes": _decimal(nullable=False), "avoided_starts": _decimal(nullable=False),
            "restart_cost_cluster_minutes": _decimal(nullable=False), "net_cluster_minutes": _decimal(nullable=False),
            "dbu_cost_per_cluster_minute": _decimal(nullable=False), "aws_cluster_hourly_price": _decimal(),
            "aws_status": {"type": "string", "minLength": 1},
        }, nullable=True),
        "cross_check": _obj({
            "outcome": {"enum": list(CROSS_CHECK_OUTCOMES)}, "reason": {"enum": [None, *CROSS_CHECK_REASONS]},
            "replay_dbu_saving": _decimal(nullable=False), "billed_dbu_reduction": _decimal(), "difference_pct": _decimal(),
            "noise": _decimal(), "pre_weekly_costs": {"type": "array", "items": _decimal(nullable=False)},
        }, nullable=True),
        "billed_change": _obj({
            "basis": {"enum": list(BILLED_CHANGE_BASES)}, "days": {"type": "integer", "minimum": 1},
            "currency": {"type": ["string", "null"], "pattern": "^[A-Z]{3}$"},
            "dbu": _decimal(nullable=False), "aws_modeled": _decimal(), "total": _decimal(),
            "annualized_dbu": _decimal(nullable=False), "annualized_total": _decimal(),
        }, nullable=True),
        "size": size_schema(),
        "levels": levels_schema(),
    }, nullable=True)
    properties = {
        "observation_format_version": {"const": OBSERVATION_FORMAT_VERSION},
        "workspace_id": {"type": "string", "minLength": 1},
        "warehouse_id": {"type": "string", "minLength": 1},
        "source": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
        "observed_at_utc": TIMESTAMP,
        "recorded_at_utc": TIMESTAMP,
        "applied_at_utc": NULLABLE_TIMESTAMP,
        "effective_time_quality": {"enum": list(EFFECTIVE_TIME_QUALITIES)},
        "window_start_utc": NULLABLE_TIMESTAMP,
        "window_end_utc": NULLABLE_TIMESTAMP,
        "config": config,
        "elapsed_days": {"type": "integer", "minimum": 0},
        "query_count": {"type": ["integer", "null"], "minimum": 0},
        "representative_regime": {"type": ["boolean", "null"]},
        "evidence_quality": {"enum": [x.value for x in EvidenceQuality]},
        "workload": {"type": "object"},  # informational: enters no identity and no decision
        "performance": performance,
        "financial_quality": financial_quality,
        "economics": economics,
        "overlap_count": {"type": "integer", "minimum": 0},
        "origin_attribution": {"enum": [x.value for x in OriginAttribution]},
        "intent_state": {"enum": [x.value for x in IntentState]},
        "intent_reference": {"type": ["string", "null"]},
        "actor": {"type": ["string", "null"]},
        "applied_scenario_id": {"type": ["string", "null"], "pattern": r"^SCN-[0-9A-F]{24}$"},
        "rollback_requested": {"type": "boolean"},
        "rollback_observed": {"type": "boolean"},
        "rollback_reason": {"type": ["string", "null"]},
        "replacement_package_id": {"type": ["string", "null"], "pattern": r"^PKG-[0-9A-F]{24}$"},
        "correction": correction,
        "measurement": measurement,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationObservation",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }
