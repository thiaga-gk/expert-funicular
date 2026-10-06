"""Canonical Realizer Contract v1.0.0.

The RecommendationPackage (PKG-*) remains immutable.  This module defines the
separate realization lineage (RPKG-*), append-only ledger envelope (RLE-*), and
derived snapshot (RSNAP-*), plus the deterministic public ID builders.  Domain
calculations live in the engine and its capabilities; this module owns stable
public types, canonical serialization/IDs and semantic validation only.

It presents the complete v1.0.0 surface: the observation-side enums
(``realizer.contracts.analysis``) and the reconciliation enums
(``realizer.contracts.reconciliation``) are defined with their group and
re-exported here, so ``__all__`` is the single-file contract's ``__all__``
plus the ID builders.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from realizer.contracts.analysis import EvidenceQuality, IntentState, OriginAttribution
from realizer.contracts.reconciliation import (
    ApplicationState,
    ImplementationConformance,
    PlanProgressState,
    RegenerationStatus,
    SourcePackageDisposition,
)

CONTRACT_VERSION = "1.0.0"
LEDGER_CONTRACT_VERSION = "1.0.0"
HASH_HEX_LENGTH = 24


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


class AttributionState(str, Enum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    PARTIAL_ATTRIBUTION = "PARTIAL_ATTRIBUTION"
    CORRELATED_ONLY = "CORRELATED_ONLY"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


class AttributionClaimStatus(str, Enum):
    CLAIMABLE = "CLAIMABLE"
    OBSERVABLE_ONLY = "OBSERVABLE_ONLY"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


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



# ---------------------------------------------------------------------------
# Public ID builders (moved verbatim from the single-file engine; content dicts unchanged)
# ---------------------------------------------------------------------------


def make_rpkg_id(package_id: str) -> str:
    return content_id("RPKG", {
        "kind": "REALIZATION_PACKAGE",
        "contract_major": CONTRACT_VERSION.split(".")[0],
        "recommendation_package_id": package_id,
    })


def make_evidence_id(kind: str, package: Mapping[str, Any], observation: Mapping[str, Any], payload: Any) -> str:
    warehouse = package["warehouse"]
    return content_id("REVID", {
        "evidence_type": kind,
        "workspace_id": warehouse["workspace_id"],
        "warehouse_id": warehouse["warehouse_id"],
        "source": observation.get("source", "NORMALIZED_OBSERVATION"),
        "window": observation.get("window") or {
            "start_utc": observation.get("window_start_utc"),
            "end_utc": observation.get("window_end_utc") or observation.get("observed_at_utc"),
        },
        "payload": payload,
    })


def make_app_id(rpkg_id: str, scenario_id: str, step: int, source_hash: str, observed_hash: str, anchor_evidence_id: str) -> str:
    return content_id("APP", {
        "rpkg": rpkg_id,
        "scenario_id": scenario_id,
        "step": step,
        "source_config_hash": source_hash,
        "observed_config_hash": observed_hash,
        "anchor_evidence_id": anchor_evidence_id,
    })


def make_val_id(app_id: str, observation: Mapping[str, Any], policy: Any) -> str:
    """VAL id; *policy* is any object with the Realizer policy's ``snapshot_id`` (PSNAP-*)."""
    return content_id("VAL", {
        "app": app_id,
        "window_start": observation.get("window_start_utc") or observation.get("applied_at_utc") or observation.get("observed_at_utc"),
        "window_end": observation.get("window_end_utc") or observation.get("observed_at_utc"),
        "policy_snapshot_id": policy.snapshot_id,
        "analyzer_evidence": observation.get("performance", {}),
    })


def make_measurement_id(app_id: str, observation: Mapping[str, Any], cf_id: str, basis: str) -> str:
    return content_id("MEAS", {
        "app": app_id,
        "window_start": observation.get("window_start_utc") or observation.get("applied_at_utc"),
        "window_end": observation.get("window_end_utc") or observation.get("observed_at_utc"),
        "economic_basis": basis,
        "counterfactual_id": cf_id,
        "correction_version": (observation.get("correction") or {}).get("version"),
    })


__all__ = [
    "CONTRACT_VERSION", "LEDGER_CONTRACT_VERSION", "HASH_HEX_LENGTH",
    "IntentState", "SourcePackageDisposition", "ImplementationConformance",
    "ApplicationState", "PlanProgressState", "ValidationState", "RealizationState",
    "MaturityState", "OriginAttribution", "AttributionState", "AttributionClaimStatus",
    "EvidenceQuality", "RegenerationStatus", "RecognitionStatus", "CompatibilityStatus",
    "EventType", "RealizationLedgerEntry", "RealizationPackage", "canonical_decimal",
    "canonical_json", "sha256_hex", "content_id",
    "make_rpkg_id", "make_evidence_id", "make_app_id", "make_val_id", "make_measurement_id",
]
