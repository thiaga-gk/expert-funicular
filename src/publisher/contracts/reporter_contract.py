"""Reporter contract v3.2.0 for Databricks SQL Warehouse Optimization.

This module is intentionally independent from the publisher pipeline.  It defines the stable,
serialization-facing contract that every warehouse recommendation package must satisfy.
Presentation artifacts (workbooks, markdown, UI) consume this contract; they do not
change its semantics.

Identity model
--------------
* recommendation_package_id: immutable assessment package for one warehouse/epoch.
* epoch_id: immutable configuration regime.
* technique_id: stable catalog identity shared across warehouses/runs.
* candidate_id: one technique decision against one warehouse/epoch/evidence set.
* scenario_id: one mutually-exclusive bundled execution path.
* realization_id: one post-change measurement episode.

Hashing uses SHA-256 over an explicitly constructed canonical payload.  Volatile
presentation fields such as generated_at, filenames, logs, or workbook layout are
excluded from IDs.

Every dataclass has a ``validate()`` that raises ValueError; ``RecommendationPackage.validate()`` also recomputes every content ID and the
evidence fingerprint. Unknown values are None, never zero; DBU and AWS stay separate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

CONTRACT_VERSION = "3.2.0"
# Re-evaluation gates (contract 2.0.0: readiness replaces the 1.2.0 epoch-age gate). The validator embeds them, so
# changing the matching publisher policy thresholds (readiness_min_billed_days, min_economics_cost_history_days)
# requires a contract version bump.
REEVALUATION_READY_BILLED_DAYS = 2
REEVALUATION_COST_HISTORY_DAYS = 7
REEVALUATION_BLOCKING_CODES = ("COST_HISTORY_INSUFFICIENT", "CURRENT_CONFIG_EPOCH_TOO_NEW")
HASH_HEX_LENGTH = 24  # 96-bit display identifier; full SHA-256 remains available in provenance.
# Cost profile (contract 3.0.0): the risk-appetite thresholds a cost profile sets, grouped by technique. The group and
# field names are part of the contract; their values per profile are publisher policy, checked by the package gate.
COST_PROFILE_GROUPS: Dict[str, tuple] = {
    "size": ("spill_query_pct_downsize_safe", "p95_execution_ms_downsize_safe", "p99_execution_ms_downsize_safe"),
    "max_clusters": ("time_at_max_pct_safe_reduce", "max_cluster_cut_headroom_pct"),
    "min_clusters": ("time_at_min_pct_reduce", "queue_wait_p95_ms_safe", "min_cluster_busy_start_percentile"),
    "auto_stop": ("cold_start_added_share_max_pct",),
    "cross_technique": ("cost_change_min_annual_savings", "queue_query_pct_safe", "canary_p95_regression_pct", "growth_hold_weekly_ratio"),
}
_WAREHOUSE_REF_PATTERN = re.compile(r"^(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ConfidenceLevel(str, Enum):
    """Band of a Confidence score: HIGH >= 0.80, MEDIUM >= 0.60, LOW below; UNKNOWN only with a null score."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class PhotonStatus(str, Enum):
    """Photon state observed from current-epoch billing usage (product_features.is_photon). Observation only, never recommended."""
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class EvidenceQualityStatus(str, Enum):
    """How much of the required evidence (query sample, cost history, epoch maturity, source coverage) is present."""
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class AssessmentStatus(str, Enum):
    """Package-level outcome across all candidates of one warehouse/epoch."""
    OPTIMIZATION_AVAILABLE = "OPTIMIZATION_AVAILABLE"
    NO_CHANGE = "NO_CHANGE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RecommendationStatus(str, Enum):
    """Per-candidate decision status.

    NO_CHANGE keeps the current value; BLOCKED and INSUFFICIENT_EVIDENCE require blocker codes and carry no counterfactual.
    """
    ACTIONABLE = "ACTIONABLE"
    TEST_REQUIRED = "TEST_REQUIRED"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    NO_CHANGE = "NO_CHANGE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ExecutionState(str, Enum):
    """State of one plan step. PENDING_REEVALUATION means a time-clearable gate hides it; its pricing is CONTINGENT, never authoritative."""
    READY = "READY"
    READY_FOR_CANARY = "READY_FOR_CANARY"
    PENDING_REEVALUATION = "PENDING_REEVALUATION"
    BLOCKED = "BLOCKED"
    REQUIRED = "REQUIRED"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"


class PricingBasis(str, Enum):
    """Whether a plan step's projected economics may feed scenario/package economics (contract 1.1.0)."""
    AUTHORITATIVE = "AUTHORITATIVE"
    CONTINGENT_PENDING_REEVALUATION = "CONTINGENT_PENDING_REEVALUATION"
    # Figures replayed from the immediately preceding epoch (contract 1.2.0, reserved for Stage 2); never feed package economics.
    CONTINGENT_CARRIED_FORWARD = "CONTINGENT_CARRIED_FORWARD"


class ReevaluationStatus(str, Enum):
    """State of the re-evaluation window; WAITING_FOR_EVIDENCE is the only value today."""
    WAITING_FOR_EVIDENCE = "WAITING_FOR_EVIDENCE"


class MeasurementStatus(str, Enum):
    """Whether an observed configuration change has been measured for savings; NOT_MEASURED is the only value today."""
    NOT_MEASURED = "NOT_MEASURED"


class MatchPolicy(str, Enum):
    """How a carried-forward plan is matched to the observed change; EXACT_SINGLE_KNOB is the only policy today."""
    EXACT_SINGLE_KNOB = "EXACT_SINGLE_KNOB"


class MatchOutcome(str, Enum):
    """Result of replaying the preceding epoch's plan against the observed change (MATCHED, MISMATCH, or NOT_REPLAYABLE)."""
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    NOT_REPLAYABLE = "NOT_REPLAYABLE"


class CarriedForwardDisposition(str, Enum):
    """What became of one carried-forward step: done by the observed change, still waiting, or no longer valid."""
    COMPLETED = "COMPLETED"
    PENDING_REEVALUATION = "PENDING_REEVALUATION"
    INVALIDATED = "INVALIDATED"


class ScenarioStatus(str, Enum):
    """Role of a scenario in the package; at most one is RECOMMENDED and it must match recommended_plan.scenario_id."""
    RECOMMENDED = "RECOMMENDED"
    ALTERNATIVE = "ALTERNATIVE"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ScenarioType(str, Enum):
    """Kind of mutually exclusive execution path: in-place knob changes, warehouse-type migration, or the no-change baseline."""
    IN_PLACE_OPTIMIZATION = "IN_PLACE_OPTIMIZATION"
    WAREHOUSE_TYPE_MIGRATION = "WAREHOUSE_TYPE_MIGRATION"
    NO_CHANGE_BASELINE = "NO_CHANGE_BASELINE"


class CompatibilityStatus(str, Enum):
    """Aggregate migration compatibility; UNKNOWN is never read as COMPATIBLE."""
    COMPATIBLE = "COMPATIBLE"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class CompatibilityMode(str, Enum):
    """Where compatibility evidence came from: read-only REST API (B), human-in-the-loop fallback (A), both, or system tables only."""
    B_READ_ONLY_API = "B_READ_ONLY_API"
    A_HITL_FALLBACK = "A_HITL_FALLBACK"
    B_READ_ONLY_API_WITH_A_FALLBACK = "B_READ_ONLY_API_WITH_A_FALLBACK"
    SYSTEM_TABLE_ONLY = "SYSTEM_TABLE_ONLY"


class CheckStatus(str, Enum):
    """Result of one compatibility check; UNKNOWN means not determined, not passed."""
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EstimationStatus(str, Enum):
    """How a savings/cost range was produced.

    UNAVAILABLE must carry null ranges (unknown is never zero); NO_DELTA_MODELED is a known zero (no change, guardrail change or no steps);
    PRE_CANARY_MODELED_RANGE (migration or canary-gated) and PROVISIONAL_MODELED_RANGE are modeled but unvalidated; MODELED_RANGE is the plain model;
    MEASURED is for observed figures.
    """
    MODELED_RANGE = "MODELED_RANGE"
    PROVISIONAL_MODELED_RANGE = "PROVISIONAL_MODELED_RANGE"
    PRE_CANARY_MODELED_RANGE = "PRE_CANARY_MODELED_RANGE"
    MEASURED = "MEASURED"
    NO_DELTA_MODELED = "NO_DELTA_MODELED"
    UNAVAILABLE = "UNAVAILABLE"


class CostAvailability(str, Enum):
    """Which components of a CostBreakdown are known: all (COMPLETE), exactly one of DBU/AWS (PARTIAL), or none (UNAVAILABLE)."""
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class CostBasis(str, Enum):
    """Provenance of one cost component; UNAVAILABLE exactly when that component is null."""
    OBSERVED_DATABRICKS_BILLING = "OBSERVED_DATABRICKS_BILLING"
    OBSERVED_AWS_BILLING = "OBSERVED_AWS_BILLING"
    MODELED_AWS_LIST_PRICE = "MODELED_AWS_LIST_PRICE"
    MODELED_CURRENT_EPOCH = "MODELED_CURRENT_EPOCH"
    FORECAST = "FORECAST"
    COUNTERFACTUAL = "COUNTERFACTUAL"
    UNAVAILABLE = "UNAVAILABLE"


class TunableKnob(str, Enum):
    """Configuration knob a candidate or observed change refers to (values are the config field names).

    enable_photon and warehouse_channel are observation-only: they are never recommended because no priced effect exists.
    """
    WAREHOUSE_TYPE = "warehouse_type"
    WAREHOUSE_SIZE = "warehouse_size"
    MIN_CLUSTERS = "min_clusters"
    MAX_CLUSTERS = "max_clusters"
    AUTO_STOP_MINUTES = "auto_stop_minutes"
    WAREHOUSE_CHANNEL = "warehouse_channel"
    ENABLE_PHOTON = "enable_photon"


class Objective(str, Enum):
    """Goal a candidate serves."""
    COST = "COST"
    PERFORMANCE = "PERFORMANCE"
    COST_AND_PERFORMANCE = "COST_AND_PERFORMANCE"
    COST_GUARDRAIL = "COST_GUARDRAIL"
    STABILITY = "STABILITY"
    RELIABILITY = "RELIABILITY"
    OPERATIONS = "OPERATIONS"
    MIGRATION = "MIGRATION"


class DependencyType(str, Enum):
    """Kind of thing a DependencyRef points at."""
    CANDIDATE = "CANDIDATE"
    TECHNIQUE = "TECHNIQUE"
    KNOB = "KNOB"
    COMPATIBILITY_CHECK = "COMPATIBILITY_CHECK"
    EVIDENCE = "EVIDENCE"


class ValidationType(str, Enum):
    """How a plan step is validated before or after the change."""
    NONE = "NONE"
    A_B_CANARY = "A_B_CANARY"
    HITL = "HITL"
    COMPATIBILITY = "COMPATIBILITY"
    REMEASURE = "REMEASURE"


class PlanStepType(str, Enum):
    """Kind of execution-plan step; only CANDIDATE steps reference a candidate and knob."""
    CANDIDATE = "CANDIDATE"
    REMEASURE = "REMEASURE"
    COMPATIBILITY_VALIDATION = "COMPATIBILITY_VALIDATION"
    CANARY = "CANARY"
    COMPARE = "COMPARE"
    ROLLBACK_GATE = "ROLLBACK_GATE"


class BoundName(str, Enum):
    """Which bound of a low/base/high range a scenario-envelope point comes from."""
    LOW = "LOW"
    BASE = "BASE"
    HIGH = "HIGH"


class WorkloadLatencyClass(str, Enum):
    """Latency profile of the workload; UNKNOWN when not derivable."""
    INTERACTIVE = "INTERACTIVE"
    MIXED = "MIXED"
    BATCH = "BATCH"
    UNKNOWN = "UNKNOWN"


class ConcurrencyPattern(str, Enum):
    """Concurrency shape of the workload; UNKNOWN when not derivable."""
    STEADY = "STEADY"
    BURSTY = "BURSTY"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ActivityPattern(str, Enum):
    """Time pattern of workload activity; UNKNOWN when not derivable."""
    CONTINUOUS = "CONTINUOUS"
    BUSINESS_HOURS = "BUSINESS_HOURS"
    SCHEDULED = "SCHEDULED"
    SPORADIC = "SPORADIC"
    UNKNOWN = "UNKNOWN"


class RealizationStatus(str, Enum):
    """Lifecycle of the post-change measurement for the package; NOT_APPLIED requires no realization records."""
    NOT_APPLIED = "NOT_APPLIED"
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    VALIDATING = "VALIDATING"
    REALIZED = "REALIZED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


class ValidationOutcome(str, Enum):
    """Result of a post-change validation window."""
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"


class CostProfileName(str, Enum):
    """Risk appetite a warehouse was evaluated under: how much performance risk a supported cut may take."""
    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    CONSERVATIVE = "conservative"


class CostProfileSource(str, Enum):
    """Why the warehouse got its cost profile: the default, a nonprod warehouse tag, or a --cost-profile override."""
    DEFAULT = "DEFAULT"
    TAG_DEFAULT = "TAG_DEFAULT"
    CLI_ALL = "CLI_ALL"
    CLI_WAREHOUSE = "CLI_WAREHOUSE"


class RealizationOutcomeStatus(str, Enum):
    """Final outcome of one realized change."""
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    """Raise ValueError(message) when the condition is false; the single failure path of every validator."""
    if not condition:
        raise ValueError(message)


_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FINGERPRINT_PATTERN = re.compile(r"^SHA256:[A-F0-9]{64}$")


def _is_iso8601(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def _validate_identifier(value: str, prefix: str, *, length: int = HASH_HEX_LENGTH) -> None:
    """Require PREFIX- followed by exactly ``length`` uppercase hex characters."""
    expected = f"{prefix}-"
    _require(value.startswith(expected), f"{value!r} must start with {expected!r}")
    suffix = value[len(expected):]
    _require(len(suffix) == length, f"{value!r} must contain {length} hex characters after {expected}")
    _require(all(ch in "0123456789ABCDEF" for ch in suffix), f"{value!r} must use uppercase hex")


def _validate_nonnegative(value: Optional[float], field_name: str) -> None:
    if value is not None:
        _require(value >= 0, f"{field_name} must be >= 0")


def _validate_pct(value: Optional[float], field_name: str) -> None:
    if value is not None:
        _require(0 <= value <= 100, f"{field_name} must be in [0, 100]")


def _parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso_utc(value: dt.datetime) -> str:
    """UTC ISO string at second precision, rounded UP so a published 'earliest' time is never before the true gate."""
    value = value.astimezone(dt.timezone.utc)
    if value.microsecond:
        value = value.replace(microsecond=0) + dt.timedelta(seconds=1)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def expected_earliest_reevaluation_time(start_time: str, blocking_codes: Sequence[str], analysis_time: str) -> str:
    """Pure function of the epoch start, the analysis time and the active gates (contract 2.0.0).

    readiness gate: 00:00Z of (analysis date + 1 day), when one more day can have settled (the settled basis ends two
    days before the analysis day), never before the epoch start (how many more days are needed depends on future
    billing, so this is the next chance only). cost-history gate: 00:00Z of (epoch start date + REEVALUATION_COST_HISTORY_DAYS), the earliest time
    the last required billing day can be fully covered.  The result is the later of the active gates.
    """
    start = _parse_utc(start_time)
    times = []
    if "CURRENT_CONFIG_EPOCH_TOO_NEW" in blocking_codes:
        analysis = _parse_utc(analysis_time).replace(hour=0, minute=0, second=0, microsecond=0)
        times.append(max(start, analysis + dt.timedelta(days=1)))
    if "COST_HISTORY_INSUFFICIENT" in blocking_codes:
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        times.append(midnight + dt.timedelta(days=REEVALUATION_COST_HISTORY_DAYS))
    _require(bool(times), "at least one blocking code is required")
    return _iso_utc(max(times))


def to_primitive(value: Any) -> Any:
    """Convert nested contract objects to JSON-compatible primitives.

    Dataclasses become dicts in field-declaration order, enums their value, mappings string-keyed dicts, tuples lists, and
    datetimes UTC ISO strings with a Z suffix. Plain values pass through.
    """
    if dataclasses.is_dataclass(value):
        return {f.name: to_primitive(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): to_primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(v) for v in value]
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return value


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def _decimal_string(value: Any) -> str:
    """Normalize a number to a canonical decimal string (1, 1.0 and 1.00 all become '1'; zero is '0') for hashing."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Unsupported numeric hash value: {value!r}") from exc
    if d == 0:
        return "0"
    normalized = d.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonicalize_for_hash(value: Any) -> Any:
    """Canonicalize an explicitly chosen identity payload.

    Rules for hash payloads only:
    * dict keys are sorted by JSON serialization;
    * booleans/null remain native;
    * all numbers become normalized decimal strings, avoiding 1 vs 1.0 drift;
    * datetimes become UTC ISO-8601 strings;
    * list order is preserved because callers decide whether a list is semantic order or
      pre-sort it for set-like semantics.
    Raises TypeError for anything else.
    """
    value = to_primitive(value)
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return _decimal_string(value)
    if isinstance(value, Mapping):
        return {str(k): canonicalize_for_hash(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonicalize_for_hash(v) for v in value]
    raise TypeError(f"Unsupported canonical hash type: {type(value).__name__}")


def canonical_json_for_hash(payload: Mapping[str, Any]) -> str:
    """Serialize a canonicalized payload as compact, key-sorted, non-ASCII-escaped JSON that rejects NaN/Infinity."""
    return json.dumps(
        canonicalize_for_hash(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(payload: Mapping[str, Any]) -> str:
    """Uppercase SHA-256 hex digest of the canonical JSON of ``payload``."""
    return hashlib.sha256(canonical_json_for_hash(payload).encode("utf-8")).hexdigest().upper()


def content_id(prefix: str, payload: Mapping[str, Any]) -> str:
    """Content-addressed public ID: ``PREFIX-`` plus the first HASH_HEX_LENGTH (24) hex characters of the payload hash."""
    return f"{prefix}-{sha256_hex(payload)[:HASH_HEX_LENGTH]}"


def make_epoch_id(
    *,
    workspace_id: str,
    warehouse_id: str,
    start_time: str,
    warehouse_type: str,
    warehouse_size: str,
    min_clusters: int,
    max_clusters: int,
    auto_stop_minutes: int,
    warehouse_channel: str,
) -> str:
    """Create the immutable config-epoch ID (EPOCH-).

    Hashed: contract_version, workspace_id, warehouse_id, the epoch start time and the authoritative system.compute.warehouses
    knobs (type, size, min/max clusters, auto-stop, channel). Photon is intentionally excluded: it is an observed billing
    feature in this product, not an authoritative epoch field.
    """
    return content_id("EPOCH", {
        "contract_version": CONTRACT_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "start_time": start_time,
        "warehouse_type": warehouse_type,
        "warehouse_size": warehouse_size,
        "min_clusters": min_clusters,
        "max_clusters": max_clusters,
        "auto_stop_minutes": auto_stop_minutes,
        "warehouse_channel": warehouse_channel,
    })


def evidence_id_payload(
    *,
    workspace_id: str,
    warehouse_id: str,
    epoch_id: str,
    metric: str,
    value: Any,
    unit: str,
    source: str,
    source_field: str,
    observation_window: str,
    threshold: Any,
    comparator: str,
) -> Dict[str, Any]:
    """Hash payload of one evidence record.

    Contains the identity and measured content (workspace, warehouse, epoch, metric, value, unit, source, source_field,
    observation_window, threshold, comparator). Presentation prose (interpretation) and epoch_scope are not part of the ID.
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "epoch_id": epoch_id,
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": source,
        "source_field": source_field,
        "observation_window": observation_window,
        "threshold": threshold,
        "comparator": comparator,
    }


def make_evidence_id(**kwargs: Any) -> str:
    """EVID- ID of one evidence record: the content hash of ``evidence_id_payload``."""
    return content_id("EVID", evidence_id_payload(**kwargs))


def make_evidence_fingerprint(
    *,
    workspace_id: str,
    warehouse_id: str,
    epoch_id: str,
    evidence_records: Sequence[Mapping[str, Any]],
) -> str:
    """Package-level ``SHA256:<64 hex>`` fingerprint of the decision-relevant evidence registry.

    Hashes the ID, measured content and epoch_scope of every record, sorted by evidence_id; presentation prose
    (interpretation) is excluded.
    """
    normalized = []
    for item in evidence_records:
        normalized.append({
            "evidence_id": item["evidence_id"],
            "metric": item["metric"],
            "value": item.get("value"),
            "unit": item["unit"],
            "source": item["source"],
            "source_field": item["source_field"],
            "epoch_scope": item["epoch_scope"],
            "observation_window": item["observation_window"],
            "threshold": item.get("threshold"),
            "comparator": item.get("comparator", ""),
        })
    payload = {
        "contract_version": CONTRACT_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "epoch_id": epoch_id,
        "evidence": sorted(normalized, key=lambda x: x["evidence_id"]),
    }
    digest = hashlib.sha256(canonical_json_for_hash(payload).encode("utf-8")).hexdigest().upper()
    return f"SHA256:{digest}"


def make_candidate_id(
    *,
    workspace_id: str,
    warehouse_id: str,
    epoch_id: str,
    technique_id: str,
    knob: str,
    current_value: Any,
    recommended_value: Any,
    evidence_refs: Sequence[str],
    policy_version: str,
    analyzer_version: str,
    optimizer_version: str,
    estimator_version: str,
    reviewer_version: str,
    decision_graph_version: str,
    cost_profile: Mapping[str, Any],
) -> str:
    """CAND- ID of one technique decision against one warehouse/epoch/evidence set.

    Hashed: workspace, warehouse, epoch, technique, knob, current and recommended value, the sorted unique evidence_refs,
    every component version (policy, analyzer, optimizer, estimator, reviewer, decision graph) and the cost profile's hash
    payload (name and applied thresholds). Economics and prose are not hashed.
    """
    payload = {
        "contract_version": CONTRACT_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "epoch_id": epoch_id,
        "technique_id": technique_id,
        "knob": knob,
        "current_value": current_value,
        "recommended_value": recommended_value,
        "evidence_refs": sorted(set(evidence_refs)),
        "policy_version": policy_version,
        "analyzer_version": analyzer_version,
        "optimizer_version": optimizer_version,
        "estimator_version": estimator_version,
        "reviewer_version": reviewer_version,
        "decision_graph_version": decision_graph_version,
        "cost_profile": dict(cost_profile),
    }
    return content_id("CAND", payload)


def make_scenario_id(
    *,
    workspace_id: str,
    warehouse_id: str,
    epoch_id: str,
    scenario_type: str,
    ordered_candidate_ids: Sequence[str],
    compatibility_status: str,
    policy_version: str,
    bundle_estimator_version: str,
    decision_graph_version: str,
    cost_profile: Mapping[str, Any],
) -> str:
    """SCN- ID of one mutually exclusive execution path.

    Hashed: workspace, warehouse, epoch, scenario_type, the ORDERED candidate ids (order is execution order), compatibility
    status, the policy, bundle-estimator and decision-graph versions and the cost profile's hash payload. Economics and
    prose are not hashed.
    """
    payload = {
        "contract_version": CONTRACT_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "epoch_id": epoch_id,
        "scenario_type": scenario_type,
        "ordered_candidate_ids": list(ordered_candidate_ids),
        "compatibility_status": compatibility_status,
        "policy_version": policy_version,
        "bundle_estimator_version": bundle_estimator_version,
        "decision_graph_version": decision_graph_version,
        "cost_profile": dict(cost_profile),
    }
    return content_id("SCN", payload)


def make_realization_id(
    *,
    recommendation_package_id: str,
    scenario_id: str,
    candidate_id: Optional[str],
    validation_window_start: str,
    validation_window_end: str,
    applied_config: Mapping[str, Any],
) -> str:
    """REAL- ID of one post-change measurement episode.

    Hashed: package id, scenario id, candidate id (may be None), the validation window bounds and the applied config.
    """
    payload = {
        "contract_version": CONTRACT_VERSION,
        "recommendation_package_id": recommendation_package_id,
        "scenario_id": scenario_id,
        "candidate_id": candidate_id,
        "validation_window_start": validation_window_start,
        "validation_window_end": validation_window_end,
        "applied_config": dict(applied_config),
    }
    return content_id("REAL", payload)


# ---------------------------------------------------------------------------
# Stable value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Confidence:
    """Deterministic confidence: a score in [0, 1] with its level band, optional sub-score components and method.

    A null score means unknown and requires level UNKNOWN; it never means zero confidence.
    """
    score: Optional[float]
    level: ConfidenceLevel
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    method: Optional[str] = None

    def validate(self) -> None:
        """Reject a score or component outside [0, 1], a level that disagrees with the score band, or a null score with a level other than UNKNOWN."""
        if self.score is None:
            _require(self.level == ConfidenceLevel.UNKNOWN, "null confidence score requires UNKNOWN level")
        else:
            _require(0 <= self.score <= 1, "confidence.score must be in [0, 1]")
            expected = ConfidenceLevel.HIGH if self.score >= 0.80 else ConfidenceLevel.MEDIUM if self.score >= 0.60 else ConfidenceLevel.LOW
            _require(self.level == expected, f"confidence level {self.level.value} does not match score {self.score}; expected {expected.value}")
        for name, value in self.components.items():
            if value is not None:
                _require(0 <= value <= 1, f"confidence component {name} must be in [0, 1]")


@dataclass(frozen=True)
class Range3:
    """Low/base/high triple of one economic quantity (savings are signed, costs are not). A modeled range, not a probability interval."""
    low: float
    base: float
    high: float

    def validate(self, *, nonnegative: bool = False, field_name: str = "range") -> None:
        """Reject low <= base <= high violations; with ``nonnegative`` (costs) also a negative low."""
        _require(self.low <= self.base <= self.high, f"{field_name} must satisfy low <= base <= high")
        if nonnegative:
            _require(self.low >= 0, f"{field_name}.low must be >= 0")


@dataclass(frozen=True)
class CostBreakdown:
    """One cost figure split into DBU, AWS and total, in the package currency.

    A component is None when unknown (never 0). COMPLETE needs all three, PARTIAL exactly one of DBU/AWS and a null total,
    UNAVAILABLE none; each basis is UNAVAILABLE exactly when its component is None.
    """
    dbu_cost: Optional[float]
    aws_cost: Optional[float]
    total_cost: Optional[float]
    availability: CostAvailability
    dbu_basis: CostBasis
    aws_basis: CostBasis

    def validate(self) -> None:
        """Reject negative costs, total != dbu + aws (tolerance 0.02), availability or basis that disagrees with which components are null."""
        _validate_nonnegative(self.dbu_cost, "dbu_cost")
        _validate_nonnegative(self.aws_cost, "aws_cost")
        _validate_nonnegative(self.total_cost, "total_cost")
        if self.dbu_cost is not None and self.aws_cost is not None and self.total_cost is not None:
            _require(abs((self.dbu_cost + self.aws_cost) - self.total_cost) <= 0.02, "total_cost must equal dbu_cost + aws_cost")
        if self.availability == CostAvailability.COMPLETE:
            _require(self.dbu_cost is not None and self.aws_cost is not None and self.total_cost is not None, "COMPLETE cost requires all components")
        elif self.availability == CostAvailability.PARTIAL:
            _require((self.dbu_cost is None) != (self.aws_cost is None), "PARTIAL cost requires exactly one of DBU/AWS components")
            _require(self.total_cost is None, "PARTIAL cost must not publish total_cost")
        elif self.availability == CostAvailability.UNAVAILABLE:
            _require(self.dbu_cost is None and self.aws_cost is None and self.total_cost is None, "UNAVAILABLE cost requires null components")
        _require((self.dbu_cost is None) == (self.dbu_basis == CostBasis.UNAVAILABLE), "dbu_basis must reflect DBU availability")
        _require((self.aws_cost is None) == (self.aws_basis == CostBasis.UNAVAILABLE), "aws_basis must reflect AWS availability")


@dataclass(frozen=True)
class ComponentRanges:
    """DBU, AWS and total ranges of one economic quantity; each is None when that component is unknown. Savings are signed, costs non-negative."""
    dbu: Optional[Range3]
    aws: Optional[Range3]
    total: Optional[Range3]

    def validate(self, *, nonnegative: bool, field_name: str) -> None:
        """Validate each present range; when all three exist, reject total != dbu + aws on any bound (tolerance 0.03)."""
        for component, value in (("dbu", self.dbu), ("aws", self.aws), ("total", self.total)):
            if value is not None:
                value.validate(nonnegative=nonnegative, field_name=f"{field_name}.{component}")
        if self.dbu is not None and self.aws is not None and self.total is not None:
            for bound in ("low", "base", "high"):
                expected = getattr(self.dbu, bound) + getattr(self.aws, bound)
                actual = getattr(self.total, bound)
                _require(abs(expected - actual) <= 0.03, f"{field_name}.{bound} total must equal DBU + AWS")


def _require_unavailable_means_null(status: "EstimationStatus", *ranges: Optional["ComponentRanges"], field_name: str) -> None:
    """Unknown cost is unavailable/null, never zero: an UNAVAILABLE estimate must not carry any number."""
    if status == EstimationStatus.UNAVAILABLE:
        for r in ranges:
            _require(
                r is None or (r.dbu is None and r.aws is None and r.total is None),
                f"{field_name}: an UNAVAILABLE estimate must have null ranges, never zero",
            )


# ---------------------------------------------------------------------------
# Warehouse identity and current state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentEpoch:
    """Identity of the current configuration epoch (immutable regime): its EPOCH- id, start time and age in days. Always current in a package."""
    epoch_id: str
    start_time: str
    age_days: int
    is_current: bool

    def validate(self) -> None:
        """Reject a malformed EPOCH- id, a non-ISO-8601 start_time, negative age_days, or is_current false."""
        _validate_identifier(self.epoch_id, "EPOCH")
        _require(_is_iso8601(self.start_time), "current_epoch.start_time must be ISO-8601")
        _require(self.age_days >= 0, "current_epoch.age_days must be >= 0")
        _require(self.is_current is True, "current_epoch.is_current must be true in a recommendation package")


@dataclass(frozen=True)
class WarehouseIdentity:
    """Public identity of the assessed warehouse; (workspace_id, warehouse_id) is the key. owner is None when billing does not attribute one."""
    workspace_id: str
    warehouse_id: str
    warehouse_name: str
    owner: Optional[str]
    region: str
    current_epoch: CurrentEpoch

    def validate(self) -> None:
        """Reject blank workspace_id, warehouse_id, warehouse_name or region and an invalid current_epoch."""
        _require(bool(self.workspace_id.strip()), "workspace_id is required")
        _require(bool(self.warehouse_id.strip()), "warehouse_id is required")
        _require(bool(self.warehouse_name.strip()), "warehouse_name is required")
        _require(bool(self.region.strip()), "region is required")
        self.current_epoch.validate()


@dataclass(frozen=True)
class PhotonConfigObservation:
    """Photon state observed from billing, with confidence and coverage (percentages in [0, 100]).

    Observation only: not a configuration read and never a recommendation.
    """
    status: PhotonStatus
    evidence_source: str
    confidence: Confidence
    enabled_usage_pct: Optional[float] = None
    known_feature_coverage_pct: Optional[float] = None
    freshness_days: Optional[int] = None

    def validate(self) -> None:
        """Reject percentages outside [0, 100], negative freshness_days, or HIGH/MEDIUM confidence on an UNKNOWN status."""
        self.confidence.validate()
        _validate_pct(self.enabled_usage_pct, "photon.enabled_usage_pct")
        _validate_pct(self.known_feature_coverage_pct, "photon.known_feature_coverage_pct")
        if self.freshness_days is not None:
            _require(self.freshness_days >= 0, "photon.freshness_days must be >= 0")
        if self.status == PhotonStatus.UNKNOWN:
            _require(self.confidence.level in {ConfidenceLevel.LOW, ConfidenceLevel.UNKNOWN}, "UNKNOWN Photon should not carry HIGH/MEDIUM confidence")


@dataclass(frozen=True)
class CurrentConfig:
    """Current-epoch warehouse configuration plus the Photon observation.

    The whole object (including the observation) enters the package hash and the epoch id is derived from its knobs.
    """
    warehouse_type: str
    warehouse_size: str
    min_clusters: int
    max_clusters: int
    auto_stop_minutes: int
    warehouse_channel: str
    photon: PhotonConfigObservation

    def validate(self) -> None:
        """Reject blank type/size/channel, min_clusters outside [1, 30], max_clusters below min or above 40, or negative auto_stop_minutes."""
        _require(bool(self.warehouse_type), "warehouse_type is required")
        _require(bool(self.warehouse_size), "warehouse_size is required")
        _require(self.min_clusters >= 1, "min_clusters must be >= 1")
        _require(self.min_clusters <= 30, "min_clusters must be <= 30")
        _require(self.max_clusters >= self.min_clusters, "max_clusters must be >= min_clusters")
        _require(self.max_clusters <= 40, "max_clusters must be <= 40")
        _require(self.auto_stop_minutes >= 0, "auto_stop_minutes must be >= 0")
        _require(bool(self.warehouse_channel), "warehouse_channel is required")
        self.photon.validate()


# ---------------------------------------------------------------------------
# Workload profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuerySourceStat:
    """Query count and share (percent in [0, 100]) of one query source in the current epoch."""
    query_count: int
    query_pct: float

    def validate(self) -> None:
        """Reject a negative count or a percentage outside [0, 100]."""
        _require(self.query_count >= 0, "query_count must be >= 0")
        _validate_pct(self.query_pct, "query_pct")


@dataclass(frozen=True)
class ClientApplicationStat:
    """Query count of one client application in the current epoch."""
    query_count: int

    def validate(self) -> None:
        """Reject a negative query_count."""
        _require(self.query_count >= 0, "client application query_count must be >= 0")


@dataclass(frozen=True)
class WorkloadActivity:
    """Thirty-day query volume and active-day count (0..30) with the per-active-day average."""
    queries_30d: int
    active_days_30d: int
    queries_per_active_day: float

    def validate(self) -> None:
        """Reject negative counts, active_days_30d outside [0, 30], or a negative average."""
        _require(self.queries_30d >= 0, "queries_30d must be >= 0")
        _require(0 <= self.active_days_30d <= 30, "active_days_30d must be in [0, 30]")
        _require(self.queries_per_active_day >= 0, "queries_per_active_day must be >= 0")


@dataclass(frozen=True)
class WorkloadCharacteristics:
    """Derived workload labels; each is UNKNOWN when it cannot be derived."""
    latency_class: WorkloadLatencyClass
    concurrency_pattern: ConcurrencyPattern
    activity_pattern: ActivityPattern


@dataclass(frozen=True)
class WorkloadProfile:
    """Multi-label query-source profile (a query can carry several sources), client applications, activity and derived characteristics."""
    primary_query_source: str
    query_sources: Dict[str, QuerySourceStat]
    client_applications: Dict[str, ClientApplicationStat]
    activity: WorkloadActivity
    workload_characteristics: WorkloadCharacteristics
    multi_label: bool = True

    def validate(self) -> None:
        """Reject a blank primary_query_source (or one missing from a non-empty query_sources), invalid stats or activity, or multi_label false."""
        _require(bool(self.primary_query_source), "primary_query_source is required")
        if self.query_sources:
            _require(self.primary_query_source in self.query_sources, "primary_query_source must be present in query_sources")
        for stat in self.query_sources.values():
            stat.validate()
        for stat in self.client_applications.values():
            stat.validate()
        self.activity.validate()
        _require(self.multi_label is True, "query-source profile is explicitly multi-label")


# ---------------------------------------------------------------------------
# Baseline economics and assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastInfo:
    """Model name, confidence and prose assumptions behind the forward 365-day no-change cost."""
    model: str
    confidence: Confidence
    assumptions: List[str]

    def validate(self) -> None:
        """Reject a blank model or invalid confidence."""
        _require(bool(self.model), "forecast.model is required")
        self.confidence.validate()


@dataclass(frozen=True)
class CostBaseline:
    """Trailing-365d actual and forward no-change 365d cost of the warehouse in one currency.

    Components are None when unknown; DBU and AWS stay separate.
    """
    currency: str
    actual_365d: CostBreakdown
    forward_365d: CostBreakdown
    forecast: ForecastInfo

    def validate(self) -> None:
        """Reject a currency that is not a 3-letter uppercase code or an invalid breakdown/forecast."""
        _require(len(self.currency) == 3 and self.currency.isalpha() and self.currency.upper() == self.currency, "currency must be ISO-like uppercase 3-letter code")
        self.actual_365d.validate()
        self.forward_365d.validate()
        self.forecast.validate()


@dataclass(frozen=True)
class AssessmentSummary:
    """Candidate counts by RecommendationStatus; candidate_count is the total and must equal the sum of the status counts."""
    candidate_count: int
    actionable_count: int
    test_required_count: int
    review_count: int
    blocked_count: int
    no_change_count: int
    not_applicable_count: int
    insufficient_evidence_count: int

    def validate(self) -> None:
        """Reject negative counts or a candidate_count that differs from the sum of the status counts."""
        values = [
            self.candidate_count,
            self.actionable_count,
            self.test_required_count,
            self.review_count,
            self.blocked_count,
            self.no_change_count,
            self.not_applicable_count,
            self.insufficient_evidence_count,
        ]
        _require(all(v >= 0 for v in values), "assessment counts must be >= 0")
        subtotal = sum(values[1:])
        _require(self.candidate_count == subtotal, "candidate_count must equal status counts")


@dataclass(frozen=True)
class EvidenceQuality:
    """Sufficiency flags (query sample, cost history, epoch maturity), source coverage percent and confidence for the whole evidence set."""
    status: EvidenceQualityStatus
    confidence: Confidence
    query_sample_sufficient: bool
    cost_history_sufficient: bool
    epoch_mature: bool
    source_coverage_pct: float

    def validate(self) -> None:
        """Reject invalid confidence or a source_coverage_pct outside [0, 100]."""
        self.confidence.validate()
        _validate_pct(self.source_coverage_pct, "source_coverage_pct")


@dataclass(frozen=True)
class Assessment:
    """Package-level status, candidate counts and evidence quality. Counts must describe the actual candidate list (checked by the package)."""
    overall_status: AssessmentStatus
    summary: AssessmentSummary
    evidence_quality: EvidenceQuality

    def validate(self) -> None:
        """Validate the summary and evidence quality."""
        self.summary.validate()
        self.evidence_quality.validate()


# ---------------------------------------------------------------------------
# Evidence registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """One fact in the evidence registry, keyed by its EVID- id.

    The id hashes the measured content (see evidence_id_payload); interpretation is presentation prose and not hashed.
    value and threshold may be None (no numeric value or threshold).
    """
    evidence_id: str
    metric: str
    value: Any
    unit: str
    source: str
    source_field: str
    epoch_scope: str
    observation_window: str
    threshold: Any
    comparator: str
    interpretation: str

    def validate(self) -> None:
        """Reject a malformed EVID- id or a blank metric, unit, source, source_field, epoch_scope, observation_window or interpretation."""
        _validate_identifier(self.evidence_id, "EVID")
        _require(bool(self.metric), "evidence.metric is required")
        _require(bool(self.unit), "evidence.unit is required")
        _require(bool(self.source), "evidence.source is required")
        _require(bool(self.source_field), "evidence.source_field is required")
        _require(bool(self.epoch_scope), "evidence.epoch_scope is required")
        _require(bool(self.observation_window), "evidence.observation_window is required")
        _require(bool(self.interpretation), "evidence.interpretation is required")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyRef:
    """Reference from a candidate or step to a candidate, technique, knob, compatibility check or evidence item, with an optional condition."""
    dependency_type: DependencyType
    ref_id: str
    condition: Optional[str] = None

    def validate(self) -> None:
        """Reject a blank ref_id."""
        _require(bool(self.ref_id), "dependency.ref_id is required")


@dataclass(frozen=True)
class IndependentEconomics:
    """Isolated one-technique counterfactual of a candidate.

    Never additive across candidates (is_additive must be False): combined effects are priced only by scenarios. An UNAVAILABLE
    estimate has null ranges; a known zero is NO_DELTA_MODELED.
    """
    is_additive: bool
    estimation_status: EstimationStatus
    baseline_365d: CostBreakdown
    estimated_cost_365d: ComponentRanges
    savings_365d: ComponentRanges

    def validate(self) -> None:
        """Reject is_additive true, invalid baseline or ranges (costs non-negative, savings signed), or numbers on an UNAVAILABLE estimate."""
        _require(self.is_additive is False, "candidate economics must be non-additive")
        self.baseline_365d.validate()
        self.estimated_cost_365d.validate(nonnegative=True, field_name="estimated_cost_365d")
        self.savings_365d.validate(nonnegative=False, field_name="savings_365d")
        _require_unavailable_means_null(self.estimation_status, self.estimated_cost_365d, self.savings_365d, field_name="independent economics")


@dataclass(frozen=True)
class CandidateRecommendation:
    """One technique decision for one knob of one warehouse/epoch.

    Ids are content-addressed (make_candidate_id); evidence_refs are unique registry ids. Economics are standalone and
    non-additive; invalidates lists knobs whose analysis this change makes stale.
    """
    candidate_id: str
    technique_id: str
    knob: TunableKnob
    status: RecommendationStatus
    current_value: Any
    recommended_value: Any
    objectives: List[Objective]
    rationale: str
    evidence_refs: List[str]
    blockers: List[str]
    dependencies: List[DependencyRef]
    invalidates: List[TunableKnob]
    confidence: Confidence
    independent_economics: IndependentEconomics

    def validate(self, *, evidence_ids: Iterable[str]) -> None:
        """Reject a malformed CAND- id, a technique_id outside the WH-...-NNN form, empty objectives or rationale, duplicate or unknown evidence_refs, invalid
        dependencies, confidence or economics, a NO_CHANGE candidate that changes the value, a NOT_APPLICABLE candidate claiming savings, and a BLOCKED or
        INSUFFICIENT_EVIDENCE candidate without blocker codes.
        """
        _validate_identifier(self.candidate_id, "CAND")
        _require(self.technique_id.startswith("WH-") and self.technique_id[-4:-3] == "-" and self.technique_id[-3:].isdigit(), "technique_id must use WH-...-NNN catalog form")
        _require(len(self.objectives) >= 1, "candidate.objectives must not be empty")
        _require(bool(self.rationale), "candidate.rationale is required")
        evidence_set = set(evidence_ids)
        _require(len(self.evidence_refs) == len(set(self.evidence_refs)), "candidate.evidence_refs must be unique")
        _require(all(ref in evidence_set for ref in self.evidence_refs), "candidate references unknown evidence")
        for ref in self.evidence_refs:
            _validate_identifier(ref, "EVID")
        for dependency in self.dependencies:
            dependency.validate()
        self.confidence.validate()
        self.independent_economics.validate()
        if self.status == RecommendationStatus.NO_CHANGE:
            _require(self.current_value == self.recommended_value, "NO_CHANGE candidate must preserve value")
        if self.status == RecommendationStatus.NOT_APPLICABLE:
            _require(self.independent_economics.savings_365d.total is None or self.independent_economics.savings_365d.total.base == 0, "NOT_APPLICABLE cannot claim savings")
        if self.status in {RecommendationStatus.BLOCKED, RecommendationStatus.INSUFFICIENT_EVIDENCE}:
            _require(len(self.blockers) >= 1, f"{self.status.value} candidate requires blocker codes")


# ---------------------------------------------------------------------------
# Scenarios and execution plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatibilityCheck:
    """One migration compatibility check with its status, whether a human must confirm it, and supporting evidence ids."""
    check: str
    status: CheckStatus
    requires_hitl: bool
    evidence_refs: List[str] = field(default_factory=list)
    details: Optional[str] = None


@dataclass(frozen=True)
class Compatibility:
    """Aggregate migration compatibility of a scenario: status, evidence mode and the individual checks."""
    status: CompatibilityStatus
    mode: CompatibilityMode
    checks: List[CompatibilityCheck]


@dataclass(frozen=True)
class ValidationGate:
    """How a plan step is validated and how long to wait before re-measuring (days)."""
    validation_type: ValidationType
    success_metrics: List[str]
    remeasure_days: Optional[int] = None
    action_after: Optional[str] = None

    def validate(self) -> None:
        """Reject remeasure_days outside [1, 90]."""
        if self.remeasure_days is not None:
            _require(1 <= self.remeasure_days <= 90, "remeasure_days must be in [1, 90]")


@dataclass(frozen=True)
class StepProjection:
    """Stateful savings for one plan step, signed (positive = savings), with its pricing basis.

    ``running_savings_365d`` is cumulative up to and including this step. ``step_savings_365d`` (contract 1.3.0) is this
    step's own change on its pricing chain, per realization track (running savings after this step minus after the
    previous step on the same chain): low/high are the smallest/largest track change, base is the base track's change.
    """
    pricing_basis: PricingBasis
    running_savings_365d: ComponentRanges
    step_savings_365d: Optional[ComponentRanges] = None

    def validate(self) -> None:
        """Reject invalid ranges and a step_savings component that is null unless the matching running_savings component is null."""
        self.running_savings_365d.validate(nonnegative=False, field_name="plan_step.projected_economics.running_savings_365d")
        if self.step_savings_365d is not None:
            self.step_savings_365d.validate(nonnegative=False, field_name="plan_step.projected_economics.step_savings_365d")
            for component in ("dbu", "aws", "total"):
                _require(
                    (getattr(self.step_savings_365d, component) is None) == (getattr(self.running_savings_365d, component) is None),
                    f"plan_step.projected_economics.step_savings_365d.{component} must be null exactly when running_savings_365d.{component} is null",
                )


@dataclass(frozen=True)
class PlanStep:
    """One ordered step of a scenario's execution plan.

    CANDIDATE steps reference a candidate, technique and knob; other step types carry an action. projected_economics is optional.
    """
    step: int
    step_type: PlanStepType
    candidate_id: Optional[str]
    technique_id: Optional[str]
    knob: Optional[TunableKnob]
    action: Optional[str]
    current_value: Any
    recommended_value: Any
    execution_state: ExecutionState
    confidence: Confidence
    validation: ValidationGate
    dependencies: List[DependencyRef]
    invalidates: List[TunableKnob]
    projected_economics: Optional[StepProjection] = None

    def validate(self, *, candidate_ids: Iterable[str]) -> None:
        """Reject a step number below 1, invalid confidence/validation/dependencies, a pricing basis other than AUTHORITATIVE or CONTINGENT_PENDING_REEVALUATION, a
        pending step that is not CONTINGENT (or a non-pending priced step that is), and a CANDIDATE step with an unknown candidate or missing technique/knob (or
        any other step without an action).
        """
        _require(self.step >= 1, "plan step must be >= 1")
        self.confidence.validate()
        self.validation.validate()
        if self.projected_economics is not None:
            self.projected_economics.validate()
            _require(
                self.projected_economics.pricing_basis in (PricingBasis.AUTHORITATIVE, PricingBasis.CONTINGENT_PENDING_REEVALUATION),
                "plan steps may only use AUTHORITATIVE or CONTINGENT_PENDING_REEVALUATION pricing",
            )
            pending = self.execution_state == ExecutionState.PENDING_REEVALUATION
            _require(
                pending == (self.projected_economics.pricing_basis == PricingBasis.CONTINGENT_PENDING_REEVALUATION),
                "a PENDING_REEVALUATION step must be priced CONTINGENT and every other priced step AUTHORITATIVE",
            )
        for dependency in self.dependencies:
            dependency.validate()
        if self.step_type == PlanStepType.CANDIDATE:
            _require(self.candidate_id is not None, "candidate plan step requires candidate_id")
            _require(self.candidate_id in set(candidate_ids), "plan step references unknown candidate")
            _validate_identifier(self.candidate_id, "CAND")
            _require(self.technique_id is not None, "candidate plan step requires technique_id")
            _require(self.knob is not None, "candidate plan step requires knob")
        else:
            _require(self.action is not None, "action plan step requires action")


@dataclass(frozen=True)
class ScenarioEconomics:
    """Stateful, ordered, bundled counterfactual of one scenario, authoritative for that scenario; never additive across scenarios.

    contingent_* is the separate upside of steps awaiting re-evaluation and never feeds package economics. An UNAVAILABLE estimate
    (including contingent_*) has null ranges.
    """
    estimation_status: EstimationStatus
    baseline_365d: CostBreakdown
    estimated_cost_365d: ComponentRanges
    savings_365d: ComponentRanges
    method: str
    is_additive: bool = False
    # Contingent upside of steps awaiting re-evaluation; never feeds package economics. Null when no step is
    # pending or when it cannot be priced (unknown cost is null, never zero).
    contingent_estimated_cost_365d: Optional[ComponentRanges] = None
    contingent_savings_365d: Optional[ComponentRanges] = None

    def validate(self) -> None:
        """Reject is_additive true, a blank method, invalid ranges, numbers on an UNAVAILABLE estimate, and contingent cost and savings that are not both
        present or both null.
        """
        _require(self.is_additive is False, "scenario economics are stateful and non-additive")
        _require(bool(self.method), "scenario economics method is required")
        self.baseline_365d.validate()
        self.estimated_cost_365d.validate(nonnegative=True, field_name="scenario.estimated_cost_365d")
        self.savings_365d.validate(nonnegative=False, field_name="scenario.savings_365d")
        _require_unavailable_means_null(
            self.estimation_status, self.estimated_cost_365d, self.savings_365d,
            self.contingent_estimated_cost_365d, self.contingent_savings_365d, field_name="scenario economics",
        )
        _require(
            (self.contingent_estimated_cost_365d is None) == (self.contingent_savings_365d is None),
            "contingent cost and savings must be published together or both null",
        )
        if self.contingent_savings_365d is not None:
            self.contingent_estimated_cost_365d.validate(nonnegative=True, field_name="scenario.contingent_estimated_cost_365d")
            self.contingent_savings_365d.validate(nonnegative=False, field_name="scenario.contingent_savings_365d")


@dataclass(frozen=True)
class Scenario:
    """One mutually exclusive execution path with ordered steps and its own stateful economics.

    candidate_refs keep execution order (hashed in order). Exactly one scenario is RECOMMENDED when the plan names one.
    """
    scenario_id: str
    scenario_name: str
    scenario_type: ScenarioType
    status: ScenarioStatus
    candidate_refs: List[str]
    evidence_refs: List[str]
    confidence: Confidence
    compatibility: Compatibility
    portfolio_economics: ScenarioEconomics
    execution_plan: List[PlanStep]

    def validate(self, *, candidate_ids: Iterable[str], evidence_ids: Iterable[str]) -> None:
        """Reject a malformed SCN- id, a blank name, unknown or duplicate candidate_refs, unknown evidence_refs, invalid confidence, economics or steps, steps that are not
        contiguous from 1, and headline or contingent economics that disagree with the step projections.
        """
        _validate_identifier(self.scenario_id, "SCN")
        _require(bool(self.scenario_name), "scenario_name is required")
        candidate_set = set(candidate_ids)
        evidence_set = set(evidence_ids)
        _require(all(ref in candidate_set for ref in self.candidate_refs), "scenario references unknown candidate")
        _require(all(ref in evidence_set for ref in self.evidence_refs), "scenario references unknown evidence")
        _require(len(self.candidate_refs) == len(set(self.candidate_refs)), "scenario candidate_refs must be unique")
        self.confidence.validate()
        self.portfolio_economics.validate()
        ordered_steps = sorted(self.execution_plan, key=lambda x: x.step)
        _require([x.step for x in ordered_steps] == list(range(1, len(ordered_steps) + 1)), "scenario steps must be contiguous starting at 1")
        for step in ordered_steps:
            step.validate(candidate_ids=candidate_set)
        self._validate_projection_consistency(ordered_steps)

    def _validate_projection_consistency(self, ordered_steps: List["PlanStep"]) -> None:
        """Scenario headline economics are authoritative-only; contingent upside is separate (contract 1.1.0)."""
        econ = self.portfolio_economics

        def last_projection(basis: PricingBasis) -> Optional[ComponentRanges]:
            matching = [x.projected_economics for x in ordered_steps
                        if x.projected_economics is not None and x.projected_economics.pricing_basis == basis]
            return matching[-1].running_savings_365d if matching else None

        def same(a: ComponentRanges, b: ComponentRanges, name: str) -> None:
            for component in ("dbu", "aws", "total"):
                ra, rb = getattr(a, component), getattr(b, component)
                if ra is None or rb is None:
                    continue
                for bound in ("low", "base", "high"):
                    _require(abs(getattr(ra, bound) - getattr(rb, bound)) <= 0.03, f"{name}: {component}.{bound} mismatch")

        if econ.contingent_savings_365d is not None:
            _require(
                any(x.execution_state == ExecutionState.PENDING_REEVALUATION for x in ordered_steps),
                "contingent economics require at least one PENDING_REEVALUATION step",
            )
            last_contingent = last_projection(PricingBasis.CONTINGENT_PENDING_REEVALUATION)
            if last_contingent is not None:
                same(econ.contingent_savings_365d, last_contingent, "contingent savings must equal the last pending step's running savings")
        if econ.estimation_status != EstimationStatus.UNAVAILABLE:
            last_authoritative = last_projection(PricingBasis.AUTHORITATIVE)
            if last_authoritative is not None:
                same(econ.savings_365d, last_authoritative, "scenario savings must equal the last authoritative step's running savings")
        # contract 1.3.0: the first authoritative step starts from the no-change baseline, so its own change equals its
        # running savings; step lows/highs are per-step track minima/maxima, so their sums bracket the final running range.
        auth_steps = [x.projected_economics for x in ordered_steps
                      if x.projected_economics is not None and x.projected_economics.pricing_basis == PricingBasis.AUTHORITATIVE
                      and x.projected_economics.step_savings_365d is not None]
        if auth_steps:
            same(auth_steps[0].step_savings_365d, auth_steps[0].running_savings_365d, "first authoritative step savings must equal its running savings")
            tol = 0.03 * len(auth_steps)
            last = auth_steps[-1].running_savings_365d
            for component in ("dbu", "aws", "total"):
                final = getattr(last, component)
                parts = [getattr(x.step_savings_365d, component) for x in auth_steps]
                if final is None or any(p is None for p in parts):
                    continue
                _require(sum(p.low for p in parts) <= final.low + tol, f"step savings {component}.low must not exceed the final running low")
                _require(sum(p.high for p in parts) >= final.high - tol, f"step savings {component}.high must not fall below the final running high")


@dataclass(frozen=True)
class ReevaluationWindow:
    """Time-clearable gates that currently hide the next step (contract 1.2.0). Null on the plan when nothing waits."""
    status: ReevaluationStatus
    earliest_reevaluation_time: str
    blocking_codes: List[str]
    method: str

    def validate(self) -> None:
        """Reject a non-ISO-8601 time, empty, unknown, unsorted or duplicated blocking_codes, and a blank method."""
        _require(_is_iso8601(self.earliest_reevaluation_time), "reevaluation.earliest_reevaluation_time must be ISO-8601")
        _require(len(self.blocking_codes) >= 1, "reevaluation.blocking_codes must not be empty")
        _require(all(code in REEVALUATION_BLOCKING_CODES for code in self.blocking_codes), "reevaluation.blocking_codes contains an unknown gate")
        _require(list(self.blocking_codes) == sorted(set(self.blocking_codes)), "reevaluation.blocking_codes must be sorted and unique")
        _require(bool(self.method), "reevaluation.method is required")


@dataclass(frozen=True)
class ConfigChange:
    """One knob's previous and current value across the epoch boundary."""
    knob: TunableKnob
    previous_value: Any
    current_value: Any


@dataclass(frozen=True)
class ObservedConfigChange:
    """The configuration change that opened the current epoch (authoritative diff of the previous and current snapshots).

    Carries no savings and is not realization; entries are sorted and unique by knob.
    """
    previous_epoch_id: str
    previous_epoch_start: str
    changed_at: str
    changes: List[ConfigChange]
    measurement_status: MeasurementStatus

    def validate(self) -> None:
        """Reject a malformed previous epoch id, non-ISO-8601 dates, empty changes, knobs that are not sorted and unique, or entries whose values do not differ."""
        _validate_identifier(self.previous_epoch_id, "EPOCH")
        _require(_is_iso8601(self.previous_epoch_start), "observed_change.previous_epoch_start must be ISO-8601")
        _require(_is_iso8601(self.changed_at), "observed_change.changed_at must be ISO-8601")
        _require(len(self.changes) >= 1, "observed_change.changes must not be empty")
        knobs = [c.knob.value for c in self.changes]
        _require(knobs == sorted(set(knobs)), "observed_change.changes must be sorted and unique by knob")
        for change in self.changes:
            _require(change.previous_value != change.current_value, "observed_change entries must differ")

    def fingerprint(self) -> str:
        """SHA256 fingerprint of the whole block; it enters the package hash so a different observed change gives a different package id."""
        return f"SHA256:{sha256_hex(to_primitive(self)).upper()}"


@dataclass(frozen=True)
class CarriedForwardStep:
    """One step of the preceding epoch's plan replayed against the observed change (reserved, Stage 2). Rebased savings only on PENDING_REEVALUATION steps."""
    order: int
    knob: TunableKnob
    technique_id: str
    previous_value: Any
    recommended_value: Any
    disposition: CarriedForwardDisposition
    reason_code: Optional[str]
    rebased_running_savings_365d: Optional["ComponentRanges"]

    def validate(self) -> None:
        """Reject order below 1, a blank technique_id, a reason_code outside ^[A-Z][A-Z0-9_]*$, an INVALIDATED step without reason_code, and rebased savings on a step
        that is not PENDING_REEVALUATION.
        """
        _require(self.order >= 1, "carried-forward step order must be >= 1")
        _require(bool(self.technique_id), "carried-forward step technique_id is required")
        if self.reason_code is not None:
            _require(bool(_CODE_PATTERN.match(self.reason_code)), "carried-forward step reason_code must match ^[A-Z][A-Z0-9_]*$")
        if self.disposition == CarriedForwardDisposition.INVALIDATED:
            _require(bool(self.reason_code), "INVALIDATED carried-forward step requires reason_code")
        if self.rebased_running_savings_365d is not None:
            _require(self.disposition == CarriedForwardDisposition.PENDING_REEVALUATION, "only PENDING_REEVALUATION carried-forward steps carry savings")
            self.rebased_running_savings_365d.validate(nonnegative=False, field_name="carried_forward.step.rebased_running_savings_365d")


@dataclass(frozen=True)
class CarriedForwardPlan:
    """Contingent replay of the preceding epoch's plan (reserved; null until Stage 2 ships). Never feeds package economics."""
    source_epoch_id: str
    as_of: str
    match_policy: MatchPolicy
    match_outcome: MatchOutcome
    match_reason_code: Optional[str]
    replay_fingerprint: str
    steps: List[CarriedForwardStep]
    remaining_upside_365d: Optional["ComponentRanges"]
    pricing_basis: PricingBasis

    def fingerprint(self) -> str:
        """Hash of the whole published block, so any change to steps, dates or reasons changes the package identity."""
        return f"SHA256:{sha256_hex(to_primitive(self)).upper()}"

    def validate(self) -> None:
        """Reject a malformed source epoch id, a non-ISO-8601 as_of, a replay_fingerprint outside SHA256:<64 uppercase hex>, a bad reason code, a pricing basis other than
        CONTINGENT_CARRIED_FORWARD, non-contiguous steps, and outcome-inconsistent content (NOT_REPLAYABLE needs no steps or upside, MISMATCH needs every step
        INVALIDATED and no upside, MATCHED needs a COMPLETED first step; a reason code is required unless MATCHED), and a remaining upside that is unpriced or
        differs from the last pending step's rebased savings.
        """
        _validate_identifier(self.source_epoch_id, "EPOCH")
        _require(_is_iso8601(self.as_of), "carried_forward.as_of must be ISO-8601")
        _require(bool(_FINGERPRINT_PATTERN.match(self.replay_fingerprint)), "replay_fingerprint must use SHA256:<64 uppercase hex>")
        if self.match_reason_code is not None:
            _require(bool(_CODE_PATTERN.match(self.match_reason_code)), "carried_forward match_reason_code must match ^[A-Z][A-Z0-9_]*$")
        _require(self.pricing_basis == PricingBasis.CONTINGENT_CARRIED_FORWARD, "carried-forward economics must be CONTINGENT_CARRIED_FORWARD")
        _require([s.order for s in self.steps] == list(range(1, len(self.steps) + 1)), "carried-forward steps must be contiguous starting at 1")
        for step in self.steps:
            step.validate()
        if self.match_outcome != MatchOutcome.MATCHED:
            _require(bool(self.match_reason_code), "carried-forward match_reason_code is required unless MATCHED")
        if self.match_outcome == MatchOutcome.NOT_REPLAYABLE:
            _require(len(self.steps) == 0 and self.remaining_upside_365d is None, "NOT_REPLAYABLE requires no steps and null remaining upside")
        if self.match_outcome == MatchOutcome.MISMATCH:
            _require(all(s.disposition == CarriedForwardDisposition.INVALIDATED for s in self.steps), "MISMATCH requires every step INVALIDATED")
            _require(self.remaining_upside_365d is None, "MISMATCH requires null remaining upside")
        if self.match_outcome == MatchOutcome.MATCHED:
            _require(len(self.steps) >= 1 and self.steps[0].disposition == CarriedForwardDisposition.COMPLETED, "MATCHED requires the first step COMPLETED")
        pending = [s for s in self.steps if s.disposition == CarriedForwardDisposition.PENDING_REEVALUATION and s.rebased_running_savings_365d is not None]
        if self.remaining_upside_365d is not None:
            self.remaining_upside_365d.validate(nonnegative=False, field_name="carried_forward.remaining_upside_365d")
            _require(bool(pending), "remaining upside requires a priced pending step")
            last = pending[-1].rebased_running_savings_365d
            for component in ("dbu", "aws", "total"):
                a, b = getattr(self.remaining_upside_365d, component), getattr(last, component)
                if a is None or b is None:
                    continue
                for bound in ("low", "base", "high"):
                    _require(abs(getattr(a, bound) - getattr(b, bound)) <= 0.03, "remaining upside must equal the last pending step's rebased savings")


@dataclass(frozen=True)
class RecommendedPlan:
    """The selection: which scenario is recommended (None when none), why, the next action, whether a human must approve, and the optional re-evaluation window.

    selection_reason_code and next_action are stable codes; decision_summary is prose.
    """
    scenario_id: Optional[str]
    selection_reason_code: str
    next_action: str
    requires_hitl: bool
    decision_summary: str
    reevaluation: Optional[ReevaluationWindow] = None

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        """Reject an unknown or malformed scenario_id, blank selection_reason_code, next_action or decision_summary, and an invalid reevaluation window."""
        if self.scenario_id is not None:
            _validate_identifier(self.scenario_id, "SCN")
            _require(self.scenario_id in set(scenario_ids), "recommended_plan references unknown scenario")
        _require(bool(self.selection_reason_code), "selection_reason_code is required")
        _require(bool(self.next_action), "next_action is required")
        _require(bool(self.decision_summary), "decision_summary is required")
        if self.reevaluation is not None:
            self.reevaluation.validate()


# ---------------------------------------------------------------------------
# Package-level economics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EconomicsPoint:
    """One exact scenario/bound economics point.  Savings may be negative; costs may not."""
    dbu: Optional[float]
    aws: Optional[float]
    total: Optional[float]

    def validate(self, *, nonnegative: bool, field_name: str) -> None:
        """Reject negative components when ``nonnegative`` (costs) and total != dbu + aws (tolerance 0.03); savings points may be negative."""
        if nonnegative:
            _validate_nonnegative(self.dbu, f"{field_name}.dbu")
            _validate_nonnegative(self.aws, f"{field_name}.aws")
            _validate_nonnegative(self.total, f"{field_name}.total")
        if self.dbu is not None and self.aws is not None and self.total is not None:
            _require(abs((self.dbu + self.aws) - self.total) <= 0.03, f"{field_name}.total must equal DBU + AWS")


@dataclass(frozen=True)
class ScenarioEnvelopePoint:
    """One bound (LOW, BASE or HIGH) of one scenario as a package envelope point, carrying that scenario's DBU/AWS/total savings and estimated cost."""
    scenario_id: str
    bound: BoundName
    savings_365d: EconomicsPoint
    estimated_cost_365d: EconomicsPoint

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        """Reject a malformed or unknown scenario_id or invalid savings (signed) or cost (non-negative) points."""
        _validate_identifier(self.scenario_id, "SCN")
        _require(self.scenario_id in set(scenario_ids), "scenario envelope references unknown scenario")
        self.savings_365d.validate(nonnegative=False, field_name="scenario_envelope.savings_365d")
        self.estimated_cost_365d.validate(nonnegative=True, field_name="scenario_envelope.estimated_cost_365d")


@dataclass(frozen=True)
class ExcludedScenario:
    """A scenario left out of package economics, with the stable reason code (for example SCENARIO_ECONOMICS_UNAVAILABLE)."""
    scenario_id: str
    reason_code: str

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        """Reject a malformed or unknown scenario_id or a blank reason_code."""
        _validate_identifier(self.scenario_id, "SCN")
        _require(self.scenario_id in set(scenario_ids), "excluded scenario references unknown scenario")
        _require(bool(self.reason_code), "excluded scenario reason_code is required")


@dataclass(frozen=True)
class ScenarioEnvelope:
    """The three source points of the package opportunity envelope: minimum (LOW bound), recommended base (BASE bound) and maximum (HIGH bound); each None when no scenario is assessable."""
    minimum_savings: Optional[ScenarioEnvelopePoint]
    recommended_base: Optional[ScenarioEnvelopePoint]
    maximum_savings: Optional[ScenarioEnvelopePoint]

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        """Reject invalid points, minimum total savings above maximum, and points that reference the wrong bound (LOW, BASE, HIGH respectively)."""
        for item in (self.minimum_savings, self.recommended_base, self.maximum_savings):
            if item is not None:
                item.validate(scenario_ids=scenario_ids)
        if self.minimum_savings and self.maximum_savings:
            if self.minimum_savings.savings_365d.total is not None and self.maximum_savings.savings_365d.total is not None:
                _require(self.minimum_savings.savings_365d.total <= self.maximum_savings.savings_365d.total, "minimum savings must be <= maximum savings")
        if self.minimum_savings:
            _require(self.minimum_savings.bound == BoundName.LOW, "minimum_savings must reference LOW bound")
        if self.recommended_base:
            _require(self.recommended_base.bound == BoundName.BASE, "recommended_base must reference BASE bound")
        if self.maximum_savings:
            _require(self.maximum_savings.bound == BoundName.HIGH, "maximum_savings must reference HIGH bound")


@dataclass(frozen=True)
class PackageEconomicsSemantics:
    """Fixed declaration of what package economics are: a SCENARIO_ENVELOPE, non-additive, not a probability interval."""
    method: str
    is_additive: bool
    is_probability_interval: bool

    def validate(self) -> None:
        """Reject any method other than SCENARIO_ENVELOPE, is_additive true, or is_probability_interval true."""
        _require(self.method == "SCENARIO_ENVELOPE", "package economics method must be SCENARIO_ENVELOPE")
        _require(self.is_additive is False, "package economics must be non-additive")
        _require(self.is_probability_interval is False, "package range is not a probability interval")


@dataclass(frozen=True)
class PackageEconomics:
    """Opportunity envelope across the economically assessable scenarios.

    low is the minimum LOW total savings, base the recommended scenario's BASE total savings, high the maximum HIGH total savings, so the three may
    come from different scenarios. Not a probability interval, not additive. estimated_cost_365d is baseline total minus these savings (bounds swapped).
    Both ranges are None when no scenario is assessable; DBU/AWS parts live on the envelope points. Every scenario is included or excluded.
    """
    currency: str
    baseline_365d: CostBreakdown
    estimated_savings_365d: Optional[Range3]
    estimated_cost_365d: Optional[Range3]
    scenario_envelope: ScenarioEnvelope
    included_scenario_ids: List[str]
    excluded_scenarios: List[ExcludedScenario]
    semantics: PackageEconomicsSemantics

    def validate(self, *, scenario_ids: Iterable[str], recommended_scenario_id: Optional[str]) -> None:
        """Reject a currency that is not a 3-letter uppercase code, invalid baseline or ranges, duplicate or unknown included/excluded scenarios, a scenario both
        included and excluded, an invalid envelope or semantics, and an envelope base that is not the recommended scenario.
        """
        scenario_set = set(scenario_ids)
        _require(len(self.currency) == 3 and self.currency.isupper(), "package economics currency must be uppercase 3-letter code")
        self.baseline_365d.validate()
        if self.estimated_savings_365d is not None:
            self.estimated_savings_365d.validate(nonnegative=False, field_name="package.estimated_savings_365d")
        if self.estimated_cost_365d is not None:
            self.estimated_cost_365d.validate(nonnegative=True, field_name="package.estimated_cost_365d")
        _require(len(self.included_scenario_ids) == len(set(self.included_scenario_ids)), "included_scenario_ids must be unique")
        _require(all(x in scenario_set for x in self.included_scenario_ids), "package economics includes unknown scenario")
        for item in self.excluded_scenarios:
            item.validate(scenario_ids=scenario_set)
        excluded_ids = {x.scenario_id for x in self.excluded_scenarios}
        _require(not (set(self.included_scenario_ids) & excluded_ids), "scenario cannot be both included and excluded")
        self.scenario_envelope.validate(scenario_ids=scenario_set)
        self.semantics.validate()
        if self.scenario_envelope.recommended_base is not None and recommended_scenario_id is not None:
            _require(self.scenario_envelope.recommended_base.scenario_id == recommended_scenario_id, "package base must come from recommended scenario")


# ---------------------------------------------------------------------------
# Realization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedChange:
    """Record that a change was applied (when, by whom, previous and applied config). Realization is outside this read-only publisher's scope today."""
    applied_at: str
    applied_by: str
    previous_config: Dict[str, Any]
    applied_config: Dict[str, Any]

    def validate(self) -> None:
        """Reject a non-ISO-8601 applied_at or a blank applied_by."""
        _require(_is_iso8601(self.applied_at), "applied_at must be ISO-8601")
        _require(bool(self.applied_by), "applied_by is required")


@dataclass(frozen=True)
class RealizationValidation:
    """Post-change validation window, outcome, guardrail result (None = not evaluated) and the evidence ids it relied on."""
    validation_window_start: str
    validation_window_end: str
    validation_status: ValidationOutcome
    performance_guardrails_passed: Optional[bool]
    finding_refs: List[str]
    notes: List[str]

    def validate(self, *, evidence_ids: Iterable[str]) -> None:
        """Reject non-ISO-8601 window bounds, a start after the end, or finding_refs missing from the evidence registry."""
        _require(_is_iso8601(self.validation_window_start), "validation_window_start must be ISO-8601")
        _require(_is_iso8601(self.validation_window_end), "validation_window_end must be ISO-8601")
        _require(self.validation_window_start <= self.validation_window_end, "validation window start must be <= end")
        evidence_set = set(evidence_ids)
        _require(all(ref in evidence_set for ref in self.finding_refs), "realization references unknown evidence")


@dataclass(frozen=True)
class RealizationObservedCost:
    """Observed post-change cost in one currency; components are None when unknown."""
    currency: str
    dbu_cost: Optional[float]
    aws_cost: Optional[float]
    total_cost: Optional[float]

    def validate(self) -> None:
        """Reject negative components or total != dbu + aws (tolerance 0.02)."""
        _validate_nonnegative(self.dbu_cost, "realization observed dbu_cost")
        _validate_nonnegative(self.aws_cost, "realization observed aws_cost")
        _validate_nonnegative(self.total_cost, "realization observed total_cost")
        if self.dbu_cost is not None and self.aws_cost is not None and self.total_cost is not None:
            _require(abs((self.dbu_cost + self.aws_cost) - self.total_cost) <= 0.02, "realization total_cost must equal DBU + AWS")


@dataclass(frozen=True)
class RealizedSavings:
    """Measured savings (signed, positive = savings) per component and annualized; None when unknown."""
    currency: str
    dbu_savings: Optional[float]
    aws_savings: Optional[float]
    total_savings: Optional[float]
    annualized_total_savings: Optional[float]

    def validate(self) -> None:
        """Reject total_savings != dbu_savings + aws_savings (tolerance 0.02) when all three are present."""
        if self.dbu_savings is not None and self.aws_savings is not None and self.total_savings is not None:
            _require(abs((self.dbu_savings + self.aws_savings) - self.total_savings) <= 0.02, "realized total_savings must equal DBU + AWS")


@dataclass(frozen=True)
class RealizationOutcome:
    """Final outcome of a realized change: status, recommendation accuracy in [0, 1] (None = not measured), rollback flag and learning codes."""
    status: RealizationOutcomeStatus
    recommendation_accuracy: Optional[float]
    rollback_required: bool
    learning_codes: List[str]

    def validate(self) -> None:
        """Reject recommendation_accuracy outside [0, 1]."""
        if self.recommendation_accuracy is not None:
            _require(0 <= self.recommendation_accuracy <= 1, "recommendation_accuracy must be in [0,1]")


@dataclass(frozen=True)
class RealizationRecord:
    """One measurement episode of an applied change; its REAL- id is derived from the package, scenario, candidate, window and applied config."""
    realization_id: str
    recommendation_package_id: str
    scenario_id: str
    candidate_id: Optional[str]
    technique_id: Optional[str]
    applied: AppliedChange
    validation: RealizationValidation
    observed_cost: RealizationObservedCost
    realized_savings: RealizedSavings
    outcome: RealizationOutcome

    def validate(self, *, package_id: str, scenario_ids: Iterable[str], candidate_ids: Iterable[str], evidence_ids: Iterable[str]) -> None:
        """Reject a malformed id, a package id mismatch, unknown scenario or candidate, invalid parts, and a realization_id that is not the recomputed make_realization_id."""
        _validate_identifier(self.realization_id, "REAL")
        _require(self.recommendation_package_id == package_id, "realization package id mismatch")
        _require(self.scenario_id in set(scenario_ids), "realization references unknown scenario")
        if self.candidate_id is not None:
            _require(self.candidate_id in set(candidate_ids), "realization references unknown candidate")
        self.applied.validate()
        self.validation.validate(evidence_ids=evidence_ids)
        self.observed_cost.validate()
        self.realized_savings.validate()
        self.outcome.validate()
        expected = make_realization_id(
            recommendation_package_id=self.recommendation_package_id,
            scenario_id=self.scenario_id,
            candidate_id=self.candidate_id,
            validation_window_start=self.validation.validation_window_start,
            validation_window_end=self.validation.validation_window_end,
            applied_config=self.applied.applied_config,
        )
        _require(self.realization_id == expected, f"realization id mismatch: expected {expected}")


@dataclass(frozen=True)
class RealizationAggregate:
    """Roll-up of measured/successful/rolled-back candidate counts and realized savings in the package currency."""
    measured_candidates: int
    successful_candidates: int
    rolled_back_candidates: int
    currency: str
    realized_savings_to_date: float
    annualized_realized_savings: float

    def validate(self) -> None:
        """Reject negative counts or successful/rolled-back counts above measured_candidates."""
        _require(self.measured_candidates >= 0, "measured_candidates must be >= 0")
        _require(0 <= self.successful_candidates <= self.measured_candidates, "successful_candidates out of range")
        _require(0 <= self.rolled_back_candidates <= self.measured_candidates, "rolled_back_candidates out of range")


@dataclass(frozen=True)
class Realization:
    """Realization state of the package. Packages from this publisher are NOT_APPLIED with no records."""
    status: RealizationStatus
    realization_records: List[RealizationRecord]
    aggregate: RealizationAggregate

    def validate(self, *, package_id: str, scenario_ids: Iterable[str], candidate_ids: Iterable[str], evidence_ids: Iterable[str]) -> None:
        """Reject records on a NOT_APPLIED realization, invalid records, or an invalid aggregate."""
        if self.status == RealizationStatus.NOT_APPLIED:
            _require(len(self.realization_records) == 0, "NOT_APPLIED realization must have no records")
        for record in self.realization_records:
            record.validate(package_id=package_id, scenario_ids=scenario_ids, candidate_ids=candidate_ids, evidence_ids=evidence_ids)
        self.aggregate.validate()


# ---------------------------------------------------------------------------
# Cost profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostProfile:
    """The cost profile one warehouse was evaluated under (contract 3.0.0).

    ``applied_thresholds`` holds the effective value of every profiled threshold, grouped by technique
    (COST_PROFILE_GROUPS). ``name`` and ``applied_thresholds`` enter every candidate, scenario and package id;
    ``source`` and ``source_detail`` say how the profile was chosen and are presentation only (the same effective
    profile gives the same ids however it was chosen). ``source_detail`` is None for DEFAULT, ``tag <key>=<value>``
    (the allow-listed nonprod tag that matched) for TAG_DEFAULT, ``--cost-profile <name>`` for CLI_ALL and
    ``--cost-profile <name>=<warehouse>`` for CLI_WAREHOUSE.
    """
    name: CostProfileName
    source: CostProfileSource
    source_detail: Optional[str]
    applied_thresholds: Dict[str, Dict[str, float]]

    def hash_payload(self) -> Dict[str, Any]:
        """The identity part of the profile: name and applied thresholds (never source or source_detail)."""
        return {"name": CostProfileName(self.name).value, "applied_thresholds": self.applied_thresholds}

    def validate(self) -> None:
        """Reject an unknown name or source, applied thresholds whose groups and fields differ from COST_PROFILE_GROUPS
        or that are not finite numbers, and a source_detail that does not match its source."""
        name, source = CostProfileName(self.name), CostProfileSource(self.source)
        _require({k: sorted(v) for k, v in self.applied_thresholds.items()} == {k: sorted(v) for k, v in COST_PROFILE_GROUPS.items()},
                 "cost_profile.applied_thresholds must hold exactly the COST_PROFILE_GROUPS fields")
        for group in self.applied_thresholds.values():
            for key, value in group.items():
                _require(isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf"),
                         f"cost_profile.applied_thresholds.{key} must be a finite number")
        detail = self.source_detail
        if source == CostProfileSource.DEFAULT:
            _require(detail is None, "cost_profile.source_detail must be null for DEFAULT")
            _require(name == CostProfileName.BALANCED, "a DEFAULT cost profile is balanced")
        elif source == CostProfileSource.TAG_DEFAULT:
            _require(detail is not None and re.fullmatch(r"tag [a-z0-9_-]+=[a-z0-9_-]+", detail) is not None,
                     "TAG_DEFAULT source_detail must be 'tag <key>=<value>'")
            _require(name == CostProfileName.AGGRESSIVE, "a TAG_DEFAULT cost profile is aggressive")
        elif source == CostProfileSource.CLI_ALL:
            _require(detail == f"--cost-profile {name.value}", "CLI_ALL source_detail must be '--cost-profile <name>'")
        else:
            prefix = f"--cost-profile {name.value}="
            _require(detail is not None and detail.startswith(prefix) and _WAREHOUSE_REF_PATTERN.match(detail[len(prefix):]) is not None,
                     "CLI_WAREHOUSE source_detail must be '--cost-profile <name>=<warehouse>'")


# ---------------------------------------------------------------------------
# Provenance and top-level package
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Versions, evidence fingerprint and sources a package was built from.

    The component versions and evidence_fingerprint enter the package hash; generated_at is volatile presentation metadata and never does.
    package_hash_full_sha256 is the full 64-hex hash behind the truncated package id (None until set).
    """
    contract_version: str
    policy_version: str
    analyzer_version: str
    optimizer_version: str
    modeler_version: str
    estimator_version: str
    bundle_estimator_version: str
    reviewer_version: str
    decision_graph_version: str
    technique_catalog_version: str
    evidence_fingerprint: str
    source_tables: List[str]
    source_regions: List[str]
    generated_at: str
    package_hash_full_sha256: Optional[str] = None

    def validate(self) -> None:
        """Reject a contract_version other than CONTRACT_VERSION, a blank component version, an evidence_fingerprint that is not a full SHA256:<64 hex>,
        empty source_tables or source_regions, a non-ISO-8601 generated_at, and a package_hash_full_sha256 that is not 64 characters.
        """
        _require(self.contract_version == CONTRACT_VERSION, f"provenance.contract_version must be {CONTRACT_VERSION}")
        for name in (
            "policy_version", "analyzer_version", "optimizer_version", "modeler_version",
            "estimator_version", "bundle_estimator_version", "reviewer_version",
            "decision_graph_version", "technique_catalog_version",
        ):
            _require(bool(getattr(self, name)), f"provenance.{name} is required")
        _require(self.evidence_fingerprint.startswith("SHA256:"), "evidence_fingerprint must use SHA256:<hex>")
        _require(len(self.evidence_fingerprint.split(":", 1)[1]) == 64, "evidence_fingerprint must contain full SHA-256")
        _require(len(self.source_tables) >= 1, "source_tables must not be empty")
        _require(len(self.source_regions) >= 1, "source_regions must not be empty")
        _require(_is_iso8601(self.generated_at), "generated_at must be ISO-8601")
        if self.package_hash_full_sha256 is not None:
            _require(len(self.package_hash_full_sha256) == 64, "package_hash_full_sha256 must be 64 hex characters")


# Knobs each scoped scenario type may name (contract 3.1.0); the no-change baseline is never a scoped scenario.
SCOPE_SCENARIO_KNOBS = {
    ScenarioType.IN_PLACE_OPTIMIZATION: (
        TunableKnob.AUTO_STOP_MINUTES, TunableKnob.MAX_CLUSTERS, TunableKnob.MIN_CLUSTERS, TunableKnob.WAREHOUSE_SIZE,
    ),
    ScenarioType.WAREHOUSE_TYPE_MIGRATION: (TunableKnob.WAREHOUSE_TYPE,),
}


@dataclass(frozen=True)
class ScopedScenario:
    """One scenario a restricted run assessed and the knobs (sorted by value) it was allowed to recommend."""
    scenario_type: ScenarioType
    knobs: List[TunableKnob]

    def validate(self) -> None:
        """Reject the no-change baseline, an empty or unsorted knob list, duplicates and knobs of another scenario type."""
        allowed = SCOPE_SCENARIO_KNOBS.get(self.scenario_type)
        _require(allowed is not None, f"run_scope scenario type {self.scenario_type.value} cannot be scoped")
        values = [k.value for k in self.knobs]
        _require(len(values) >= 1, "run_scope scenario needs at least one knob")
        _require(values == sorted(set(values)), "run_scope knobs must be unique and sorted")
        _require(all(k in allowed for k in self.knobs), f"run_scope knobs do not belong to {self.scenario_type.value}")


@dataclass(frozen=True)
class SafetyInclusion:
    """A candidate kept outside the run scope because it is a safety action (a fail-fast canary rollback)."""
    candidate_id: str
    knob: TunableKnob
    reason_code: str

    def validate(self) -> None:
        """Reject a malformed candidate ID and any reason other than CANARY_GUARDRAIL_BREACHED."""
        _validate_identifier(self.candidate_id, "CAND")
        _require(self.reason_code == "CANARY_GUARDRAIL_BREACHED", "safety inclusion reason must be CANARY_GUARDRAIL_BREACHED")


@dataclass(frozen=True)
class RunScope:
    """The restricted scope a package was produced under (contract 3.1.0); the package's ``run_scope`` is None for the full scope.

    ``scenarios`` are sorted by type; ``safety_inclusions`` are sorted by knob. A scope that names every knob of
    every scoped scenario type is the full scope and must be null instead.
    """
    scenarios: List[ScopedScenario]
    safety_inclusions: List[SafetyInclusion] = field(default_factory=list)

    def knobs(self) -> List[TunableKnob]:
        """Every in-scope knob."""
        return [k for s in self.scenarios for k in s.knobs]

    def scenario_types(self) -> List[ScenarioType]:
        """The scoped scenario types, in order."""
        return [s.scenario_type for s in self.scenarios]

    def validate(self) -> None:
        """Reject an empty, unsorted or duplicated scenario list, a full scope, and unsorted or duplicated safety inclusions."""
        _require(len(self.scenarios) >= 1, "run_scope needs at least one scenario")
        types = [s.scenario_type.value for s in self.scenarios]
        _require(types == sorted(set(types)), "run_scope scenarios must be unique and sorted by type")
        for s in self.scenarios:
            s.validate()
        full = {k for knobs in SCOPE_SCENARIO_KNOBS.values() for k in knobs}
        _require(set(self.knobs()) != full, "a full scope is represented by run_scope null")
        for x in self.safety_inclusions:
            x.validate()
            _require(x.knob not in self.knobs(), "a safety inclusion must be outside the run scope")
        knobs = [x.knob.value for x in self.safety_inclusions]
        _require(knobs == sorted(set(knobs)), "safety inclusions must be unique and sorted by knob")


@dataclass(frozen=True)
class RecommendationPackage:
    """Public machine source of truth: one immutable, content-addressed package per warehouse and current configuration epoch.

    Candidate economics are isolated and non-additive, scenario economics are stateful, package economics is a scenario envelope. The PKG- id hashes
    package_hash_payload (never generated_at or prose). observed_change and carried_forward_plan are None when absent; run_scope is None for the
    full scope (every candidate domain and scenario assessed).
    """
    recommendation_package_id: str
    contract_version: str
    warehouse: WarehouseIdentity
    current_config: CurrentConfig
    workload_profile: WorkloadProfile
    cost_baseline: CostBaseline
    assessment: Assessment
    evidence: Dict[str, EvidenceRecord]
    candidates: List[CandidateRecommendation]
    scenarios: List[Scenario]
    recommended_plan: RecommendedPlan
    package_economics: PackageEconomics
    realization: Realization
    provenance: Provenance
    cost_profile: CostProfile
    observed_change: Optional[ObservedConfigChange] = None
    carried_forward_plan: Optional[CarriedForwardPlan] = None
    run_scope: Optional[RunScope] = None

    def validate(self, *, verify_content_id: bool = True) -> None:
        """Semantic validation of the whole package (raises ValueError).

        Recomputes the epoch, evidence, candidate and scenario ids and the evidence fingerprint; checks references, the single RECOMMENDED scenario,
        the re-evaluation window and observed/carried-forward blocks, that package economics equal the envelope of the included scenarios, that
        assessment counts match the candidates, currency consistency, and the full hash. ``verify_content_id=False`` skips only the final PKG- id
        comparison.
        """
        _validate_identifier(self.recommendation_package_id, "PKG")
        _require(self.contract_version == CONTRACT_VERSION, f"contract_version must be {CONTRACT_VERSION}")
        self.warehouse.validate()
        self.current_config.validate()
        self.workload_profile.validate()
        self.cost_baseline.validate()
        self.assessment.validate()
        self.provenance.validate()
        self.cost_profile.validate()
        profile = self.cost_profile.hash_payload()

        expected_epoch = make_epoch_id(
            workspace_id=self.warehouse.workspace_id,
            warehouse_id=self.warehouse.warehouse_id,
            start_time=self.warehouse.current_epoch.start_time,
            warehouse_type=self.current_config.warehouse_type,
            warehouse_size=self.current_config.warehouse_size,
            min_clusters=self.current_config.min_clusters,
            max_clusters=self.current_config.max_clusters,
            auto_stop_minutes=self.current_config.auto_stop_minutes,
            warehouse_channel=self.current_config.warehouse_channel,
        )
        _require(self.warehouse.current_epoch.epoch_id == expected_epoch, f"epoch id mismatch: expected {expected_epoch}")

        evidence_ids = set(self.evidence)
        _require(len(evidence_ids) == len(self.evidence), "evidence ids must be unique")
        for key, item in self.evidence.items():
            _require(key == item.evidence_id, "evidence dictionary key must equal evidence_id")
            item.validate()
            expected_evidence_id = make_evidence_id(
                workspace_id=self.warehouse.workspace_id,
                warehouse_id=self.warehouse.warehouse_id,
                epoch_id=self.warehouse.current_epoch.epoch_id,
                metric=item.metric, value=item.value, unit=item.unit, source=item.source,
                source_field=item.source_field, observation_window=item.observation_window,
                threshold=item.threshold, comparator=item.comparator,
            )
            _require(item.evidence_id == expected_evidence_id, f"evidence id mismatch: expected {expected_evidence_id}")
        _require(self.provenance.evidence_fingerprint == self.evidence_fingerprint_expected(), "evidence_fingerprint does not match evidence registry")

        candidate_ids = [x.candidate_id for x in self.candidates]
        _require(len(candidate_ids) == len(set(candidate_ids)), "candidate ids must be unique")
        for candidate in self.candidates:
            candidate.validate(evidence_ids=evidence_ids)
            expected_candidate_id = make_candidate_id(
                workspace_id=self.warehouse.workspace_id, warehouse_id=self.warehouse.warehouse_id,
                epoch_id=self.warehouse.current_epoch.epoch_id, technique_id=candidate.technique_id,
                knob=candidate.knob.value, current_value=candidate.current_value, recommended_value=candidate.recommended_value,
                evidence_refs=candidate.evidence_refs, policy_version=self.provenance.policy_version,
                analyzer_version=self.provenance.analyzer_version, optimizer_version=self.provenance.optimizer_version,
                estimator_version=self.provenance.estimator_version, reviewer_version=self.provenance.reviewer_version,
                decision_graph_version=self.provenance.decision_graph_version, cost_profile=profile,
            )
            _require(candidate.candidate_id == expected_candidate_id, f"candidate id mismatch: expected {expected_candidate_id}")

        scenario_ids = [x.scenario_id for x in self.scenarios]
        _require(len(scenario_ids) == len(set(scenario_ids)), "scenario ids must be unique")
        for scenario in self.scenarios:
            scenario.validate(candidate_ids=candidate_ids, evidence_ids=evidence_ids)
            expected_scenario_id = make_scenario_id(
                workspace_id=self.warehouse.workspace_id, warehouse_id=self.warehouse.warehouse_id,
                epoch_id=self.warehouse.current_epoch.epoch_id, scenario_type=scenario.scenario_type.value,
                ordered_candidate_ids=scenario.candidate_refs, compatibility_status=scenario.compatibility.status.value,
                policy_version=self.provenance.policy_version, bundle_estimator_version=self.provenance.bundle_estimator_version,
                decision_graph_version=self.provenance.decision_graph_version, cost_profile=profile,
            )
            _require(scenario.scenario_id == expected_scenario_id, f"scenario id mismatch: expected {expected_scenario_id}")

        self.recommended_plan.validate(scenario_ids=scenario_ids)
        recommended = [x for x in self.scenarios if x.status == ScenarioStatus.RECOMMENDED]
        if self.recommended_plan.scenario_id is None:
            _require(len(recommended) == 0, "scenario marked RECOMMENDED but recommended_plan has no scenario_id")
        else:
            _require(len(recommended) == 1, "exactly one scenario must be RECOMMENDED")
            _require(recommended[0].scenario_id == self.recommended_plan.scenario_id, "RECOMMENDED scenario mismatch")

        self._validate_reevaluation_and_change(scenario_by_id_for_plan={x.scenario_id: x for x in self.scenarios})
        self._validate_run_scope()

        self.package_economics.validate(
            scenario_ids=scenario_ids,
            recommended_scenario_id=self.recommended_plan.scenario_id,
        )
        included = set(self.package_economics.included_scenario_ids)
        excluded = {x.scenario_id for x in self.package_economics.excluded_scenarios}
        _require(included | excluded == set(scenario_ids), "package economics must classify every scenario as included or excluded")
        scenario_by_id = {x.scenario_id: x for x in self.scenarios}
        assessable = []
        for sid in self.package_economics.included_scenario_ids:
            total = scenario_by_id[sid].portfolio_economics.savings_365d.total
            _require(total is not None, f"included scenario {sid} must have total savings range")
            assessable.append((sid, total))
        if assessable:
            low_sid, low_range = min(assessable, key=lambda x: x[1].low)
            high_sid, high_range = max(assessable, key=lambda x: x[1].high)
            _require(self.recommended_plan.scenario_id in included, "recommended scenario must be economically included when envelope exists")
            base_range = scenario_by_id[self.recommended_plan.scenario_id].portfolio_economics.savings_365d.total
            expected_savings = Range3(low_range.low, base_range.base, high_range.high)
            _require(self.package_economics.estimated_savings_365d == expected_savings, "package savings envelope does not match scenario ranges")
            baseline_total = self.package_economics.baseline_365d.total_cost
            if baseline_total is not None:
                expected_cost = Range3(
                    round(baseline_total - expected_savings.high, 2),
                    round(baseline_total - expected_savings.base, 2),
                    round(baseline_total - expected_savings.low, 2),
                )
                _require(self.package_economics.estimated_cost_365d == expected_cost, "package cost envelope does not match baseline minus scenario savings")
            envelope = self.package_economics.scenario_envelope
            _require(envelope.minimum_savings is not None and envelope.minimum_savings.scenario_id == low_sid, "minimum envelope source scenario mismatch")
            _require(envelope.maximum_savings is not None and envelope.maximum_savings.scenario_id == high_sid, "maximum envelope source scenario mismatch")
            _require(envelope.recommended_base is not None and envelope.recommended_base.scenario_id == self.recommended_plan.scenario_id, "base envelope source scenario mismatch")
        else:
            _require(self.package_economics.estimated_savings_365d is None and self.package_economics.estimated_cost_365d is None, "empty scenario envelope requires null package estimates")

        self.realization.validate(
            package_id=self.recommendation_package_id,
            scenario_ids=scenario_ids,
            candidate_ids=candidate_ids,
            evidence_ids=evidence_ids,
        )

        # Currency is package-wide.  We deliberately reject silent currency mixing.
        _require(self.package_economics.currency == self.cost_baseline.currency, "package economics currency must match cost baseline")
        _require(self.realization.aggregate.currency == self.cost_baseline.currency, "realization currency must match package currency")

        # Assessment must describe the actual candidate set.
        counts = {status: 0 for status in RecommendationStatus}
        for candidate in self.candidates:
            counts[candidate.status] += 1
        summary = self.assessment.summary
        _require(summary.candidate_count == len(self.candidates), "assessment candidate_count mismatch")
        _require(summary.actionable_count == counts[RecommendationStatus.ACTIONABLE], "actionable_count mismatch")
        _require(summary.test_required_count == counts[RecommendationStatus.TEST_REQUIRED], "test_required_count mismatch")
        _require(summary.review_count == counts[RecommendationStatus.REVIEW], "review_count mismatch")
        _require(summary.blocked_count == counts[RecommendationStatus.BLOCKED], "blocked_count mismatch")
        _require(summary.no_change_count == counts[RecommendationStatus.NO_CHANGE], "no_change_count mismatch")
        _require(summary.not_applicable_count == counts[RecommendationStatus.NOT_APPLICABLE], "not_applicable_count mismatch")
        _require(summary.insufficient_evidence_count == counts[RecommendationStatus.INSUFFICIENT_EVIDENCE], "insufficient_evidence_count mismatch")

        if self.provenance.package_hash_full_sha256 is not None:
            _require(self.provenance.package_hash_full_sha256 == self.full_package_hash(), "package_hash_full_sha256 mismatch")
        if verify_content_id:
            expected = self.expected_package_id()
            _require(self.recommendation_package_id == expected, f"package id mismatch: expected {expected}")

    def _cost_history_days_evidence(self) -> Optional[float]:
        for item in self.evidence.values():
            if item.metric == "cost_history_days" and isinstance(item.value, (int, float)):
                return float(item.value)
        return None

    def _evidence_value(self, metric: str) -> Optional[float]:
        for item in self.evidence.values():
            if item.metric == metric and isinstance(item.value, (int, float)) and not isinstance(item.value, bool):
                return float(item.value)
        return None

    def active_reevaluation_codes(self) -> List[str]:
        """Sorted blocking gate codes active now, derived from evidence: COST_HISTORY_INSUFFICIENT when cost_history_days is below
        REEVALUATION_COST_HISTORY_DAYS, CURRENT_CONFIG_EPOCH_TOO_NEW when billed_cost_days is below REEVALUATION_READY_BILLED_DAYS or missing (fail closed).
        """
        codes = []
        days = self._cost_history_days_evidence()
        if days is not None and days < REEVALUATION_COST_HISTORY_DAYS:
            codes.append("COST_HISTORY_INSUFFICIENT")
        billed = self._evidence_value("billed_cost_days")
        if billed is None or billed < REEVALUATION_READY_BILLED_DAYS:  # missing record: not ready (fail closed)
            codes.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
        return sorted(codes)

    def _validate_reevaluation_and_change(self, *, scenario_by_id_for_plan: Mapping[str, Scenario]) -> None:
        start_time = self.warehouse.current_epoch.start_time
        window = self.recommended_plan.reevaluation
        active = self.active_reevaluation_codes()
        if window is None:
            _require(not active, f"reevaluation window is required while gates are active: {active}")
        else:
            _require(list(window.blocking_codes) == active, "reevaluation.blocking_codes must equal the gates that are currently active")
            _require(
                window.earliest_reevaluation_time == expected_earliest_reevaluation_time(start_time, window.blocking_codes, self.provenance.generated_at),
                "reevaluation.earliest_reevaluation_time does not match the epoch start, analysis time and active gates",
            )
            _require(_parse_utc(window.earliest_reevaluation_time) >= _parse_utc(start_time), "reevaluation time must not precede the epoch start")
        if self.recommended_plan.next_action == "WAIT_FOR_REEVALUATION":
            _require(window is not None, "WAIT_FOR_REEVALUATION requires a reevaluation window")
            scenario = scenario_by_id_for_plan.get(self.recommended_plan.scenario_id) if self.recommended_plan.scenario_id else None
            if scenario is not None:
                _require(
                    not any(s.execution_state in (ExecutionState.READY, ExecutionState.READY_FOR_CANARY) for s in scenario.execution_plan),
                    "WAIT_FOR_REEVALUATION is only valid when the recommended scenario has no ready step",
                )
        change = self.observed_change
        if change is not None:
            change.validate()
            _require(change.changed_at == start_time, "observed_change.changed_at must equal the current epoch start")
            _require(change.previous_epoch_id != self.warehouse.current_epoch.epoch_id, "observed_change.previous_epoch_id must differ from the current epoch")
        for candidate in self.candidates:
            if "CANARY_GUARDRAIL_BREACHED" in candidate.blockers:
                # A canary rollback restores the value the observed change replaced (contract 2.0.0).
                _require(candidate.status == RecommendationStatus.ACTIONABLE, "a CANARY_GUARDRAIL_BREACHED candidate must be ACTIONABLE")
                _require(change is not None, "a CANARY_GUARDRAIL_BREACHED candidate requires observed_change")
                previous = next((c.previous_value for c in change.changes if c.knob == candidate.knob), None) if change is not None else None
                _require(previous is not None and candidate.recommended_value == previous,
                         "a CANARY_GUARDRAIL_BREACHED candidate must restore observed_change.previous_value of its knob")
        carried = self.carried_forward_plan
        if carried is not None:
            carried.validate()
            _require(change is not None, "carried_forward_plan requires observed_change")
            _require(carried.source_epoch_id == change.previous_epoch_id, "carried_forward.source_epoch_id must equal observed_change.previous_epoch_id")
            _require(
                window is not None and "CURRENT_CONFIG_EPOCH_TOO_NEW" in window.blocking_codes,
                "carried_forward_plan is only valid while the current configuration is not ready (readiness gate active)",
            )
            current = {(c.knob, repr(c.current_value)) for c in change.changes}
            for step in carried.steps:
                if step.disposition == CarriedForwardDisposition.COMPLETED:
                    _require((step.knob, repr(step.recommended_value)) in current, "COMPLETED carried-forward step must match an observed change")
            if carried.match_outcome == MatchOutcome.MATCHED:
                _require(len(change.changes) == 1, "MATCHED requires exactly one observed knob change")

    def _validate_run_scope(self) -> None:
        """A restricted run's package holds exactly one candidate per in-scope knob plus its safety inclusions, and only
        scenarios of in-scope types (the no-change baseline when current-type tuning is outside the scope)."""
        scope = self.run_scope
        if scope is None:
            _require(all(s.scenario_type != ScenarioType.NO_CHANGE_BASELINE for s in self.scenarios),
                     "a NO_CHANGE_BASELINE scenario requires a run_scope")
            return
        scope.validate()
        in_scope = set(scope.knobs())
        knobs = [c.knob for c in self.candidates]
        _require(len(knobs) == len(set(knobs)), "a scoped package has at most one candidate per knob")
        _require(in_scope <= set(knobs), "a scoped package needs one candidate per in-scope knob")
        by_id = {c.candidate_id: c for c in self.candidates}
        safety = {x.knob: x for x in scope.safety_inclusions}
        for c in self.candidates:
            if c.knob in in_scope:
                continue
            x = safety.get(c.knob)
            _require(x is not None and x.candidate_id == c.candidate_id, f"candidate {c.candidate_id} is outside the run scope")
        for x in scope.safety_inclusions:
            c = by_id.get(x.candidate_id)
            _require(c is not None and c.knob == x.knob and "CANARY_GUARDRAIL_BREACHED" in c.blockers,
                     "a safety inclusion must reference a CANARY_GUARDRAIL_BREACHED candidate of its knob")
        allowed = set(scope.scenario_types())
        if ScenarioType.IN_PLACE_OPTIMIZATION not in allowed:
            allowed.add(ScenarioType.IN_PLACE_OPTIMIZATION if scope.safety_inclusions else ScenarioType.NO_CHANGE_BASELINE)
        for s in self.scenarios:
            _require(s.scenario_type in allowed, f"scenario {s.scenario_id} ({s.scenario_type.value}) is outside the run scope")

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible primitives of the whole package (see to_primitive)."""
        return to_primitive(self)

    def evidence_fingerprint_expected(self) -> str:
        """Recompute the evidence fingerprint from the registry (same payload as make_evidence_fingerprint)."""
        payload = {
            "contract_version": self.contract_version,
            "workspace_id": self.warehouse.workspace_id,
            "warehouse_id": self.warehouse.warehouse_id,
            "epoch_id": self.warehouse.current_epoch.epoch_id,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "metric": item.metric,
                    "value": item.value,
                    "unit": item.unit,
                    "source": item.source,
                    "source_field": item.source_field,
                    "epoch_scope": item.epoch_scope,
                    "observation_window": item.observation_window,
                    "threshold": item.threshold,
                    "comparator": item.comparator,
                }
                for _, item in sorted(self.evidence.items())
            ],
        }
        return f"SHA256:{hashlib.sha256(canonical_json_for_hash(payload).encode('utf-8')).hexdigest().upper()}"

    def package_hash_payload(self) -> Dict[str, Any]:
        """Explicit payload behind the package id and full hash.

        Hashed: contract and component versions, workspace/warehouse/epoch, current_config, evidence fingerprint, ordered candidate and scenario ids, the
        re-evaluation status, time and codes (not its prose method), the observed-change and carried-forward fingerprints, the cost profile's name
        and applied thresholds, and the run scope. Not hashed: generated_at, the cost profile's source and source_detail, prose and other volatile
        presentation metadata.
        """
        return {
            "contract_version": self.contract_version,
            "workspace_id": self.warehouse.workspace_id,
            "warehouse_id": self.warehouse.warehouse_id,
            "current_epoch_id": self.warehouse.current_epoch.epoch_id,
            "current_config": self.current_config,
            "evidence_fingerprint": self.provenance.evidence_fingerprint,
            "policy_version": self.provenance.policy_version,
            "analyzer_version": self.provenance.analyzer_version,
            "optimizer_version": self.provenance.optimizer_version,
            "modeler_version": self.provenance.modeler_version,
            "estimator_version": self.provenance.estimator_version,
            "bundle_estimator_version": self.provenance.bundle_estimator_version,
            "reviewer_version": self.provenance.reviewer_version,
            "decision_graph_version": self.provenance.decision_graph_version,
            "technique_catalog_version": self.provenance.technique_catalog_version,
            "ordered_candidate_ids": [x.candidate_id for x in self.candidates],
            "ordered_scenario_ids": [x.scenario_id for x in self.scenarios],
            # contract 1.2.0: blocks that change what a consumer sees without changing candidate/scenario/evidence IDs.
            # Prose (reevaluation.method) is excluded.
            "reevaluation": None if self.recommended_plan.reevaluation is None else {
                "status": self.recommended_plan.reevaluation.status.value,
                "earliest_reevaluation_time": self.recommended_plan.reevaluation.earliest_reevaluation_time,
                "blocking_codes": list(self.recommended_plan.reevaluation.blocking_codes),
            },
            "observed_change_fingerprint": None if self.observed_change is None else self.observed_change.fingerprint(),
            "carried_forward_fingerprint": None if self.carried_forward_plan is None else self.carried_forward_plan.fingerprint(),
            "cost_profile": self.cost_profile.hash_payload(),
            # contract 3.1.0: a restricted scope changes what the package contains (null = full scope).
            "run_scope": self.run_scope,
        }

    def expected_package_id(self) -> str:
        """PKG- id recomputed from package_hash_payload."""
        return content_id("PKG", self.package_hash_payload())

    def full_package_hash(self) -> str:
        """Full uppercase SHA-256 hex of package_hash_payload (the value behind provenance.package_hash_full_sha256)."""
        return sha256_hex(self.package_hash_payload())


def package_hash_payload_from_document(document: Mapping[str, Any]) -> Dict[str, Any]:
    """``RecommendationPackage.package_hash_payload`` rebuilt from a serialized package (the JSON a consumer reads).

    The same fields as the dataclass method; the observed-change and carried-forward fingerprints hash their serialized
    blocks, which is what the dataclass fingerprints hash. A consumer recomputes the package id and full hash with it
    (``content_id("PKG", ...)``, ``sha256_hex(...)``) without rebuilding the frozen package. Raises KeyError on a missing
    hashed field.
    """
    provenance, warehouse, plan = document["provenance"], document["warehouse"], document["recommended_plan"]
    reevaluation = plan.get("reevaluation")
    observed, carried = document["observed_change"], document["carried_forward_plan"]
    profile = document["cost_profile"]
    return {
        "contract_version": document["contract_version"],
        "workspace_id": warehouse["workspace_id"],
        "warehouse_id": warehouse["warehouse_id"],
        "current_epoch_id": warehouse["current_epoch"]["epoch_id"],
        "current_config": document["current_config"],
        "evidence_fingerprint": provenance["evidence_fingerprint"],
        "policy_version": provenance["policy_version"],
        "analyzer_version": provenance["analyzer_version"],
        "optimizer_version": provenance["optimizer_version"],
        "modeler_version": provenance["modeler_version"],
        "estimator_version": provenance["estimator_version"],
        "bundle_estimator_version": provenance["bundle_estimator_version"],
        "reviewer_version": provenance["reviewer_version"],
        "decision_graph_version": provenance["decision_graph_version"],
        "technique_catalog_version": provenance["technique_catalog_version"],
        "ordered_candidate_ids": [x["candidate_id"] for x in document["candidates"]],
        "ordered_scenario_ids": [x["scenario_id"] for x in document["scenarios"]],
        "reevaluation": None if reevaluation is None else {
            "status": reevaluation["status"],
            "earliest_reevaluation_time": reevaluation["earliest_reevaluation_time"],
            "blocking_codes": list(reevaluation["blocking_codes"]),
        },
        "observed_change_fingerprint": None if observed is None else f"SHA256:{sha256_hex(observed).upper()}",
        "carried_forward_fingerprint": None if carried is None else f"SHA256:{sha256_hex(carried).upper()}",
        "cost_profile": {"name": profile["name"], "applied_thresholds": profile["applied_thresholds"]},
        "run_scope": document["run_scope"],
    }
