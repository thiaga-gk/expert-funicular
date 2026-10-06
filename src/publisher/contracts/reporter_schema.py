"""Build the locked Reporter Contract JSON Schema (version: CONTRACT_VERSION).

The generated file is committed under schema/recommendation-package.schema.json and
is checked byte-for-byte in tests so the Python schema source and artifact cannot drift.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

from .reporter_contract import (
    CONTRACT_VERSION,
    COST_PROFILE_GROUPS,
    ActivityPattern,
    AssessmentStatus,
    BoundName,
    CheckStatus,
    CompatibilityMode,
    CompatibilityStatus,
    ConcurrencyPattern,
    ConfidenceLevel,
    CostAvailability,
    CarriedForwardDisposition,
    CostBasis,
    CostProfileName,
    CostProfileSource,
    DependencyType,
    EstimationStatus,
    EvidenceQualityStatus,
    MatchOutcome,
    MatchPolicy,
    MeasurementStatus,
    ExecutionState,
    Objective,
    PhotonStatus,
    PlanStepType,
    PricingBasis,
    RealizationOutcomeStatus,
    REEVALUATION_BLOCKING_CODES,
    SCOPE_SCENARIO_KNOBS,
    ReevaluationStatus,
    RealizationStatus,
    RecommendationStatus,
    ScenarioStatus,
    ScenarioType,
    TunableKnob,
    ValidationOutcome,
    ValidationType,
    WorkloadLatencyClass,
)


def enum_values(enum_cls: Any) -> list[str]:
    """Values of an Enum class in declaration order."""
    return [x.value for x in enum_cls]


def obj(properties: Dict[str, Any], required: Iterable[str] | None = None, *, additional: bool = False) -> Dict[str, Any]:
    """Closed object schema: every property required unless ``required`` is given, additional properties rejected unless ``additional``."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required if required is not None else properties.keys()),
        "additionalProperties": additional,
    }


def nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Allow null in addition to ``schema`` (the contract represents unknown as an explicit null)."""
    return {"anyOf": [schema, {"type": "null"}]}


def ref(name: str) -> Dict[str, Any]:
    """JSON Schema reference to a definition under $defs."""
    return {"$ref": f"#/$defs/{name}"}


def arr(items: Dict[str, Any], *, unique: bool = False, min_items: int | None = None) -> Dict[str, Any]:
    """Array schema with optional uniqueItems and minItems."""
    out: Dict[str, Any] = {"type": "array", "items": items}
    if unique:
        out["uniqueItems"] = True
    if min_items is not None:
        out["minItems"] = min_items
    return out


def enum_schema(enum_cls: Any) -> Dict[str, Any]:
    """String schema restricted to an Enum class's values, so the schema cannot drift from the Python enum."""
    return {"type": "string", "enum": enum_values(enum_cls)}


def id_schema(prefix: str) -> Dict[str, Any]:
    """String schema for a content ID: PREFIX- followed by 24 uppercase hex characters."""
    return {"type": "string", "pattern": rf"^{prefix}-[A-F0-9]{{24}}$"}


def config_value_schema() -> Dict[str, Any]:
    """Schema for a configuration value of any knob (string, integer, number, boolean or null)."""
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
        ]
    }


def build_schema() -> Dict[str, Any]:
    """Build the Draft 2020-12 JSON Schema of RecommendationPackage as a dict.

    Enums, ID patterns, CONTRACT_VERSION and re-evaluation codes come from reporter_contract so the two agree. Objects are closed and all keys
    required; unknown or unavailable values are explicit nulls. Cross-field rules live in the semantic validators, not here.
    """
    defs: Dict[str, Any] = {}

    defs["Confidence"] = obj({
        "score": nullable({"type": "number", "minimum": 0, "maximum": 1}),
        "level": enum_schema(ConfidenceLevel),
        "components": {
            "type": "object",
            "additionalProperties": nullable({"type": "number", "minimum": 0, "maximum": 1}),
        },
        "method": nullable({"type": "string", "minLength": 1}),
    })

    defs["Range3"] = obj({
        "low": {"type": "number"},
        "base": {"type": "number"},
        "high": {"type": "number"},
    })

    defs["CostBreakdown"] = obj({
        "dbu_cost": nullable({"type": "number", "minimum": 0}),
        "aws_cost": nullable({"type": "number", "minimum": 0}),
        "total_cost": nullable({"type": "number", "minimum": 0}),
        "availability": enum_schema(CostAvailability),
        "dbu_basis": enum_schema(CostBasis),
        "aws_basis": enum_schema(CostBasis),
    })

    defs["ComponentRanges"] = obj({
        "dbu": nullable(ref("Range3")),
        "aws": nullable(ref("Range3")),
        "total": nullable(ref("Range3")),
    })

    defs["CurrentEpoch"] = obj({
        "epoch_id": id_schema("EPOCH"),
        "start_time": {"type": "string", "format": "date-time"},
        "age_days": {"type": "integer", "minimum": 0},
        "is_current": {"const": True},
    })

    defs["WarehouseIdentity"] = obj({
        "workspace_id": {"type": "string", "minLength": 1},
        "warehouse_id": {"type": "string", "minLength": 1},
        "warehouse_name": {"type": "string", "minLength": 1},
        "owner": nullable({"type": "string", "minLength": 1}),
        "region": {"type": "string", "minLength": 1},
        "current_epoch": ref("CurrentEpoch"),
    })

    defs["PhotonConfigObservation"] = obj({
        "status": enum_schema(PhotonStatus),
        "evidence_source": {"type": "string", "minLength": 1},
        "confidence": ref("Confidence"),
        "enabled_usage_pct": nullable({"type": "number", "minimum": 0, "maximum": 100}),
        "known_feature_coverage_pct": nullable({"type": "number", "minimum": 0, "maximum": 100}),
        "freshness_days": nullable({"type": "integer", "minimum": 0}),
    })

    defs["CurrentConfig"] = obj({
        "warehouse_type": {"type": "string", "minLength": 1},
        "warehouse_size": {"type": "string", "minLength": 1},
        "min_clusters": {"type": "integer", "minimum": 1, "maximum": 30},
        "max_clusters": {"type": "integer", "minimum": 1, "maximum": 40},
        "auto_stop_minutes": {"type": "integer", "minimum": 0},
        "warehouse_channel": {"type": "string", "minLength": 1},
        "photon": ref("PhotonConfigObservation"),
    })

    defs["QuerySourceStat"] = obj({
        "query_count": {"type": "integer", "minimum": 0},
        "query_pct": {"type": "number", "minimum": 0, "maximum": 100},
    })
    defs["ClientApplicationStat"] = obj({"query_count": {"type": "integer", "minimum": 0}})
    defs["WorkloadActivity"] = obj({
        "queries_30d": {"type": "integer", "minimum": 0},
        "active_days_30d": {"type": "integer", "minimum": 0, "maximum": 30},
        "queries_per_active_day": {"type": "number", "minimum": 0},
    })
    defs["WorkloadCharacteristics"] = obj({
        "latency_class": enum_schema(WorkloadLatencyClass),
        "concurrency_pattern": enum_schema(ConcurrencyPattern),
        "activity_pattern": enum_schema(ActivityPattern),
    })
    defs["WorkloadProfile"] = obj({
        "primary_query_source": {"type": "string", "minLength": 1},
        "query_sources": {"type": "object", "additionalProperties": ref("QuerySourceStat")},
        "client_applications": {"type": "object", "additionalProperties": ref("ClientApplicationStat")},
        "activity": ref("WorkloadActivity"),
        "workload_characteristics": ref("WorkloadCharacteristics"),
        "multi_label": {"const": True},
    })

    defs["ForecastInfo"] = obj({
        "model": {"type": "string", "minLength": 1},
        "confidence": ref("Confidence"),
        "assumptions": arr({"type": "string"}, unique=True),
    })
    defs["CostBaseline"] = obj({
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "actual_365d": ref("CostBreakdown"),
        "forward_365d": ref("CostBreakdown"),
        "forecast": ref("ForecastInfo"),
    })

    defs["AssessmentSummary"] = obj({
        "candidate_count": {"type": "integer", "minimum": 0},
        "actionable_count": {"type": "integer", "minimum": 0},
        "test_required_count": {"type": "integer", "minimum": 0},
        "review_count": {"type": "integer", "minimum": 0},
        "blocked_count": {"type": "integer", "minimum": 0},
        "no_change_count": {"type": "integer", "minimum": 0},
        "not_applicable_count": {"type": "integer", "minimum": 0},
        "insufficient_evidence_count": {"type": "integer", "minimum": 0},
    })
    defs["EvidenceQuality"] = obj({
        "status": enum_schema(EvidenceQualityStatus),
        "confidence": ref("Confidence"),
        "query_sample_sufficient": {"type": "boolean"},
        "cost_history_sufficient": {"type": "boolean"},
        "epoch_mature": {"type": "boolean"},
        "source_coverage_pct": {"type": "number", "minimum": 0, "maximum": 100},
    })
    defs["Assessment"] = obj({
        "overall_status": enum_schema(AssessmentStatus),
        "summary": ref("AssessmentSummary"),
        "evidence_quality": ref("EvidenceQuality"),
    })

    defs["EvidenceRecord"] = obj({
        "evidence_id": id_schema("EVID"),
        "metric": {"type": "string", "minLength": 1},
        "value": {},
        "unit": {"type": "string", "minLength": 1},
        "source": {"type": "string", "minLength": 1},
        "source_field": {"type": "string", "minLength": 1},
        "epoch_scope": {"type": "string", "minLength": 1},
        "observation_window": {"type": "string", "minLength": 1},
        "threshold": {},
        "comparator": {"type": "string"},
        "interpretation": {"type": "string", "minLength": 1},
    })

    defs["DependencyRef"] = obj({
        "dependency_type": enum_schema(DependencyType),
        "ref_id": {"type": "string", "minLength": 1},
        "condition": nullable({"type": "string", "minLength": 1}),
    })

    defs["IndependentEconomics"] = obj({
        "is_additive": {"const": False},
        "estimation_status": enum_schema(EstimationStatus),
        "baseline_365d": ref("CostBreakdown"),
        "estimated_cost_365d": ref("ComponentRanges"),
        "savings_365d": ref("ComponentRanges"),
    })

    defs["CandidateRecommendation"] = obj({
        "candidate_id": id_schema("CAND"),
        "technique_id": {"type": "string", "pattern": "^WH-[A-Z0-9-]+-[0-9]{3}$"},
        "knob": enum_schema(TunableKnob),
        "status": enum_schema(RecommendationStatus),
        "current_value": config_value_schema(),
        "recommended_value": config_value_schema(),
        "objectives": arr(enum_schema(Objective), unique=True, min_items=1),
        "rationale": {"type": "string", "minLength": 1},
        "evidence_refs": arr(id_schema("EVID"), unique=True),
        "blockers": arr({"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}, unique=True),
        "dependencies": arr(ref("DependencyRef")),
        "invalidates": arr(enum_schema(TunableKnob), unique=True),
        "confidence": ref("Confidence"),
        "independent_economics": ref("IndependentEconomics"),
    })

    defs["CompatibilityCheck"] = obj({
        "check": {"type": "string", "minLength": 1},
        "status": enum_schema(CheckStatus),
        "requires_hitl": {"type": "boolean"},
        "evidence_refs": arr(id_schema("EVID"), unique=True),
        "details": nullable({"type": "string"}),
    })
    defs["Compatibility"] = obj({
        "status": enum_schema(CompatibilityStatus),
        "mode": enum_schema(CompatibilityMode),
        "checks": arr(ref("CompatibilityCheck")),
    })

    defs["ValidationGate"] = obj({
        "validation_type": enum_schema(ValidationType),
        "success_metrics": arr({"type": "string", "minLength": 1}, unique=True),
        "remeasure_days": nullable({"type": "integer", "minimum": 1, "maximum": 90}),
        "action_after": nullable({"type": "string", "minLength": 1}),
    })
    defs["StepProjection"] = obj({
        "pricing_basis": {"type": "string", "enum": [PricingBasis.AUTHORITATIVE.value, PricingBasis.CONTINGENT_PENDING_REEVALUATION.value]},
        "running_savings_365d": ref("ComponentRanges"),
        "step_savings_365d": nullable(ref("ComponentRanges")),
    })
    defs["PlanStep"] = obj({
        "step": {"type": "integer", "minimum": 1},
        "step_type": enum_schema(PlanStepType),
        "candidate_id": nullable(id_schema("CAND")),
        "technique_id": nullable({"type": "string", "pattern": "^WH-[A-Z0-9-]+-[0-9]{3}$"}),
        "knob": nullable(enum_schema(TunableKnob)),
        "action": nullable({"type": "string", "minLength": 1}),
        "current_value": config_value_schema(),
        "recommended_value": config_value_schema(),
        "execution_state": enum_schema(ExecutionState),
        "confidence": ref("Confidence"),
        "validation": ref("ValidationGate"),
        "dependencies": arr(ref("DependencyRef")),
        "invalidates": arr(enum_schema(TunableKnob), unique=True),
        "projected_economics": nullable(ref("StepProjection")),
    })

    defs["ScenarioEconomics"] = obj({
        "estimation_status": enum_schema(EstimationStatus),
        "baseline_365d": ref("CostBreakdown"),
        "estimated_cost_365d": ref("ComponentRanges"),
        "savings_365d": ref("ComponentRanges"),
        "method": {"type": "string", "minLength": 1},
        "is_additive": {"const": False},
        "contingent_estimated_cost_365d": nullable(ref("ComponentRanges")),
        "contingent_savings_365d": nullable(ref("ComponentRanges")),
    })
    defs["Scenario"] = obj({
        "scenario_id": id_schema("SCN"),
        "scenario_name": {"type": "string", "minLength": 1},
        "scenario_type": enum_schema(ScenarioType),
        "status": enum_schema(ScenarioStatus),
        "candidate_refs": arr(id_schema("CAND"), unique=True),
        "evidence_refs": arr(id_schema("EVID"), unique=True),
        "confidence": ref("Confidence"),
        "compatibility": ref("Compatibility"),
        "portfolio_economics": ref("ScenarioEconomics"),
        "execution_plan": arr(ref("PlanStep")),
    })

    defs["ReevaluationWindow"] = obj({
        "status": enum_schema(ReevaluationStatus),
        "earliest_reevaluation_time": {"type": "string", "format": "date-time"},
        "blocking_codes": arr({"type": "string", "enum": list(REEVALUATION_BLOCKING_CODES)}, unique=True, min_items=1),
        "method": {"type": "string", "minLength": 1},
    })
    defs["RecommendedPlan"] = obj({
        "scenario_id": nullable(id_schema("SCN")),
        "selection_reason_code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
        "next_action": {"type": "string", "minLength": 1},
        "requires_hitl": {"type": "boolean"},
        "decision_summary": {"type": "string", "minLength": 1},
        "reevaluation": nullable(ref("ReevaluationWindow")),
    })
    defs["ConfigChange"] = obj({
        "knob": enum_schema(TunableKnob),
        "previous_value": {},
        "current_value": {},
    })
    defs["ObservedConfigChange"] = obj({
        "previous_epoch_id": id_schema("EPOCH"),
        "previous_epoch_start": {"type": "string", "format": "date-time"},
        "changed_at": {"type": "string", "format": "date-time"},
        "changes": arr(ref("ConfigChange"), min_items=1),
        "measurement_status": enum_schema(MeasurementStatus),
    })
    defs["CarriedForwardStep"] = obj({
        "order": {"type": "integer", "minimum": 1},
        "knob": enum_schema(TunableKnob),
        "technique_id": {"type": "string", "minLength": 1},
        "previous_value": {},
        "recommended_value": {},
        "disposition": enum_schema(CarriedForwardDisposition),
        "reason_code": nullable({"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}),
        "rebased_running_savings_365d": nullable(ref("ComponentRanges")),
    })
    defs["CarriedForwardPlan"] = obj({
        "source_epoch_id": id_schema("EPOCH"),
        "as_of": {"type": "string", "format": "date-time"},
        "match_policy": enum_schema(MatchPolicy),
        "match_outcome": enum_schema(MatchOutcome),
        "match_reason_code": nullable({"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}),
        "replay_fingerprint": {"type": "string", "pattern": "^SHA256:[A-F0-9]{64}$"},
        "steps": arr(ref("CarriedForwardStep")),
        "remaining_upside_365d": nullable(ref("ComponentRanges")),
        "pricing_basis": {"const": "CONTINGENT_CARRIED_FORWARD"},
    })

    defs["EconomicsPoint"] = obj({
        "dbu": nullable({"type": "number"}),
        "aws": nullable({"type": "number"}),
        "total": nullable({"type": "number"}),
    })
    defs["ScenarioEnvelopePoint"] = obj({
        "scenario_id": id_schema("SCN"),
        "bound": enum_schema(BoundName),
        "savings_365d": ref("EconomicsPoint"),
        "estimated_cost_365d": ref("EconomicsPoint"),
    })
    defs["ExcludedScenario"] = obj({
        "scenario_id": id_schema("SCN"),
        "reason_code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
    })
    defs["ScenarioEnvelope"] = obj({
        "minimum_savings": nullable(ref("ScenarioEnvelopePoint")),
        "recommended_base": nullable(ref("ScenarioEnvelopePoint")),
        "maximum_savings": nullable(ref("ScenarioEnvelopePoint")),
    })
    defs["PackageEconomicsSemantics"] = obj({
        "method": {"const": "SCENARIO_ENVELOPE"},
        "is_additive": {"const": False},
        "is_probability_interval": {"const": False},
    })
    defs["PackageEconomics"] = obj({
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "baseline_365d": ref("CostBreakdown"),
        "estimated_savings_365d": nullable(ref("Range3")),
        "estimated_cost_365d": nullable(ref("Range3")),
        "scenario_envelope": ref("ScenarioEnvelope"),
        "included_scenario_ids": arr(id_schema("SCN"), unique=True),
        "excluded_scenarios": arr(ref("ExcludedScenario")),
        "semantics": ref("PackageEconomicsSemantics"),
    })

    defs["AppliedChange"] = obj({
        "applied_at": {"type": "string", "format": "date-time"},
        "applied_by": {"type": "string", "minLength": 1},
        "previous_config": {"type": "object", "additionalProperties": config_value_schema()},
        "applied_config": {"type": "object", "additionalProperties": config_value_schema()},
    })
    defs["RealizationValidation"] = obj({
        "validation_window_start": {"type": "string", "format": "date-time"},
        "validation_window_end": {"type": "string", "format": "date-time"},
        "validation_status": enum_schema(ValidationOutcome),
        "performance_guardrails_passed": nullable({"type": "boolean"}),
        "finding_refs": arr(id_schema("EVID"), unique=True),
        "notes": arr({"type": "string"}),
    })
    defs["RealizationObservedCost"] = obj({
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "dbu_cost": nullable({"type": "number", "minimum": 0}),
        "aws_cost": nullable({"type": "number", "minimum": 0}),
        "total_cost": nullable({"type": "number", "minimum": 0}),
    })
    defs["RealizedSavings"] = obj({
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "dbu_savings": nullable({"type": "number"}),
        "aws_savings": nullable({"type": "number"}),
        "total_savings": nullable({"type": "number"}),
        "annualized_total_savings": nullable({"type": "number"}),
    })
    defs["RealizationOutcome"] = obj({
        "status": enum_schema(RealizationOutcomeStatus),
        "recommendation_accuracy": nullable({"type": "number", "minimum": 0, "maximum": 1}),
        "rollback_required": {"type": "boolean"},
        "learning_codes": arr({"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}, unique=True),
    })
    defs["RealizationRecord"] = obj({
        "realization_id": id_schema("REAL"),
        "recommendation_package_id": id_schema("PKG"),
        "scenario_id": id_schema("SCN"),
        "candidate_id": nullable(id_schema("CAND")),
        "technique_id": nullable({"type": "string", "pattern": "^WH-[A-Z0-9-]+-[0-9]{3}$"}),
        "applied": ref("AppliedChange"),
        "validation": ref("RealizationValidation"),
        "observed_cost": ref("RealizationObservedCost"),
        "realized_savings": ref("RealizedSavings"),
        "outcome": ref("RealizationOutcome"),
    })
    defs["RealizationAggregate"] = obj({
        "measured_candidates": {"type": "integer", "minimum": 0},
        "successful_candidates": {"type": "integer", "minimum": 0},
        "rolled_back_candidates": {"type": "integer", "minimum": 0},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "realized_savings_to_date": {"type": "number"},
        "annualized_realized_savings": {"type": "number"},
    })
    defs["Realization"] = obj({
        "status": enum_schema(RealizationStatus),
        "realization_records": arr(ref("RealizationRecord")),
        "aggregate": ref("RealizationAggregate"),
    })

    in_place_knobs = [k.value for k in SCOPE_SCENARIO_KNOBS[ScenarioType.IN_PLACE_OPTIMIZATION]]
    defs["ScopedScenario"] = obj({
        "scenario_type": {"type": "string", "enum": [t.value for t in SCOPE_SCENARIO_KNOBS]},
        "knobs": arr({"type": "string", "enum": sorted({k.value for knobs in SCOPE_SCENARIO_KNOBS.values() for k in knobs})}, unique=True, min_items=1),
    })
    defs["SafetyInclusion"] = obj({
        "candidate_id": id_schema("CAND"),
        "knob": {"type": "string", "enum": in_place_knobs},
        "reason_code": {"const": "CANARY_GUARDRAIL_BREACHED"},
    })
    defs["RunScope"] = obj({
        "scenarios": arr(ref("ScopedScenario"), min_items=1),
        "safety_inclusions": arr(ref("SafetyInclusion")),
    })

    defs["Provenance"] = obj({
        "contract_version": {"const": CONTRACT_VERSION},
        "policy_version": {"type": "string", "minLength": 1},
        "analyzer_version": {"type": "string", "minLength": 1},
        "optimizer_version": {"type": "string", "minLength": 1},
        "modeler_version": {"type": "string", "minLength": 1},
        "estimator_version": {"type": "string", "minLength": 1},
        "bundle_estimator_version": {"type": "string", "minLength": 1},
        "reviewer_version": {"type": "string", "minLength": 1},
        "decision_graph_version": {"type": "string", "minLength": 1},
        "technique_catalog_version": {"type": "string", "minLength": 1},
        "evidence_fingerprint": {"type": "string", "pattern": "^SHA256:[A-F0-9]{64}$"},
        "source_tables": arr({"type": "string", "minLength": 1}, unique=True, min_items=1),
        "source_regions": arr({"type": "string", "minLength": 1}, unique=True, min_items=1),
        "generated_at": {"type": "string", "format": "date-time"},
        "package_hash_full_sha256": nullable({"type": "string", "pattern": "^[A-F0-9]{64}$"}),
    })

    defs["CostProfile"] = obj({
        "name": {"enum": enum_values(CostProfileName)},
        "source": {"enum": enum_values(CostProfileSource)},
        "source_detail": nullable({"type": "string", "minLength": 1}),
        "applied_thresholds": obj({
            group: obj({f: {"type": "number"} for f in fields}) for group, fields in COST_PROFILE_GROUPS.items()
        }),
    })

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:databricks-sql-warehouse-optimizer:recommendation-package:{CONTRACT_VERSION}",
        "title": "Databricks SQL Warehouse Optimization Recommendation Package",
        "description": "Locked per-warehouse reporter contract. Candidate economics are isolated/non-additive; scenario economics are stateful; package economics is a scenario opportunity envelope.",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommendation_package_id": id_schema("PKG"),
            "contract_version": {"const": CONTRACT_VERSION},
            "warehouse": ref("WarehouseIdentity"),
            "current_config": ref("CurrentConfig"),
            "workload_profile": ref("WorkloadProfile"),
            "cost_baseline": ref("CostBaseline"),
            "assessment": ref("Assessment"),
            "evidence": {
                "type": "object",
                "propertyNames": {"pattern": "^EVID-[A-F0-9]{24}$"},
                "additionalProperties": ref("EvidenceRecord"),
            },
            "candidates": arr(ref("CandidateRecommendation")),
            "scenarios": arr(ref("Scenario")),
            "recommended_plan": ref("RecommendedPlan"),
            "package_economics": ref("PackageEconomics"),
            "realization": ref("Realization"),
            "provenance": ref("Provenance"),
            "cost_profile": ref("CostProfile"),
            "observed_change": nullable(ref("ObservedConfigChange")),
            "carried_forward_plan": nullable(ref("CarriedForwardPlan")),
            "run_scope": nullable(ref("RunScope")),
        },
        "required": [
            "recommendation_package_id", "contract_version", "warehouse", "current_config",
            "workload_profile", "cost_baseline", "assessment", "evidence", "candidates",
            "scenarios", "recommended_plan", "package_economics", "realization", "provenance",
            "cost_profile", "observed_change", "carried_forward_plan", "run_scope",
        ],
        "$defs": defs,
    }
    return schema


def write_schema(path: str | Path) -> None:
    """Write the schema as key-sorted, indented JSON (the committed artifact is checked byte-for-byte against it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("output", nargs="?", default="schema/recommendation-package.schema.json")
    args = p.parse_args()
    write_schema(args.output)
