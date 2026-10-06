"""Contract adapter: maps internal candidates, scenarios and evidence onto the public Reporter JSON Contract types."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.epochs import carried_query_view, ready_billed_days
from publisher.analyzer.metrics import cost_stability
from publisher.common.hashing import canonical_json
from publisher.common.time import _iso_z, parse_datetime
from publisher.common.utils import clamp, optional_float, safe_float, safe_int
from publisher.config.defaults import SIZE_ORDER
from publisher.governor.thresholds import Thresholds
from publisher.config.versions import (
    ANALYZER_VERSION,
    BUNDLE_MODEL_VERSION,
    DECISION_VERSION,
    ESTIMATOR_VERSION,
    OPTIMIZER_VERSION,
    POLICY_VERSION,
    REVIEWER_VERSION,
)
from publisher.contracts import reporter_contract as rc
from publisher.contracts.candidate import CandidateRecommendation, Range3
from publisher.contracts.scenario import PlanStep, Scenario
from publisher.contracts.source import WarehouseEvidence
from publisher.packager.selection import _overall_status


def _scalar_config(d: Mapping[str, Any]) -> Any:
    """Return the scalar from a one-key config mapping; preserve composite mappings."""
    if len(d) == 1:
        return next(iter(d.values()))
    return dict(d)


# Contract workload-profile categories: entity sources (what ran the query) and client applications (what connected).
_ENTITY_QUERY_SOURCE_CATEGORIES = {"DASHBOARD", "JOB", "NOTEBOOK", "GENIE", "ALERT", "SQL_EDITOR", "OTHER", "UNKNOWN"}


_CLIENT_QUERY_SOURCE_CATEGORIES = {"POWER_BI", "TABLEAU", "LOOKER", "SQL_EDITOR"}


def _contract_confidence(value: Optional[Mapping[str, Any]], *, method: Optional[str] = None) -> rc.Confidence:
    """Translate internal evidence confidence to the locked reporter contract."""
    if not value or value.get("score") is None:
        return rc.Confidence(score=None, level=rc.ConfidenceLevel.UNKNOWN, components={}, method=method)
    score = clamp(safe_float(value.get("score")), 0.0, 1.0)
    level = rc.ConfidenceLevel.HIGH if score >= 0.80 else rc.ConfidenceLevel.MEDIUM if score >= 0.60 else rc.ConfidenceLevel.LOW
    components: Dict[str, Optional[float]] = {}
    for k, v in (value.get("components") or {}).items():
        components[str(k)] = None if v is None else round(clamp(safe_float(v), 0.0, 1.0), 6)
    return rc.Confidence(score=round(score, 6), level=level, components=components, method=method or value.get("method"))


def _contract_range(value: Optional[Range3]) -> Optional[rc.Range3]:
    """Internal range to contract range rounded to cents; None stays None (unknown, not zero)."""
    if value is None:
        return None
    return rc.Range3(round(float(value.low), 2), round(float(value.base), 2), round(float(value.high), 2))


def _contract_component_ranges(
    dbu: Optional[Range3], aws: Optional[Range3], total: Optional[Range3]
) -> rc.ComponentRanges:
    """DBU/AWS/total ranges for the contract; an unknown component is None, never zero."""
    return rc.ComponentRanges(_contract_range(dbu), _contract_range(aws), _contract_range(total))


def _step_projection(step: PlanStep) -> Optional[rc.StepProjection]:
    """Cumulative and own stateful savings for this step, or None when the chain could not price it."""
    if step.running_dbu_savings_range_365 is None and step.running_aws_savings_range_365 is None:
        return None
    total = step.running_estimated_savings_range_365 if step.running_aws_savings_range_365 is not None else None
    dbu_step, aws_step = step.step_dbu_savings_range_365, step.step_aws_savings_range_365
    step_total = None if dbu_step is None or aws_step is None else Range3(
        dbu_step.low + aws_step.low, dbu_step.base + aws_step.base, dbu_step.high + aws_step.high
    )
    return rc.StepProjection(
        pricing_basis=rc.PricingBasis(step.pricing_basis),
        running_savings_365d=_contract_component_ranges(
            step.running_dbu_savings_range_365, step.running_aws_savings_range_365, total
        ),
        step_savings_365d=None if dbu_step is None else _contract_component_ranges(dbu_step, aws_step, step_total),
    )


def _forward_cost_breakdown(e: WarehouseEvidence) -> rc.CostBreakdown:
    """Forward 365d baseline: DBU forecast and modeled AWS kept separate; the total exists only when both are known (availability COMPLETE/PARTIAL/UNAVAILABLE)."""
    dbu = round(e.dbu_365d_baseline_cost, 2) if e.dbu_365d_baseline_cost is not None else None
    aws = round(e.aws_baseline_cost_365, 2) if e.aws_baseline_cost_365 is not None else None
    total = round(dbu + aws, 2) if dbu is not None and aws is not None else None
    if dbu is not None and aws is not None:
        availability = rc.CostAvailability.COMPLETE
    elif dbu is not None or aws is not None:
        availability = rc.CostAvailability.PARTIAL
    else:
        availability = rc.CostAvailability.UNAVAILABLE
    aws_basis = rc.CostBasis.UNAVAILABLE if aws is None else (
        rc.CostBasis.MODELED_CURRENT_EPOCH if e.current_epoch.config.warehouse_type == "SERVERLESS" else rc.CostBasis.MODELED_AWS_LIST_PRICE
    )
    return rc.CostBreakdown(
        dbu_cost=dbu,
        aws_cost=aws,
        total_cost=total,
        availability=availability,
        dbu_basis=rc.CostBasis.FORECAST if dbu is not None else rc.CostBasis.UNAVAILABLE,
        aws_basis=aws_basis,
    )


def _actual_cost_breakdown(e: WarehouseEvidence) -> rc.CostBreakdown:
    """Historical actual is authoritative for Databricks billing; AWS stays null without CUR/effective AWS billing."""
    dbu = round(e.current_cost_365, 2) if e.current_cost_365 is not None else None
    if e.current_epoch.config.warehouse_type == "SERVERLESS" and dbu is not None:
        return rc.CostBreakdown(
            dbu_cost=dbu, aws_cost=0.0, total_cost=dbu,
            availability=rc.CostAvailability.COMPLETE,
            dbu_basis=rc.CostBasis.OBSERVED_DATABRICKS_BILLING,
            aws_basis=rc.CostBasis.MODELED_CURRENT_EPOCH,
        )
    return rc.CostBreakdown(
        dbu_cost=dbu, aws_cost=None, total_cost=None,
        availability=rc.CostAvailability.PARTIAL if dbu is not None else rc.CostAvailability.UNAVAILABLE,
        dbu_basis=rc.CostBasis.OBSERVED_DATABRICKS_BILLING if dbu is not None else rc.CostBasis.UNAVAILABLE,
        aws_basis=rc.CostBasis.UNAVAILABLE,
    )


def _forecast_confidence(e: WarehouseEvidence, t: Thresholds) -> rc.Confidence:
    """Forecast confidence from billing-history coverage and cost stability; unknown stability earns no credit."""
    coverage = clamp(safe_float(e.cost_history_days) / max(1.0, float(t.forecast_long_days)), 0.0, 1.0)
    stability = cost_stability(e.cost_cv_84)
    score = round(0.65 * coverage + 0.35 * (stability or 0.0), 6)  # unknown stability earns no credit
    return _contract_confidence(
        {"score": score, "components": {"history_coverage": coverage, "cost_stability": stability}},
        method="current_epoch_billing_cycle_evidence" if (e.forecast_metadata or {}).get("forecast_model") == "CADENCE" else "current_epoch_runrate_evidence",
    )


def _workload_profile(e: WarehouseEvidence, t: Thresholds) -> rc.WorkloadProfile:
    """Descriptive (non-decisional) workload profile from current-epoch query evidence: query-source and client mix, activity, latency class, concurrency and activity pattern. UNKNOWN below the query-sample floor."""
    ep = e.current_epoch
    q, _ = carried_query_view(ep.query)  # the same query evidence the decisions read
    profile = e.attribution.query_source if e.attribution else None
    categories = dict(profile.categories) if profile else {}
    percentages = dict(profile.percentages) if profile else {}

    query_sources: Dict[str, rc.QuerySourceStat] = {}
    for name in sorted(_ENTITY_QUERY_SOURCE_CATEGORIES):
        if name in categories:
            query_sources[name] = rc.QuerySourceStat(
                query_count=max(0, safe_int(categories.get(name))),
                query_pct=round(clamp(safe_float(percentages.get(name)), 0.0, 100.0), 6),
            )
    if not query_sources:
        query_sources["UNKNOWN"] = rc.QuerySourceStat(query_count=max(0, safe_int(q.get("queries_30"))), query_pct=100.0)

    primary = profile.primary_category if profile and profile.primary_category in query_sources else max(
        query_sources, key=lambda k: (query_sources[k].query_count, k)
    )

    clients: Dict[str, rc.ClientApplicationStat] = {}
    for name in sorted(_CLIENT_QUERY_SOURCE_CATEGORIES):
        count = safe_int(categories.get(name)) if name in categories else 0
        if count > 0:
            clients[name] = rc.ClientApplicationStat(query_count=count)

    queries_30 = max(0, safe_int(q.get("queries_30")))
    active_days = min(30, max(0, safe_int(q.get("active_days_30"))))
    per_day = round(queries_30 / active_days, 6) if active_days else 0.0

    if queries_30 < t.min_query_sample_size:
        latency_class = rc.WorkloadLatencyClass.UNKNOWN
        concurrency = rc.ConcurrencyPattern.UNKNOWN
    else:
        p95 = optional_float(q.get("p95_execution_ms_30"))
        latency_class = (
            rc.WorkloadLatencyClass.UNKNOWN if p95 is None
            else rc.WorkloadLatencyClass.INTERACTIVE if p95 <= 5_000
            else rc.WorkloadLatencyClass.BATCH if p95 >= 60_000
            else rc.WorkloadLatencyClass.MIXED
        )
        queued = optional_float(q.get("queued_query_pct_30"))
        concurrency = (
            rc.ConcurrencyPattern.UNKNOWN if queued is None
            else rc.ConcurrencyPattern.STEADY if queued <= t.queue_query_pct_safe
            else rc.ConcurrencyPattern.BURSTY if queued >= t.queue_query_pct_increase_max
            else rc.ConcurrencyPattern.MIXED
        )

    if active_days >= 25:
        activity_pattern = rc.ActivityPattern.CONTINUOUS
    elif active_days <= 8:
        activity_pattern = rc.ActivityPattern.SPORADIC
    elif primary == "JOB":
        activity_pattern = rc.ActivityPattern.SCHEDULED
    else:
        activity_pattern = rc.ActivityPattern.UNKNOWN

    return rc.WorkloadProfile(
        primary_query_source=primary,
        query_sources=query_sources,
        client_applications=clients,
        activity=rc.WorkloadActivity(queries_30d=queries_30, active_days_30d=active_days, queries_per_active_day=per_day),
        workload_characteristics=rc.WorkloadCharacteristics(
            latency_class=latency_class,
            concurrency_pattern=concurrency,
            activity_pattern=activity_pattern,
        ),
        multi_label=True,
    )


def _photon_config_observation(e: WarehouseEvidence) -> rc.PhotonConfigObservation:
    """Photon observation from current-epoch billing features. Observation-only: Photon is never a candidate, and absent evidence is UNKNOWN."""
    p = e.photon_observation
    if p is None:
        return rc.PhotonConfigObservation(
            status=rc.PhotonStatus.UNKNOWN,
            evidence_source="system.billing.usage.product_features.is_photon",
            confidence=rc.Confidence(None, rc.ConfidenceLevel.UNKNOWN, {}, "no_current_epoch_billing_feature_evidence"),
            enabled_usage_pct=None, known_feature_coverage_pct=None, freshness_days=None,
        )
    status = rc.PhotonStatus(p.status) if p.status in {x.value for x in rc.PhotonStatus} else rc.PhotonStatus.UNKNOWN
    return rc.PhotonConfigObservation(
        status=status,
        evidence_source=p.evidence_source,
        confidence=_contract_confidence(p.confidence, method="signed_current_epoch_dbu_feature_coverage"),
        enabled_usage_pct=None if p.enabled_usage_pct is None else round(float(p.enabled_usage_pct), 6),
        known_feature_coverage_pct=round(float(p.known_feature_coverage_pct), 6),
        freshness_days=p.freshness_days,
    )


def _technique_id(c: CandidateRecommendation) -> str:
    """Technique catalog ID for a candidate from its knob and the direction of the change; an unmapped knob raises (fail closed)."""
    knob = c.knob
    cur = _scalar_config(c.current_config)
    rec = _scalar_config(c.recommended_config)
    if knob == "warehouse_size":
        if cur in SIZE_ORDER and rec in SIZE_ORDER and cur != rec:
            return "WH-SIZE-DOWNSIZE-001" if SIZE_ORDER.index(rec) < SIZE_ORDER.index(cur) else "WH-SIZE-UPSIZE-001"
        return "WH-SIZE-RIGHTSIZE-001"
    if knob == "max_clusters":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-MAX-CLUSTER-REDUCE-001"
        if isinstance(cur, int) and isinstance(rec, int) and rec > cur:
            return "WH-MAX-CLUSTER-INCREASE-001"
        return "WH-MAX-CLUSTER-ASSESS-001"
    if knob == "min_clusters":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-MIN-CLUSTER-REDUCE-001"
        if isinstance(cur, int) and isinstance(rec, int) and rec > cur:
            return "WH-MIN-CLUSTER-INCREASE-001"
        return "WH-MIN-CLUSTER-ASSESS-001"
    if knob == "auto_stop_minutes":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-AUTOSTOP-REDUCE-001"
        return "WH-AUTOSTOP-ASSESS-001"
    if knob == "warehouse_type":
        if rec == "SERVERLESS" and cur != rec:
            return "WH-SERVERLESS-MIGRATE-001"
        if cur == "SERVERLESS" and rec in {"PRO", "CLASSIC"}:
            return "WH-PRO-MIGRATE-001"
        return "WH-WAREHOUSE-TYPE-ASSESS-001"
    raise ValueError(f"No technique catalog mapping for knob {knob!r}")


def _objectives(c: CandidateRecommendation) -> List[rc.Objective]:
    """Contract objectives for a candidate; migration is fixed, unknown internal objectives degrade to OPERATIONS."""
    objective = str(c.objective or "").upper()
    if c.knob == "warehouse_type":
        return [rc.Objective.MIGRATION, rc.Objective.COST_AND_PERFORMANCE, rc.Objective.OPERATIONS]
    mapping = {
        "COST": [rc.Objective.COST],
        "PERFORMANCE": [rc.Objective.PERFORMANCE],
        "PERFORMANCE_AND_COST": [rc.Objective.COST_AND_PERFORMANCE],
        "COST_AND_PERFORMANCE": [rc.Objective.COST_AND_PERFORMANCE],
        "COST_GUARDRAIL": [rc.Objective.COST_GUARDRAIL],
        "STABILITY": [rc.Objective.STABILITY],
        "RELIABILITY": [rc.Objective.RELIABILITY],
        "OPERATIONS": [rc.Objective.OPERATIONS],
        "PERFORMANCE_AND_OPERATIONS": [rc.Objective.PERFORMANCE, rc.Objective.OPERATIONS],
    }
    return mapping.get(objective, [rc.Objective.OPERATIONS])


def _recommendation_status(value: str) -> rc.RecommendationStatus:
    """Internal status string to the contract enum; an unknown status raises rather than degrading."""
    try:
        return rc.RecommendationStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported recommendation status for locked contract: {value}") from exc


def _estimation_status(c: CandidateRecommendation) -> rc.EstimationStatus:
    """How a candidate's independent economics were produced: UNAVAILABLE when nothing was priced, NO_DELTA_MODELED for no-change and guardrail changes, PRE_CANARY/PROVISIONAL for canaries, else MODELED_RANGE."""
    if c.status in {"NO_CHANGE", "NOT_APPLICABLE"}:
        return rc.EstimationStatus.NO_DELTA_MODELED
    if c.total_365d_estimated_cost_range is None and c.dbu_365d_estimated_cost_range is None:
        # Blocked, or a change whose estimate was withheld (e.g. no published Serverless DBU rate): nothing was priced.
        return rc.EstimationStatus.UNAVAILABLE
    if (c.metadata.get("bundle_effect") or {}).get("type") == "no_cost_change":
        # A guardrail/stability change (e.g. tightening max clusters) is deliberately given no priced delta.
        return rc.EstimationStatus.NO_DELTA_MODELED
    if c.knob == "warehouse_type" and c.status == "TEST_REQUIRED":
        return rc.EstimationStatus.PRE_CANARY_MODELED_RANGE
    if c.status == "TEST_REQUIRED":
        return rc.EstimationStatus.PROVISIONAL_MODELED_RANGE
    return rc.EstimationStatus.MODELED_RANGE


def _dependency_refs(values: Sequence[str]) -> List[rc.DependencyRef]:
    """Knob dependency references, de-duplicated, in input order."""
    refs = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        refs.append(rc.DependencyRef(rc.DependencyType.KNOB, value, None))
    return refs


def _tunable_knobs(values: Sequence[str]) -> List[rc.TunableKnob]:
    """Contract knobs from internal knob names, de-duplicated in input order; names outside the contract enum are dropped."""
    out = []
    for value in values:
        try:
            knob = rc.TunableKnob(value)
        except ValueError:
            continue
        if knob not in out:
            out.append(knob)
    return out


def _candidate_contracts(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    contract_epoch_id: str,
    evidence: Dict[str, rc.EvidenceRecord],
    finding_map: Dict[str, str],
    shared_cost_refs: Sequence[str],
    cost_profile: Mapping[str, Any],
) -> Tuple[List[rc.CandidateRecommendation], Dict[str, rc.CandidateRecommendation]]:
    """Contract candidates in input order plus an internal-recommendation-id -> contract candidate map.

    Public ``CAND-`` IDs come from ``rc.make_candidate_id`` (never the legacy internal IDs). Evidence refs keep
    first-seen order because the list feeds the hash payload. Candidate economics stay non-additive.
    """
    baseline = _forward_cost_breakdown(e)
    out: List[rc.CandidateRecommendation] = []
    by_old_id: Dict[str, rc.CandidateRecommendation] = {}
    for c in candidates:
        refs = []
        for f in c.findings:
            mapped = finding_map.get(f.finding_id)
            if mapped and mapped not in refs:
                refs.append(mapped)
        for cost_ref in shared_cost_refs:
            if cost_ref not in refs:
                refs.append(cost_ref)
        technique = _technique_id(c)
        current_value = _scalar_config(c.current_config)
        recommended_value = _scalar_config(c.recommended_config)
        candidate_id = rc.make_candidate_id(
            workspace_id=e.workspace_id,
            warehouse_id=e.warehouse_id,
            epoch_id=contract_epoch_id,
            technique_id=technique,
            knob=c.knob,
            current_value=current_value,
            recommended_value=recommended_value,
            evidence_refs=refs,
            policy_version=POLICY_VERSION,
            analyzer_version=ANALYZER_VERSION,
            optimizer_version=OPTIMIZER_VERSION,
            estimator_version=ESTIMATOR_VERSION,
            reviewer_version=REVIEWER_VERSION,
            decision_graph_version=DECISION_VERSION,
            cost_profile=cost_profile,
        )
        item = rc.CandidateRecommendation(
            candidate_id=candidate_id,
            technique_id=technique,
            knob=rc.TunableKnob(c.knob),
            status=_recommendation_status(c.status),
            current_value=current_value,
            recommended_value=recommended_value,
            objectives=_objectives(c),
            rationale=str(c.metadata.get("rationale") or f"Deterministic {c.knob} policy assessment."),
            evidence_refs=refs,
            blockers=list(c.blockers),
            dependencies=_dependency_refs(c.dependencies),
            invalidates=_tunable_knobs(c.invalidates),
            confidence=_contract_confidence(c.confidence, method="deterministic_evidence_score"),
            independent_economics=rc.IndependentEconomics(
                is_additive=False,
                estimation_status=_estimation_status(c),
                baseline_365d=baseline,
                estimated_cost_365d=_contract_component_ranges(
                    c.dbu_365d_estimated_cost_range, c.aws_365d_estimated_cost_range, c.total_365d_estimated_cost_range
                ),
                savings_365d=_contract_component_ranges(
                    c.dbu_365d_savings_range, c.aws_365d_savings_range, c.total_365d_savings_range
                ),
            ),
        )
        out.append(item)
        by_old_id[c.recommendation_id] = item
    return out, by_old_id


def _compatibility_contract(
    s: Scenario,
    compatibility_map: Mapping[Tuple[str, str], str],
) -> rc.Compatibility:
    """Contract compatibility from the planner's migration checks.

    Only a scenario that actually migrates and passed every check is COMPATIBLE; a failed check is INCOMPATIBLE and
    a migration that was not evaluated is UNKNOWN. PASS_WITH_LIMITATION maps to an UNKNOWN check that needs HITL.
    """
    comp = s.compatibility or {}
    assessment = str(comp.get("assessment") or "CURRENT_TYPE")
    # A planner FAIL (a check failed) is incompatible and a migration whose every check passed is compatible. An empty
    # alternative (no migration step) is never COMPATIBLE: it would hash to the same scenario ID as an empty recommended
    # plan, which is always COMPATIBLE. A migration that was not evaluated has no checks: UNKNOWN.
    migrates = any(st.knob == "warehouse_type" for st in s.steps)
    if assessment in {"CURRENT_TYPE", "COMPATIBLE"} or (assessment == "PASS" and migrates):
        status = rc.CompatibilityStatus.COMPATIBLE
    elif assessment in {"INCOMPATIBLE", "BLOCKED", "FAIL"}:
        status = rc.CompatibilityStatus.INCOMPATIBLE
    elif assessment == "NOT_EVALUATED":
        status = rc.CompatibilityStatus.UNKNOWN
    else:
        status = rc.CompatibilityStatus.REQUIRES_VALIDATION
    mode_raw = str(comp.get("evidence_mode") or ("SYSTEM_TABLE_ONLY" if assessment == "CURRENT_TYPE" else "A_HITL_FALLBACK"))
    try:
        mode = rc.CompatibilityMode(mode_raw)
    except ValueError:
        mode = rc.CompatibilityMode.A_HITL_FALLBACK

    checks: List[rc.CompatibilityCheck] = []
    for check in comp.get("checks", []) or []:
        name = str(check.get("check") or "compatibility_check")
        raw_status = str(check.get("status") or "UNKNOWN")
        status_map = {
            "PASS": rc.CheckStatus.PASS,
            "FAIL": rc.CheckStatus.FAIL,
            "UNKNOWN": rc.CheckStatus.UNKNOWN,
            "NOT_APPLICABLE": rc.CheckStatus.NOT_APPLICABLE,
            "PASS_WITH_LIMITATION": rc.CheckStatus.UNKNOWN,
        }
        check_status = status_map.get(raw_status, rc.CheckStatus.UNKNOWN)
        eid = compatibility_map.get((s.name, name))
        checks.append(rc.CompatibilityCheck(
            check=name,
            status=check_status,
            requires_hitl=check_status == rc.CheckStatus.UNKNOWN or raw_status == "PASS_WITH_LIMITATION",
            evidence_refs=[eid] if eid else [],
            details=None if check.get("evidence") is None else canonical_json(check.get("evidence")),
        ))
    return rc.Compatibility(status=status, mode=mode, checks=checks)


# Planner step states -> locked contract states. READY_FOR_HITL (apply after human approval) is the planner's name for
# the contract's READY; an unmapped state is a bug and must not silently degrade to READY.
_CONTRACT_EXECUTION_STATE = {
    "READY": rc.ExecutionState.READY,
    "READY_FOR_HITL": rc.ExecutionState.READY,
    "READY_FOR_CANARY": rc.ExecutionState.READY_FOR_CANARY,
    "PENDING_REEVALUATION": rc.ExecutionState.PENDING_REEVALUATION,
    "BLOCKED": rc.ExecutionState.BLOCKED,
}


def _scenario_estimation_status(value: str) -> rc.EstimationStatus:
    """Scenario estimate status: a plan with no steps is NO_DELTA_MODELED (known zero); an unrecognised status is UNAVAILABLE, never a guessed number."""
    if value == "NO_CHANGE":  # a plan with no steps changes nothing: a known zero, not an unavailable estimate
        return rc.EstimationStatus.NO_DELTA_MODELED
    try:
        return rc.EstimationStatus(value)
    except ValueError:
        return rc.EstimationStatus.UNAVAILABLE


def _scenario_contracts(
    e: WarehouseEvidence,
    scenarios: Sequence[Scenario],
    contract_epoch_id: str,
    candidates_by_old_id: Mapping[str, rc.CandidateRecommendation],
    candidate_contracts: Sequence[rc.CandidateRecommendation],
    compatibility_map: Mapping[Tuple[str, str], str],
    cost_profile: Mapping[str, Any],
) -> Tuple[List[rc.Scenario], Dict[str, rc.Scenario]]:
    """Contract scenarios in input order plus an internal-scenario-id -> contract scenario map.

    Public ``SCN-`` IDs come from ``rc.make_scenario_id`` over the ordered contract candidate IDs. Adds the
    compatibility-validation step for an unvalidated migration and a trailing remeasure step when any step
    invalidates another. Scenario economics are the stateful bundle estimate (non-additive); contingent economics
    cover pending-reevaluation steps only and never feed package economics. An unmapped planner execution state
    raises instead of degrading to READY.
    """
    baseline = _forward_cost_breakdown(e)
    out: List[rc.Scenario] = []
    by_old_id: Dict[str, rc.Scenario] = {}

    for s in scenarios:
        compatibility = _compatibility_contract(s, compatibility_map)
        ordered_candidates = [candidates_by_old_id[st.recommendation_id] for st in s.steps if st.recommendation_id in candidates_by_old_id]
        candidate_refs = [c.candidate_id for c in ordered_candidates]
        if s.scenario_type is not None:
            scenario_type = rc.ScenarioType(s.scenario_type)
        elif any(c.knob == rc.TunableKnob.WAREHOUSE_TYPE for c in ordered_candidates):
            scenario_type = rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION
        else:
            scenario_type = rc.ScenarioType.IN_PLACE_OPTIMIZATION
        scenario_id = rc.make_scenario_id(
            workspace_id=e.workspace_id,
            warehouse_id=e.warehouse_id,
            epoch_id=contract_epoch_id,
            scenario_type=scenario_type.value,
            ordered_candidate_ids=candidate_refs,
            compatibility_status=compatibility.status.value,
            policy_version=POLICY_VERSION,
            bundle_estimator_version=BUNDLE_MODEL_VERSION,
            decision_graph_version=DECISION_VERSION,
            cost_profile=cost_profile,
        )
        plan_steps: List[rc.PlanStep] = []
        next_step = 1
        migrates = any(c.knob == rc.TunableKnob.WAREHOUSE_TYPE for c in ordered_candidates)
        if migrates and compatibility.status == rc.CompatibilityStatus.REQUIRES_VALIDATION:
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.COMPATIBILITY_VALIDATION,
                candidate_id=None, technique_id=None, knob=None,
                action="VALIDATE_WAREHOUSE_TYPE_COMPATIBILITY",
                current_value=None, recommended_value=None,
                execution_state=rc.ExecutionState.REQUIRED,
                confidence=_contract_confidence(s.confidence, method="compatibility_and_scenario_evidence"),
                validation=rc.ValidationGate(rc.ValidationType.COMPATIBILITY, [], None, "CONTINUE_IF_PASS"),
                dependencies=[], invalidates=[],
            ))
            next_step += 1

        for st in s.steps:
            c = candidates_by_old_id.get(st.recommendation_id)
            if c is None:
                continue
            validation_raw = str(st.validation_gate or "")
            if "CANARY" in validation_raw:
                validation_type = rc.ValidationType.A_B_CANARY
                success_metrics = ["p95_execution_ms", "p99_execution_ms", "failure_rate", "queue_wait", "dbu_cost", "aws_cost"]
            elif "COMPAT" in validation_raw:
                validation_type = rc.ValidationType.COMPATIBILITY
                success_metrics = []
            elif "RE_ANALYZE" in validation_raw or st.execution_state == "PENDING_REEVALUATION":
                validation_type = rc.ValidationType.HITL
                success_metrics = []
            else:
                validation_type = rc.ValidationType.NONE
                success_metrics = []
            try:
                state = _CONTRACT_EXECUTION_STATE[st.execution_state]
            except KeyError:
                raise ValueError(f"Planner execution state {st.execution_state!r} has no locked-contract mapping") from None
            if compatibility.status == rc.CompatibilityStatus.INCOMPATIBLE:
                state = rc.ExecutionState.BLOCKED  # a failed compatibility check: no canary until it is resolved
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.CANDIDATE,
                candidate_id=c.candidate_id,
                technique_id=c.technique_id,
                knob=c.knob,
                action=None,
                current_value=c.current_value,
                recommended_value=c.recommended_value,
                execution_state=state,
                confidence=c.confidence,
                validation=rc.ValidationGate(
                    validation_type=validation_type,
                    success_metrics=success_metrics,
                    remeasure_days=st.remeasure_days if validation_type == rc.ValidationType.REMEASURE else None,
                    action_after=None,
                ),
                dependencies=_dependency_refs(st.dependencies),
                invalidates=_tunable_knobs(st.invalidates),
                projected_economics=_step_projection(st),
            ))
            next_step += 1

        if any(step.invalidates for step in plan_steps if step.step_type == rc.PlanStepType.CANDIDATE):
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.REMEASURE,
                candidate_id=None, technique_id=None, knob=None,
                action="REMEASURE_AND_REGENERATE",
                current_value=None, recommended_value=None,
                execution_state=rc.ExecutionState.REQUIRED,
                confidence=rc.Confidence(1.0, rc.ConfidenceLevel.HIGH, {"policy_gate": 1.0}, "policy_gate"),
                validation=rc.ValidationGate(
                    rc.ValidationType.REMEASURE, [],
                    remeasure_days=max(1, max((st.remeasure_days for st in s.steps if st.invalidates), default=7)),
                    action_after="REGENERATE_RECOMMENDATIONS",
                ),
                dependencies=[], invalidates=[],
            ))

        scenario_evidence_refs: List[str] = []
        for c in ordered_candidates:
            for ref in c.evidence_refs:
                if ref not in scenario_evidence_refs:
                    scenario_evidence_refs.append(ref)
        for check in compatibility.checks:
            for ref in check.evidence_refs:
                if ref not in scenario_evidence_refs:
                    scenario_evidence_refs.append(ref)

        pe = s.portfolio_estimate
        ct = pe.contingent
        economics = rc.ScenarioEconomics(
            estimation_status=_scenario_estimation_status(pe.estimate_status),
            baseline_365d=baseline,
            estimated_cost_365d=_contract_component_ranges(
                pe.dbu_365d_estimated_cost_range, pe.aws_365d_estimated_cost_range, pe.total_365d_estimated_cost_range
            ),
            savings_365d=_contract_component_ranges(
                pe.dbu_365d_savings_range, pe.aws_365d_savings_range, pe.total_365d_savings_range
            ),
            method=pe.method,
            is_additive=False,
            contingent_estimated_cost_365d=_contract_component_ranges(
                ct.dbu_365d_estimated_cost_range, ct.aws_365d_estimated_cost_range, ct.total_365d_estimated_cost_range
            ) if ct is not None else None,
            contingent_savings_365d=_contract_component_ranges(
                ct.dbu_365d_savings_range, ct.aws_365d_savings_range, ct.total_365d_savings_range
            ) if ct is not None else None,
        )
        item = rc.Scenario(
            scenario_id=scenario_id,
            scenario_name=s.name,
            scenario_type=scenario_type,
            status=rc.ScenarioStatus.RECOMMENDED if s.recommended else rc.ScenarioStatus.ALTERNATIVE,
            candidate_refs=candidate_refs,
            evidence_refs=scenario_evidence_refs,
            confidence=_contract_confidence(s.confidence, method="minimum_step_confidence"),
            compatibility=compatibility,
            portfolio_economics=economics,
            execution_plan=plan_steps,
        )
        out.append(item)
        by_old_id[s.scenario_id] = item
    return out, by_old_id


def _assessment_contract(e: WarehouseEvidence, candidates: Sequence[rc.CandidateRecommendation], scenarios: Sequence[rc.Scenario], t: Thresholds) -> rc.Assessment:
    """Per-status candidate counts, the overall status (see ``_overall_status``) and the evidence-quality block (query sample, cost history, epoch maturity, source coverage)."""
    counts = {status: 0 for status in rc.RecommendationStatus}
    for c in candidates:
        counts[c.status] += 1
    overall = _overall_status(candidates, scenarios)

    q, _ = carried_query_view(e.current_epoch.query)
    query_ok = safe_int(q.get("queries_30")) >= t.min_query_sample_size
    cost_ok = e.cost_history_days >= t.min_cost_history_days
    epoch_ok = ready_billed_days(e) >= t.readiness_min_billed_days
    source_flags = [True, bool(q), bool(e.current_epoch.events), e.cost_history_days > 0, bool(e.sql_sku_prices)]
    coverage = round(100.0 * sum(1 for x in source_flags if x) / len(source_flags), 6)
    score = round(0.25 * float(query_ok) + 0.25 * float(cost_ok) + 0.20 * float(epoch_ok) + 0.30 * coverage / 100.0, 6)
    quality_status = rc.EvidenceQualityStatus.SUFFICIENT if query_ok and cost_ok and epoch_ok and coverage >= 80 else rc.EvidenceQualityStatus.PARTIAL if coverage >= 50 else rc.EvidenceQualityStatus.INSUFFICIENT
    return rc.Assessment(
        overall_status=overall,
        summary=rc.AssessmentSummary(
            candidate_count=len(candidates),
            actionable_count=counts[rc.RecommendationStatus.ACTIONABLE],
            test_required_count=counts[rc.RecommendationStatus.TEST_REQUIRED],
            review_count=counts[rc.RecommendationStatus.REVIEW],
            blocked_count=counts[rc.RecommendationStatus.BLOCKED],
            no_change_count=counts[rc.RecommendationStatus.NO_CHANGE],
            not_applicable_count=counts[rc.RecommendationStatus.NOT_APPLICABLE],
            insufficient_evidence_count=counts[rc.RecommendationStatus.INSUFFICIENT_EVIDENCE],
        ),
        evidence_quality=rc.EvidenceQuality(
            status=quality_status,
            confidence=_contract_confidence({"score": score, "components": {"query_sample": float(query_ok), "cost_history": float(cost_ok), "epoch_maturity": float(epoch_ok), "source_coverage": coverage / 100.0}}, method="minimum_source_and_epoch_coverage"),
            query_sample_sufficient=query_ok,
            cost_history_sufficient=cost_ok,
            epoch_mature=epoch_ok,
            source_coverage_pct=coverage,
        ),
    )


def compute_reevaluation(e: WarehouseEvidence, thresholds: Thresholds, *, start_time: str, epoch_age: int) -> Optional[rc.ReevaluationWindow]:
    """Gates that currently hide the next step; None once both are clear (contract 2.0.0).

    Pure function of the epoch start, the analysis time, the settled billed days and the cost-history days. The contract
    validator embeds the same gate lengths, so a policy change to them must travel with a contract version bump.
    """
    if (thresholds.readiness_min_billed_days, thresholds.min_economics_cost_history_days) != (
        rc.REEVALUATION_READY_BILLED_DAYS, rc.REEVALUATION_COST_HISTORY_DAYS
    ):
        raise ValueError("Re-evaluation gate thresholds differ from the locked contract constants; bump the contract version")
    codes = []
    if e.cost_history_days < thresholds.min_economics_cost_history_days:
        codes.append("COST_HISTORY_INSUFFICIENT")
    if ready_billed_days(e) < thresholds.readiness_min_billed_days:
        codes.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
    if not codes:
        return None
    return rc.ReevaluationWindow(
        status=rc.ReevaluationStatus.WAITING_FOR_EVIDENCE,
        earliest_reevaluation_time=rc.expected_earliest_reevaluation_time(start_time, sorted(codes), _iso_z(e.analysis_time)),
        blocking_codes=sorted(codes),
        method=(
            f"Later of the active gates: the next settled day, 00:00Z of the analysis date + 1 day (readiness: "
            f"{rc.REEVALUATION_READY_BILLED_DAYS} settled billed days in the current epoch) and 00:00Z of epoch start date + "
            f"{rc.REEVALUATION_COST_HISTORY_DAYS} days (cost-history floor). Earliest only: billing ingestion lag, days "
            "without billing and query-sample gates can delay it."
        ),
    )


def derive_observed_change(e: WarehouseEvidence) -> Optional[rc.ObservedConfigChange]:
    """Config diff between the immediately preceding epoch and the current one (authoritative, unmeasured; contract 1.2.0)."""
    cur = e.current_epoch
    cur_start = parse_datetime(cur.epoch_start)
    prior = [x for x in e.epochs if not x.is_current and (parse_datetime(x.epoch_start) or cur_start) < cur_start]
    if not prior:
        return None
    prev = max(prior, key=lambda x: parse_datetime(x.epoch_start))
    changes = [
        rc.ConfigChange(rc.TunableKnob(knob), getattr(prev.config, knob), getattr(cur.config, knob))
        for knob in sorted(("warehouse_type", "warehouse_channel", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes"))
        if getattr(prev.config, knob) != getattr(cur.config, knob)
    ]
    if not changes:
        return None
    pc = prev.config
    previous_epoch_id = rc.make_epoch_id(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, start_time=_iso_z(prev.epoch_start),
        warehouse_type=pc.warehouse_type, warehouse_size=pc.warehouse_size, min_clusters=pc.min_clusters,
        max_clusters=pc.max_clusters, auto_stop_minutes=pc.auto_stop_minutes, warehouse_channel=pc.warehouse_channel,
    )
    return rc.ObservedConfigChange(
        previous_epoch_id=previous_epoch_id, previous_epoch_start=_iso_z(prev.epoch_start), changed_at=_iso_z(cur.epoch_start),
        changes=sorted(changes, key=lambda c: c.knob.value), measurement_status=rc.MeasurementStatus.NOT_MEASURED,
    )
