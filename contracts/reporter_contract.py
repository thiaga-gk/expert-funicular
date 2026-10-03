"""Reporter contract v1.1.0 for Databricks SQL Warehouse Optimization.

This module is intentionally independent from publisher.py.  It defines the stable,
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
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

CONTRACT_VERSION = "1.1.0"
HASH_HEX_LENGTH = 24  # 96-bit display identifier; full SHA-256 remains available in provenance.


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class PhotonStatus(str, Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class EvidenceQualityStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class AssessmentStatus(str, Enum):
    OPTIMIZATION_AVAILABLE = "OPTIMIZATION_AVAILABLE"
    NO_CHANGE = "NO_CHANGE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RecommendationStatus(str, Enum):
    ACTIONABLE = "ACTIONABLE"
    TEST_REQUIRED = "TEST_REQUIRED"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    NO_CHANGE = "NO_CHANGE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ExecutionState(str, Enum):
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


class ScenarioStatus(str, Enum):
    RECOMMENDED = "RECOMMENDED"
    ALTERNATIVE = "ALTERNATIVE"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ScenarioType(str, Enum):
    IN_PLACE_OPTIMIZATION = "IN_PLACE_OPTIMIZATION"
    WAREHOUSE_TYPE_MIGRATION = "WAREHOUSE_TYPE_MIGRATION"
    NO_CHANGE_BASELINE = "NO_CHANGE_BASELINE"


class CompatibilityStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class CompatibilityMode(str, Enum):
    B_READ_ONLY_API = "B_READ_ONLY_API"
    A_HITL_FALLBACK = "A_HITL_FALLBACK"
    B_READ_ONLY_API_WITH_A_FALLBACK = "B_READ_ONLY_API_WITH_A_FALLBACK"
    SYSTEM_TABLE_ONLY = "SYSTEM_TABLE_ONLY"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EstimationStatus(str, Enum):
    MODELED_RANGE = "MODELED_RANGE"
    PROVISIONAL_MODELED_RANGE = "PROVISIONAL_MODELED_RANGE"
    PRE_CANARY_MODELED_RANGE = "PRE_CANARY_MODELED_RANGE"
    MEASURED = "MEASURED"
    NO_DELTA_MODELED = "NO_DELTA_MODELED"
    UNAVAILABLE = "UNAVAILABLE"


class CostAvailability(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class CostBasis(str, Enum):
    OBSERVED_DATABRICKS_BILLING = "OBSERVED_DATABRICKS_BILLING"
    OBSERVED_AWS_BILLING = "OBSERVED_AWS_BILLING"
    MODELED_AWS_LIST_PRICE = "MODELED_AWS_LIST_PRICE"
    MODELED_CURRENT_EPOCH = "MODELED_CURRENT_EPOCH"
    FORECAST = "FORECAST"
    COUNTERFACTUAL = "COUNTERFACTUAL"
    UNAVAILABLE = "UNAVAILABLE"


class TunableKnob(str, Enum):
    WAREHOUSE_TYPE = "warehouse_type"
    WAREHOUSE_SIZE = "warehouse_size"
    MIN_CLUSTERS = "min_clusters"
    MAX_CLUSTERS = "max_clusters"
    AUTO_STOP_MINUTES = "auto_stop_minutes"
    WAREHOUSE_CHANNEL = "warehouse_channel"
    ENABLE_PHOTON = "enable_photon"


class Objective(str, Enum):
    COST = "COST"
    PERFORMANCE = "PERFORMANCE"
    COST_AND_PERFORMANCE = "COST_AND_PERFORMANCE"
    COST_GUARDRAIL = "COST_GUARDRAIL"
    STABILITY = "STABILITY"
    RELIABILITY = "RELIABILITY"
    OPERATIONS = "OPERATIONS"
    MIGRATION = "MIGRATION"


class DependencyType(str, Enum):
    CANDIDATE = "CANDIDATE"
    TECHNIQUE = "TECHNIQUE"
    KNOB = "KNOB"
    COMPATIBILITY_CHECK = "COMPATIBILITY_CHECK"
    EVIDENCE = "EVIDENCE"


class ValidationType(str, Enum):
    NONE = "NONE"
    A_B_CANARY = "A_B_CANARY"
    HITL = "HITL"
    COMPATIBILITY = "COMPATIBILITY"
    REMEASURE = "REMEASURE"


class PlanStepType(str, Enum):
    CANDIDATE = "CANDIDATE"
    REMEASURE = "REMEASURE"
    COMPATIBILITY_VALIDATION = "COMPATIBILITY_VALIDATION"
    CANARY = "CANARY"
    COMPARE = "COMPARE"
    ROLLBACK_GATE = "ROLLBACK_GATE"


class BoundName(str, Enum):
    LOW = "LOW"
    BASE = "BASE"
    HIGH = "HIGH"


class WorkloadLatencyClass(str, Enum):
    INTERACTIVE = "INTERACTIVE"
    MIXED = "MIXED"
    BATCH = "BATCH"
    UNKNOWN = "UNKNOWN"


class ConcurrencyPattern(str, Enum):
    STEADY = "STEADY"
    BURSTY = "BURSTY"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ActivityPattern(str, Enum):
    CONTINUOUS = "CONTINUOUS"
    BUSINESS_HOURS = "BUSINESS_HOURS"
    SCHEDULED = "SCHEDULED"
    SPORADIC = "SPORADIC"
    UNKNOWN = "UNKNOWN"


class RealizationStatus(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    VALIDATING = "VALIDATING"
    REALIZED = "REALIZED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


class ValidationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"


class RealizationOutcomeStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_iso8601(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def _validate_identifier(value: str, prefix: str, *, length: int = HASH_HEX_LENGTH) -> None:
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


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def to_primitive(value: Any) -> Any:
    """Convert nested contract objects to JSON-compatible primitives."""
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
    return json.dumps(
        canonicalize_for_hash(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_for_hash(payload).encode("utf-8")).hexdigest().upper()


def content_id(prefix: str, payload: Mapping[str, Any]) -> str:
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
    """Create the immutable config-epoch identity from authoritative system-table knobs.

    Photon is intentionally excluded: it is an observed billing feature in this product,
    not an authoritative system.compute.warehouses epoch field.
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
    return content_id("EVID", evidence_id_payload(**kwargs))


def make_evidence_fingerprint(
    *,
    workspace_id: str,
    warehouse_id: str,
    epoch_id: str,
    evidence_records: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the decision-relevant evidence, excluding presentation prose."""
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
) -> str:
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
) -> str:
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
    score: Optional[float]
    level: ConfidenceLevel
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    method: Optional[str] = None

    def validate(self) -> None:
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
    low: float
    base: float
    high: float

    def validate(self, *, nonnegative: bool = False, field_name: str = "range") -> None:
        _require(self.low <= self.base <= self.high, f"{field_name} must satisfy low <= base <= high")
        if nonnegative:
            _require(self.low >= 0, f"{field_name}.low must be >= 0")


@dataclass(frozen=True)
class CostBreakdown:
    dbu_cost: Optional[float]
    aws_cost: Optional[float]
    total_cost: Optional[float]
    availability: CostAvailability
    dbu_basis: CostBasis
    aws_basis: CostBasis

    def validate(self) -> None:
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
    dbu: Optional[Range3]
    aws: Optional[Range3]
    total: Optional[Range3]

    def validate(self, *, nonnegative: bool, field_name: str) -> None:
        for component, value in (("dbu", self.dbu), ("aws", self.aws), ("total", self.total)):
            if value is not None:
                value.validate(nonnegative=nonnegative, field_name=f"{field_name}.{component}")
        if self.dbu is not None and self.aws is not None and self.total is not None:
            for bound in ("low", "base", "high"):
                expected = getattr(self.dbu, bound) + getattr(self.aws, bound)
                actual = getattr(self.total, bound)
                _require(abs(expected - actual) <= 0.03, f"{field_name}.{bound} total must equal DBU + AWS")


# ---------------------------------------------------------------------------
# Warehouse identity and current state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentEpoch:
    epoch_id: str
    start_time: str
    age_days: int
    is_current: bool

    def validate(self) -> None:
        _validate_identifier(self.epoch_id, "EPOCH")
        _require(_is_iso8601(self.start_time), "current_epoch.start_time must be ISO-8601")
        _require(self.age_days >= 0, "current_epoch.age_days must be >= 0")
        _require(self.is_current is True, "current_epoch.is_current must be true in a recommendation package")


@dataclass(frozen=True)
class WarehouseIdentity:
    workspace_id: str
    warehouse_id: str
    warehouse_name: str
    owner: Optional[str]
    region: str
    current_epoch: CurrentEpoch

    def validate(self) -> None:
        _require(bool(self.workspace_id.strip()), "workspace_id is required")
        _require(bool(self.warehouse_id.strip()), "warehouse_id is required")
        _require(bool(self.warehouse_name.strip()), "warehouse_name is required")
        _require(bool(self.region.strip()), "region is required")
        self.current_epoch.validate()


@dataclass(frozen=True)
class PhotonConfigObservation:
    status: PhotonStatus
    evidence_source: str
    confidence: Confidence
    enabled_usage_pct: Optional[float] = None
    known_feature_coverage_pct: Optional[float] = None
    freshness_days: Optional[int] = None

    def validate(self) -> None:
        self.confidence.validate()
        _validate_pct(self.enabled_usage_pct, "photon.enabled_usage_pct")
        _validate_pct(self.known_feature_coverage_pct, "photon.known_feature_coverage_pct")
        if self.freshness_days is not None:
            _require(self.freshness_days >= 0, "photon.freshness_days must be >= 0")
        if self.status == PhotonStatus.UNKNOWN:
            _require(self.confidence.level in {ConfidenceLevel.LOW, ConfidenceLevel.UNKNOWN}, "UNKNOWN Photon should not carry HIGH/MEDIUM confidence")


@dataclass(frozen=True)
class CurrentConfig:
    warehouse_type: str
    warehouse_size: str
    min_clusters: int
    max_clusters: int
    auto_stop_minutes: int
    warehouse_channel: str
    photon: PhotonConfigObservation

    def validate(self) -> None:
        _require(bool(self.warehouse_type), "warehouse_type is required")
        _require(bool(self.warehouse_size), "warehouse_size is required")
        _require(self.min_clusters >= 1, "min_clusters must be >= 1")
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
    query_count: int
    query_pct: float

    def validate(self) -> None:
        _require(self.query_count >= 0, "query_count must be >= 0")
        _validate_pct(self.query_pct, "query_pct")


@dataclass(frozen=True)
class ClientApplicationStat:
    query_count: int

    def validate(self) -> None:
        _require(self.query_count >= 0, "client application query_count must be >= 0")


@dataclass(frozen=True)
class WorkloadActivity:
    queries_30d: int
    active_days_30d: int
    queries_per_active_day: float

    def validate(self) -> None:
        _require(self.queries_30d >= 0, "queries_30d must be >= 0")
        _require(0 <= self.active_days_30d <= 30, "active_days_30d must be in [0, 30]")
        _require(self.queries_per_active_day >= 0, "queries_per_active_day must be >= 0")


@dataclass(frozen=True)
class WorkloadCharacteristics:
    latency_class: WorkloadLatencyClass
    concurrency_pattern: ConcurrencyPattern
    activity_pattern: ActivityPattern


@dataclass(frozen=True)
class WorkloadProfile:
    primary_query_source: str
    query_sources: Dict[str, QuerySourceStat]
    client_applications: Dict[str, ClientApplicationStat]
    activity: WorkloadActivity
    workload_characteristics: WorkloadCharacteristics
    multi_label: bool = True

    def validate(self) -> None:
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
    model: str
    confidence: Confidence
    assumptions: List[str]

    def validate(self) -> None:
        _require(bool(self.model), "forecast.model is required")
        self.confidence.validate()


@dataclass(frozen=True)
class CostBaseline:
    currency: str
    actual_365d: CostBreakdown
    forward_365d: CostBreakdown
    forecast: ForecastInfo

    def validate(self) -> None:
        _require(len(self.currency) == 3 and self.currency.isalpha() and self.currency.upper() == self.currency, "currency must be ISO-like uppercase 3-letter code")
        self.actual_365d.validate()
        self.forward_365d.validate()
        self.forecast.validate()


@dataclass(frozen=True)
class AssessmentSummary:
    candidate_count: int
    actionable_count: int
    test_required_count: int
    review_count: int
    blocked_count: int
    no_change_count: int
    not_applicable_count: int
    insufficient_evidence_count: int

    def validate(self) -> None:
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
    status: EvidenceQualityStatus
    confidence: Confidence
    query_sample_sufficient: bool
    cost_history_sufficient: bool
    epoch_mature: bool
    source_coverage_pct: float

    def validate(self) -> None:
        self.confidence.validate()
        _validate_pct(self.source_coverage_pct, "source_coverage_pct")


@dataclass(frozen=True)
class Assessment:
    overall_status: AssessmentStatus
    summary: AssessmentSummary
    evidence_quality: EvidenceQuality

    def validate(self) -> None:
        self.summary.validate()
        self.evidence_quality.validate()


# ---------------------------------------------------------------------------
# Evidence registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
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
    dependency_type: DependencyType
    ref_id: str
    condition: Optional[str] = None

    def validate(self) -> None:
        _require(bool(self.ref_id), "dependency.ref_id is required")


@dataclass(frozen=True)
class IndependentEconomics:
    is_additive: bool
    estimation_status: EstimationStatus
    baseline_365d: CostBreakdown
    estimated_cost_365d: ComponentRanges
    savings_365d: ComponentRanges

    def validate(self) -> None:
        _require(self.is_additive is False, "candidate economics must be non-additive")
        self.baseline_365d.validate()
        self.estimated_cost_365d.validate(nonnegative=True, field_name="estimated_cost_365d")
        self.savings_365d.validate(nonnegative=False, field_name="savings_365d")


@dataclass(frozen=True)
class CandidateRecommendation:
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
    check: str
    status: CheckStatus
    requires_hitl: bool
    evidence_refs: List[str] = field(default_factory=list)
    details: Optional[str] = None


@dataclass(frozen=True)
class Compatibility:
    status: CompatibilityStatus
    mode: CompatibilityMode
    checks: List[CompatibilityCheck]


@dataclass(frozen=True)
class ValidationGate:
    validation_type: ValidationType
    success_metrics: List[str]
    remeasure_days: Optional[int] = None
    action_after: Optional[str] = None

    def validate(self) -> None:
        if self.remeasure_days is not None:
            _require(1 <= self.remeasure_days <= 90, "remeasure_days must be in [1, 90]")


@dataclass(frozen=True)
class StepProjection:
    """Cumulative stateful savings after this step, signed (positive = savings), with its pricing basis."""
    pricing_basis: PricingBasis
    running_savings_365d: ComponentRanges

    def validate(self) -> None:
        self.running_savings_365d.validate(nonnegative=False, field_name="plan_step.projected_economics.running_savings_365d")


@dataclass(frozen=True)
class PlanStep:
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
        _require(self.step >= 1, "plan step must be >= 1")
        self.confidence.validate()
        self.validation.validate()
        if self.projected_economics is not None:
            self.projected_economics.validate()
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
        _require(self.is_additive is False, "scenario economics are stateful and non-additive")
        _require(bool(self.method), "scenario economics method is required")
        self.baseline_365d.validate()
        self.estimated_cost_365d.validate(nonnegative=True, field_name="scenario.estimated_cost_365d")
        self.savings_365d.validate(nonnegative=False, field_name="scenario.savings_365d")
        _require(
            (self.contingent_estimated_cost_365d is None) == (self.contingent_savings_365d is None),
            "contingent cost and savings must be published together or both null",
        )
        if self.contingent_savings_365d is not None:
            self.contingent_estimated_cost_365d.validate(nonnegative=True, field_name="scenario.contingent_estimated_cost_365d")
            self.contingent_savings_365d.validate(nonnegative=False, field_name="scenario.contingent_savings_365d")


@dataclass(frozen=True)
class Scenario:
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


@dataclass(frozen=True)
class RecommendedPlan:
    scenario_id: Optional[str]
    selection_reason_code: str
    next_action: str
    requires_hitl: bool
    decision_summary: str

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        if self.scenario_id is not None:
            _validate_identifier(self.scenario_id, "SCN")
            _require(self.scenario_id in set(scenario_ids), "recommended_plan references unknown scenario")
        _require(bool(self.selection_reason_code), "selection_reason_code is required")
        _require(bool(self.next_action), "next_action is required")
        _require(bool(self.decision_summary), "decision_summary is required")


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
        if nonnegative:
            _validate_nonnegative(self.dbu, f"{field_name}.dbu")
            _validate_nonnegative(self.aws, f"{field_name}.aws")
            _validate_nonnegative(self.total, f"{field_name}.total")
        if self.dbu is not None and self.aws is not None and self.total is not None:
            _require(abs((self.dbu + self.aws) - self.total) <= 0.03, f"{field_name}.total must equal DBU + AWS")


@dataclass(frozen=True)
class ScenarioEnvelopePoint:
    scenario_id: str
    bound: BoundName
    savings_365d: EconomicsPoint
    estimated_cost_365d: EconomicsPoint

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        _validate_identifier(self.scenario_id, "SCN")
        _require(self.scenario_id in set(scenario_ids), "scenario envelope references unknown scenario")
        self.savings_365d.validate(nonnegative=False, field_name="scenario_envelope.savings_365d")
        self.estimated_cost_365d.validate(nonnegative=True, field_name="scenario_envelope.estimated_cost_365d")


@dataclass(frozen=True)
class ExcludedScenario:
    scenario_id: str
    reason_code: str

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
        _validate_identifier(self.scenario_id, "SCN")
        _require(self.scenario_id in set(scenario_ids), "excluded scenario references unknown scenario")
        _require(bool(self.reason_code), "excluded scenario reason_code is required")


@dataclass(frozen=True)
class ScenarioEnvelope:
    minimum_savings: Optional[ScenarioEnvelopePoint]
    recommended_base: Optional[ScenarioEnvelopePoint]
    maximum_savings: Optional[ScenarioEnvelopePoint]

    def validate(self, *, scenario_ids: Iterable[str]) -> None:
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
    method: str
    is_additive: bool
    is_probability_interval: bool

    def validate(self) -> None:
        _require(self.method == "SCENARIO_ENVELOPE", "package economics method must be SCENARIO_ENVELOPE")
        _require(self.is_additive is False, "package economics must be non-additive")
        _require(self.is_probability_interval is False, "package range is not a probability interval")


@dataclass(frozen=True)
class PackageEconomics:
    currency: str
    baseline_365d: CostBreakdown
    estimated_savings_365d: Optional[Range3]
    estimated_cost_365d: Optional[Range3]
    scenario_envelope: ScenarioEnvelope
    included_scenario_ids: List[str]
    excluded_scenarios: List[ExcludedScenario]
    semantics: PackageEconomicsSemantics

    def validate(self, *, scenario_ids: Iterable[str], recommended_scenario_id: Optional[str]) -> None:
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
    applied_at: str
    applied_by: str
    previous_config: Dict[str, Any]
    applied_config: Dict[str, Any]

    def validate(self) -> None:
        _require(_is_iso8601(self.applied_at), "applied_at must be ISO-8601")
        _require(bool(self.applied_by), "applied_by is required")


@dataclass(frozen=True)
class RealizationValidation:
    validation_window_start: str
    validation_window_end: str
    validation_status: ValidationOutcome
    performance_guardrails_passed: Optional[bool]
    finding_refs: List[str]
    notes: List[str]

    def validate(self, *, evidence_ids: Iterable[str]) -> None:
        _require(_is_iso8601(self.validation_window_start), "validation_window_start must be ISO-8601")
        _require(_is_iso8601(self.validation_window_end), "validation_window_end must be ISO-8601")
        _require(self.validation_window_start <= self.validation_window_end, "validation window start must be <= end")
        evidence_set = set(evidence_ids)
        _require(all(ref in evidence_set for ref in self.finding_refs), "realization references unknown evidence")


@dataclass(frozen=True)
class RealizationObservedCost:
    currency: str
    dbu_cost: Optional[float]
    aws_cost: Optional[float]
    total_cost: Optional[float]

    def validate(self) -> None:
        _validate_nonnegative(self.dbu_cost, "realization observed dbu_cost")
        _validate_nonnegative(self.aws_cost, "realization observed aws_cost")
        _validate_nonnegative(self.total_cost, "realization observed total_cost")
        if self.dbu_cost is not None and self.aws_cost is not None and self.total_cost is not None:
            _require(abs((self.dbu_cost + self.aws_cost) - self.total_cost) <= 0.02, "realization total_cost must equal DBU + AWS")


@dataclass(frozen=True)
class RealizedSavings:
    currency: str
    dbu_savings: Optional[float]
    aws_savings: Optional[float]
    total_savings: Optional[float]
    annualized_total_savings: Optional[float]

    def validate(self) -> None:
        if self.dbu_savings is not None and self.aws_savings is not None and self.total_savings is not None:
            _require(abs((self.dbu_savings + self.aws_savings) - self.total_savings) <= 0.02, "realized total_savings must equal DBU + AWS")


@dataclass(frozen=True)
class RealizationOutcome:
    status: RealizationOutcomeStatus
    recommendation_accuracy: Optional[float]
    rollback_required: bool
    learning_codes: List[str]

    def validate(self) -> None:
        if self.recommendation_accuracy is not None:
            _require(0 <= self.recommendation_accuracy <= 1, "recommendation_accuracy must be in [0,1]")


@dataclass(frozen=True)
class RealizationRecord:
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
    measured_candidates: int
    successful_candidates: int
    rolled_back_candidates: int
    currency: str
    realized_savings_to_date: float
    annualized_realized_savings: float

    def validate(self) -> None:
        _require(self.measured_candidates >= 0, "measured_candidates must be >= 0")
        _require(0 <= self.successful_candidates <= self.measured_candidates, "successful_candidates out of range")
        _require(0 <= self.rolled_back_candidates <= self.measured_candidates, "rolled_back_candidates out of range")


@dataclass(frozen=True)
class Realization:
    status: RealizationStatus
    realization_records: List[RealizationRecord]
    aggregate: RealizationAggregate

    def validate(self, *, package_id: str, scenario_ids: Iterable[str], candidate_ids: Iterable[str], evidence_ids: Iterable[str]) -> None:
        if self.status == RealizationStatus.NOT_APPLIED:
            _require(len(self.realization_records) == 0, "NOT_APPLIED realization must have no records")
        for record in self.realization_records:
            record.validate(package_id=package_id, scenario_ids=scenario_ids, candidate_ids=candidate_ids, evidence_ids=evidence_ids)
        self.aggregate.validate()


# ---------------------------------------------------------------------------
# Provenance and top-level package
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
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


@dataclass(frozen=True)
class RecommendationPackage:
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

    def validate(self, *, verify_content_id: bool = True) -> None:
        _validate_identifier(self.recommendation_package_id, "PKG")
        _require(self.contract_version == CONTRACT_VERSION, f"contract_version must be {CONTRACT_VERSION}")
        self.warehouse.validate()
        self.current_config.validate()
        self.workload_profile.validate()
        self.cost_baseline.validate()
        self.assessment.validate()
        self.provenance.validate()

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
                decision_graph_version=self.provenance.decision_graph_version,
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
                decision_graph_version=self.provenance.decision_graph_version,
            )
            _require(scenario.scenario_id == expected_scenario_id, f"scenario id mismatch: expected {expected_scenario_id}")

        self.recommended_plan.validate(scenario_ids=scenario_ids)
        recommended = [x for x in self.scenarios if x.status == ScenarioStatus.RECOMMENDED]
        if self.recommended_plan.scenario_id is None:
            _require(len(recommended) == 0, "scenario marked RECOMMENDED but recommended_plan has no scenario_id")
        else:
            _require(len(recommended) == 1, "exactly one scenario must be RECOMMENDED")
            _require(recommended[0].scenario_id == self.recommended_plan.scenario_id, "RECOMMENDED scenario mismatch")

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

    def to_dict(self) -> Dict[str, Any]:
        return to_primitive(self)

    def evidence_fingerprint_expected(self) -> str:
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
        }

    def expected_package_id(self) -> str:
        return content_id("PKG", self.package_hash_payload())

    def full_package_hash(self) -> str:
        return sha256_hex(self.package_hash_payload())


def make_recommendation_package_id_from_parts(
    *,
    contract_version: str,
    workspace_id: str,
    warehouse_id: str,
    current_epoch_id: str,
    current_config: Mapping[str, Any],
    evidence_fingerprint: str,
    policy_version: str,
    analyzer_version: str,
    optimizer_version: str,
    modeler_version: str,
    estimator_version: str,
    bundle_estimator_version: str,
    reviewer_version: str,
    decision_graph_version: str,
    technique_catalog_version: str,
    ordered_candidate_ids: Sequence[str],
    ordered_scenario_ids: Sequence[str],
) -> str:
    payload = {
        "contract_version": contract_version,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "current_epoch_id": current_epoch_id,
        "current_config": dict(current_config),
        "evidence_fingerprint": evidence_fingerprint,
        "policy_version": policy_version,
        "analyzer_version": analyzer_version,
        "optimizer_version": optimizer_version,
        "modeler_version": modeler_version,
        "estimator_version": estimator_version,
        "bundle_estimator_version": bundle_estimator_version,
        "reviewer_version": reviewer_version,
        "decision_graph_version": decision_graph_version,
        "technique_catalog_version": technique_catalog_version,
        "ordered_candidate_ids": list(ordered_candidate_ids),
        "ordered_scenario_ids": list(ordered_scenario_ids),
    }
    return content_id("PKG", payload)
