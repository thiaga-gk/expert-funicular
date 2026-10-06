"""Canonical Realizer Contract v1.0.0.

The RecommendationPackage (PKG-*) remains immutable.  This module defines the
separate realization lineage (RPKG-*), append-only ledger envelope (RLE-*), and
derived snapshot (RSNAP-*).  Domain calculations live in ``realizer.py``;
this module owns stable public types and semantic validation only.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional

CONTRACT_VERSION = "1.0.0"
LEDGER_CONTRACT_VERSION = "1.0.0"
HASH_HEX_LENGTH = 24


class IntentState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class SourcePackageDisposition(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class ImplementationConformance(str, Enum):
    EXACT = "EXACT"
    MODIFIED = "MODIFIED"
    NOT_APPLIED = "NOT_APPLIED"
    CONTRARY = "CONTRARY"
    UNRELATED = "UNRELATED"


class ApplicationState(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    DRIFTED = "DRIFTED"


class PlanProgressState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class ValidationState(str, Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class RealizationState(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    VALIDATING = "VALIDATING"
    REALIZED = "REALIZED"
    UNDER_REALIZED = "UNDER_REALIZED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MONITORING = "MONITORING"
    ROLLED_BACK = "ROLLED_BACK"
    DRIFTED = "DRIFTED"
    INVALIDATED = "INVALIDATED"
    REGENERATING = "REGENERATING"
    BLOCKED = "BLOCKED"


class MaturityState(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    COLLECTING = "COLLECTING"
    PROVISIONAL = "PROVISIONAL"
    REPRESENTATIVE = "REPRESENTATIVE"
    MATURE = "MATURE"


class OriginAttribution(str, Enum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"
    INDEPENDENT = "INDEPENDENT"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


class AttributionState(str, Enum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    PARTIAL_ATTRIBUTION = "PARTIAL_ATTRIBUTION"
    CORRELATED_ONLY = "CORRELATED_ONLY"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


class AttributionClaimStatus(str, Enum):
    CLAIMABLE = "CLAIMABLE"
    OBSERVABLE_ONLY = "OBSERVABLE_ONLY"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


class EvidenceQuality(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"


class RegenerationStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"


class RecognitionStatus(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class CompatibilityStatus(str, Enum):
    LOSSLESS = "LOSSLESS"
    LOSSY = "LOSSY"
    UNREPRESENTABLE = "UNREPRESENTABLE"


class EventType(str, Enum):
    REALIZATION_CREATED = "REALIZATION_CREATED"
    INTENT_RECORDED = "INTENT_RECORDED"
    APPLICATION_OBSERVED = "APPLICATION_OBSERVED"
    MODIFIED_APPLICATION_OBSERVED = "MODIFIED_APPLICATION_OBSERVED"
    INDEPENDENT_CHANGE_OBSERVED = "INDEPENDENT_CHANGE_OBSERVED"
    VALIDATION_READY = "VALIDATION_READY"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    ECONOMIC_MEASUREMENT_RECORDED = "ECONOMIC_MEASUREMENT_RECORDED"
    COUNTERFACTUAL_RECORDED = "COUNTERFACTUAL_RECORDED"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    INVALIDATION_RECORDED = "INVALIDATION_RECORDED"
    ROLLBACK_REQUESTED = "ROLLBACK_REQUESTED"
    ROLLBACK_OBSERVED = "ROLLBACK_OBSERVED"
    SOURCE_PACKAGE_SUPERSEDED = "SOURCE_PACKAGE_SUPERSEDED"
    SOURCE_PACKAGE_EXPIRED = "SOURCE_PACKAGE_EXPIRED"
    BILLING_CORRECTION_RECORDED = "BILLING_CORRECTION_RECORDED"
    FINANCIAL_BASIS_CHANGED = "FINANCIAL_BASIS_CHANGED"
    SOURCE_QUALITY_CHANGED = "SOURCE_QUALITY_CHANGED"
    REGENERATION_REQUESTED = "REGENERATION_REQUESTED"
    REPLACEMENT_PACKAGE_LINKED = "REPLACEMENT_PACKAGE_LINKED"


def _primitive(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {f.name: _primitive(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(k): _primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(v) for v in value]
    return value


def canonical_decimal(value: Decimal | str | int | float) -> str:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite decimal")
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def canonical_json(value: Any) -> str:
    return json.dumps(_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest().upper()


def content_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{sha256_hex(payload)[:HASH_HEX_LENGTH]}"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _validate_id(value: Optional[str], prefix: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    _require(isinstance(value, str), f"{prefix} id required")
    _require(value.startswith(prefix + "-"), f"{value!r} must start with {prefix}-")
    suffix = value[len(prefix) + 1 :]
    _require(len(suffix) == HASH_HEX_LENGTH, f"{value!r} must have {HASH_HEX_LENGTH} hex chars")
    _require(all(c in "0123456789ABCDEF" for c in suffix), f"{value!r} must use uppercase hex")


@dataclass(frozen=True)
class RealizationLedgerEntry:
    ledger_entry_id: str
    ledger_contract_version: str
    realization_package_id: str
    recommendation_package_id: str
    event_type: EventType
    effective_at_utc: str
    recorded_at_utc: str
    workspace_id: str
    warehouse_id: str
    application_episode_id: Optional[str]
    validation_episode_id: Optional[str]
    measurement_id: Optional[str]
    payload: Dict[str, Any]
    evidence_refs: List[str]
    caused_by_entry_id: Optional[str] = None
    corrects_entry_id: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _validate_id(self.ledger_entry_id, "RLE")
        _validate_id(self.realization_package_id, "RPKG")
        _require(self.recommendation_package_id.startswith("PKG-"), "PKG reference required")
        _require(self.ledger_contract_version == LEDGER_CONTRACT_VERSION, "ledger contract version mismatch")
        for value, label in ((self.effective_at_utc, "effective"), (self.recorded_at_utc, "recorded")):
            try:
                dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception as exc:
                raise ValueError(f"invalid {label} timestamp") from exc
        _validate_id(self.application_episode_id, "APP", nullable=True)
        _validate_id(self.validation_episode_id, "VAL", nullable=True)
        _validate_id(self.measurement_id, "MEAS", nullable=True)
        _validate_id(self.caused_by_entry_id, "RLE", nullable=True)
        _validate_id(self.corrects_entry_id, "RLE", nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return _primitive(self)


@dataclass(frozen=True)
class RealizationPackage:
    contract_version: str
    realization_package_id: str
    snapshot_id: str
    source_recommendation: Dict[str, Any]
    warehouse: Dict[str, Any]
    as_of_utc: str
    current_state: Dict[str, Any]
    application: Dict[str, Any]
    validation: Dict[str, Any]
    economics: Dict[str, Any]
    attribution: Dict[str, Any]
    maturity: Dict[str, Any]
    drift: Dict[str, Any]
    regeneration: Dict[str, Any]
    history: Dict[str, Any]
    provenance: Dict[str, Any]

    def validate(self) -> None:
        _require(self.contract_version == CONTRACT_VERSION, "realizer contract version mismatch")
        _validate_id(self.realization_package_id, "RPKG")
        _validate_id(self.snapshot_id, "RSNAP")
        pkg = self.source_recommendation.get("recommendation_package_id")
        _require(isinstance(pkg, str) and pkg.startswith("PKG-"), "source recommendation PKG required")
        _require(bool(self.warehouse.get("workspace_id")), "workspace_id required")
        _require(bool(self.warehouse.get("warehouse_id")), "warehouse_id required")
        try:
            dt.datetime.fromisoformat(self.as_of_utc.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError("invalid as_of_utc") from exc
        checkpoint = self.history.get("ledger_checkpoint_hash")
        _require(isinstance(checkpoint, str) and checkpoint.startswith("SHA256:"), "ledger checkpoint hash required")

    def to_dict(self) -> Dict[str, Any]:
        return _primitive(self)


__all__ = [
    "CONTRACT_VERSION", "LEDGER_CONTRACT_VERSION", "HASH_HEX_LENGTH",
    "IntentState", "SourcePackageDisposition", "ImplementationConformance",
    "ApplicationState", "PlanProgressState", "ValidationState", "RealizationState",
    "MaturityState", "OriginAttribution", "AttributionState", "AttributionClaimStatus",
    "EvidenceQuality", "RegenerationStatus", "RecognitionStatus", "CompatibilityStatus",
    "EventType", "RealizationLedgerEntry", "RealizationPackage", "canonical_decimal",
    "canonical_json", "sha256_hex", "content_id",
]
