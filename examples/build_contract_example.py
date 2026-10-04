from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.reporter_contract import *

WORKSPACE = "1234567890123456"
WAREHOUSE = "wh-demo-001"
REGION = "us-east-1"
CURRENCY = "USD"

POLICY_VERSION = "policy-v2.9.0"
ANALYZER_VERSION = "2.9.0"
OPTIMIZER_VERSION = "2.9.0"
MODELER_VERSION = "2.9.0"
ESTIMATOR_VERSION = "2.9.0"
BUNDLE_VERSION = "2.9.0"
REVIEWER_VERSION = "2.9.0"
DECISION_VERSION = "decision-graph-v2.9.0"
CATALOG_VERSION = "technique-catalog-v1"


def conf(score: float, method: str = "deterministic_weighted_evidence") -> Confidence:
    level = ConfidenceLevel.HIGH if score >= 0.80 else ConfidenceLevel.MEDIUM if score >= 0.60 else ConfidenceLevel.LOW
    return Confidence(score=score, level=level, components={"coverage": min(1.0, score + 0.05), "signal": score}, method=method)


def cost(dbu: float | None, aws: float | None, *, dbu_basis: CostBasis, aws_basis: CostBasis) -> CostBreakdown:
    total = None if dbu is None or aws is None else round(dbu + aws, 2)
    availability = CostAvailability.COMPLETE if total is not None else CostAvailability.PARTIAL if dbu is not None or aws is not None else CostAvailability.UNAVAILABLE
    return CostBreakdown(dbu, aws, total, availability, dbu_basis, aws_basis)


def ranges(dbu, aws, total) -> ComponentRanges:
    return ComponentRanges(dbu=Range3(*dbu) if dbu is not None else None,
                           aws=Range3(*aws) if aws is not None else None,
                           total=Range3(*total) if total is not None else None)


def build_package() -> RecommendationPackage:
    epoch_id = make_epoch_id(
        workspace_id=WORKSPACE, warehouse_id=WAREHOUSE, start_time="2026-07-12T14:32:00Z",
        warehouse_type="PRO", warehouse_size="LARGE", min_clusters=2, max_clusters=4,
        auto_stop_minutes=120, warehouse_channel="CURRENT",
    )

    raw_evidence = [
        ("queued_query_pct_30d", 0.0, "percent", "system.query.history", "waiting_at_capacity_duration_ms", "30d", 0.5, "<= safe", "No meaningful concurrency pressure"),
        ("spill_query_pct_30d", 0.0, "percent", "system.query.history", "spilled_local_bytes", "30d", 0.1, "<= downsize-safe", "No observed spill pressure"),
        ("p95_execution_ms_30d", 2500, "ms", "system.query.history", "execution_duration_ms", "30d", 5000, "<= downsize-safe", "Interactive execution latency is below the downsize guardrail"),
        ("p99_execution_ms_30d", 10000, "ms", "system.query.history", "execution_duration_ms", "30d", 30000, "<= downsize-safe", "Tail latency is below the downsize guardrail"),
        ("time_at_min_clusters_pct", 80.0, "percent", "system.compute.warehouse_events", "cluster_count", "current_epoch", 25.0, ">= reduce-candidate", "Warehouse spends most running time at minimum capacity"),
        ("max_observed_clusters", 2, "clusters", "system.compute.warehouse_events", "cluster_count", "current_epoch", 4, "< configured_max", "Configured maximum has not been required"),
        ("estimated_avoidable_idle_minutes", 9000, "minutes", "derived", "query_gap_model", "current_epoch", 0, "> opportunity", "Material idle-running opportunity"),
        ("photon_enabled_usage_pct", 100.0, "percent", "system.billing.usage", "product_features.is_photon", "current_epoch", 95.0, ">= enabled", "Billing evidence consistently observes Photon-enabled SQL DBUs"),
        ("serverless_workspace_eligibility", True, "boolean", "workspace compatibility evidence", "enable_serverless_compute", "point_in_time", True, "==", "Workspace reports Serverless enabled; remaining compatibility checks still require validation"),
    ]

    evidence: dict[str, EvidenceRecord] = {}
    for metric, value, unit, source, source_field, window, threshold, comparator, interpretation in raw_evidence:
        evid = make_evidence_id(
            workspace_id=WORKSPACE, warehouse_id=WAREHOUSE, epoch_id=epoch_id,
            metric=metric, value=value, unit=unit, source=source, source_field=source_field,
            observation_window=window, threshold=threshold, comparator=comparator,
        )
        evidence[evid] = EvidenceRecord(
            evidence_id=evid, metric=metric, value=value, unit=unit, source=source,
            source_field=source_field, epoch_scope="CURRENT", observation_window=window,
            threshold=threshold, comparator=comparator, interpretation=interpretation,
        )

    by_metric = {e.metric: e.evidence_id for e in evidence.values()}
    evidence_fingerprint = make_evidence_fingerprint(
        workspace_id=WORKSPACE,
        warehouse_id=WAREHOUSE,
        epoch_id=epoch_id,
        evidence_records=[to_primitive(x) for x in evidence.values()],
    )

    current_config = CurrentConfig(
        warehouse_type="PRO", warehouse_size="LARGE", min_clusters=2, max_clusters=4,
        auto_stop_minutes=120, warehouse_channel="CURRENT",
        photon=PhotonConfigObservation(
            status=PhotonStatus.ENABLED,
            evidence_source="system.billing.usage.product_features.is_photon",
            confidence=conf(0.98, "signed_current_epoch_dbu_feature_coverage"),
            enabled_usage_pct=100.0, known_feature_coverage_pct=100.0, freshness_days=0,
        ),
    )

    forward = cost(27000.0, 8793.82, dbu_basis=CostBasis.FORECAST, aws_basis=CostBasis.MODELED_AWS_LIST_PRICE)
    actual = cost(25000.0, 8150.0, dbu_basis=CostBasis.OBSERVED_DATABRICKS_BILLING, aws_basis=CostBasis.MODELED_AWS_LIST_PRICE)

    versions = dict(
        policy_version=POLICY_VERSION, analyzer_version=ANALYZER_VERSION,
        optimizer_version=OPTIMIZER_VERSION, estimator_version=ESTIMATOR_VERSION,
        reviewer_version=REVIEWER_VERSION, decision_graph_version=DECISION_VERSION,
    )

    candidate_specs = [
        # technique, knob, status, current, recommended, objective, evidence, blockers, invalidates, confidence, economics
        ("WH-SIZE-DOWNSIZE-001", TunableKnob.WAREHOUSE_SIZE, RecommendationStatus.TEST_REQUIRED, "LARGE", "MEDIUM", [Objective.COST], [by_metric["spill_query_pct_30d"],by_metric["p95_execution_ms_30d"],by_metric["p99_execution_ms_30d"],by_metric["queued_query_pct_30d"]], ["A_B_PERFORMANCE_VALIDATION_REQUIRED"], [TunableKnob.MAX_CLUSTERS,TunableKnob.MIN_CLUSTERS,TunableKnob.AUTO_STOP_MINUTES], 0.78,
         ranges((5300,7300,8900),(1800,2450,3100),(7100,9750,12000))),
        ("WH-MAX-CLUSTER-REDUCE-001", TunableKnob.MAX_CLUSTERS, RecommendationStatus.ACTIONABLE, 4, 3, [Objective.COST_GUARDRAIL], [by_metric["max_observed_clusters"],by_metric["queued_query_pct_30d"]], [], [], 0.92,
         ranges((0,0,0),(0,0,0),(0,0,0))),
        ("WH-MIN-CLUSTER-REDUCE-001", TunableKnob.MIN_CLUSTERS, RecommendationStatus.ACTIONABLE, 2, 1, [Objective.COST], [by_metric["time_at_min_clusters_pct"],by_metric["queued_query_pct_30d"]], [], [], 0.90,
         ranges((3500,6750,10000),(1000,2500,4000),(4500,9250,14000))),
        ("WH-AUTOSTOP-REDUCE-001", TunableKnob.AUTO_STOP_MINUTES, RecommendationStatus.ACTIONABLE, 120, 30, [Objective.COST], [by_metric["estimated_avoidable_idle_minutes"]], [], [], 0.93,
         ranges((4000,8100,12000),(1200,3000,5000),(5200,11100,17000))),
        ("WH-SERVERLESS-MIGRATE-001", TunableKnob.WAREHOUSE_TYPE, RecommendationStatus.TEST_REQUIRED, "PRO", "SERVERLESS", [Objective.MIGRATION,Objective.COST_AND_PERFORMANCE], [by_metric["serverless_workspace_eligibility"]], ["SERVERLESS_COMPATIBILITY_VALIDATION_REQUIRED","A_B_COST_PERFORMANCE_VALIDATION_REQUIRED"], [TunableKnob.WAREHOUSE_SIZE,TunableKnob.MIN_CLUSTERS,TunableKnob.MAX_CLUSTERS,TunableKnob.AUTO_STOP_MINUTES], 0.61,
         ranges((4000,9000,13000),(8793.82,8793.82,8793.82),(12793.82,17793.82,21793.82))),
    ]

    candidates: list[CandidateRecommendation] = []
    for technique, knob, status, cur, rec, objectives, refs, blockers, invalidates, score, saving_ranges in candidate_specs:
        cid = make_candidate_id(
            workspace_id=WORKSPACE, warehouse_id=WAREHOUSE, epoch_id=epoch_id,
            technique_id=technique, knob=knob.value, current_value=cur, recommended_value=rec,
            evidence_refs=refs, **versions,
        )
        # Independent estimated cost range is baseline minus savings high/base/low.
        def cost_from_savings(r: Range3 | None, baseline: float) -> Range3 | None:
            return None if r is None else Range3(round(baseline-r.high,2), round(baseline-r.base,2), round(baseline-r.low,2))
        estimated = ComponentRanges(
            dbu=cost_from_savings(saving_ranges.dbu,27000.0),
            aws=cost_from_savings(saving_ranges.aws,8793.82),
            total=cost_from_savings(saving_ranges.total,35793.82),
        )
        candidates.append(CandidateRecommendation(
            candidate_id=cid, technique_id=technique, knob=knob, status=status,
            current_value=cur, recommended_value=rec, objectives=objectives,
            rationale={
                TunableKnob.WAREHOUSE_SIZE:"Current epoch shows negligible spill/queue pressure and low execution latency, supporting a one-size-down canary.",
                TunableKnob.MAX_CLUSTERS:"Observed scale never required the configured maximum; lower the unused guardrail.",
                TunableKnob.MIN_CLUSTERS:"The warehouse spends most running time at minimum capacity without queue pressure.",
                TunableKnob.AUTO_STOP_MINUTES:"Idle-gap analysis shows material avoidable running time with acceptable cold-start risk.",
                TunableKnob.WAREHOUSE_TYPE:"Serverless is economically assessable but requires compatibility and representative canary validation.",
            }[knob],
            evidence_refs=refs, blockers=blockers, dependencies=[], invalidates=invalidates,
            confidence=conf(score),
            independent_economics=IndependentEconomics(
                is_additive=False,
                estimation_status=EstimationStatus.NO_DELTA_MODELED if status==RecommendationStatus.NO_CHANGE else EstimationStatus.PRE_CANARY_MODELED_RANGE if knob==TunableKnob.WAREHOUSE_TYPE else EstimationStatus.PROVISIONAL_MODELED_RANGE,
                baseline_365d=forward,
                estimated_cost_365d=estimated,
                savings_365d=saving_ranges,
            ),
        ))

    cand = {x.knob:x for x in candidates}

    plan_a_ids = [cand[TunableKnob.WAREHOUSE_SIZE].candidate_id,cand[TunableKnob.MAX_CLUSTERS].candidate_id,cand[TunableKnob.MIN_CLUSTERS].candidate_id,cand[TunableKnob.AUTO_STOP_MINUTES].candidate_id]
    plan_a_id = make_scenario_id(
        workspace_id=WORKSPACE, warehouse_id=WAREHOUSE, epoch_id=epoch_id,
        scenario_type=ScenarioType.IN_PLACE_OPTIMIZATION.value,
        ordered_candidate_ids=plan_a_ids, compatibility_status=CompatibilityStatus.COMPATIBLE.value,
        policy_version=POLICY_VERSION,bundle_estimator_version=BUNDLE_VERSION,decision_graph_version=DECISION_VERSION,
    )
    plan_b_ids = [cand[TunableKnob.WAREHOUSE_TYPE].candidate_id]
    plan_b_id = make_scenario_id(
        workspace_id=WORKSPACE, warehouse_id=WAREHOUSE, epoch_id=epoch_id,
        scenario_type=ScenarioType.WAREHOUSE_TYPE_MIGRATION.value,
        ordered_candidate_ids=plan_b_ids, compatibility_status=CompatibilityStatus.REQUIRES_VALIDATION.value,
        policy_version=POLICY_VERSION,bundle_estimator_version=BUNDLE_VERSION,decision_graph_version=DECISION_VERSION,
    )

    # Plan A headline economics are AUTHORITATIVE: only the size step is priced; the max/min/auto-stop steps
    # are PENDING_REEVALUATION after the size canary. Their stateful upside is published separately as contingent.
    plan_a_savings = ranges((3000,4800,6600),(1000,1700,2400),(4000,6500,9000))
    plan_a_cost = ranges((20400,22200,24000),(6393.82,7093.82,7793.82),(26793.82,29293.82,31793.82))
    plan_a_contingent_savings = ranges((11130.20,16781.91,22621.99),(2814.02,5232.32,7386.81),(13944.22,22014.23,30008.80))
    plan_a_contingent_cost = ranges((4378.01,10218.09,15869.80),(1407.01,3561.50,5979.80),(5785.02,13779.59,21849.60))
    step_running = {
        1: (StepProjection(PricingBasis.AUTHORITATIVE, plan_a_savings)),
        2: (StepProjection(PricingBasis.CONTINGENT_PENDING_REEVALUATION, plan_a_savings)),
        3: (StepProjection(PricingBasis.CONTINGENT_PENDING_REEVALUATION, ranges((7000,11000,15500),(1800,3300,4800),(8800,14300,20300)))),
        4: (StepProjection(PricingBasis.CONTINGENT_PENDING_REEVALUATION, plan_a_contingent_savings)),
    }
    plan_b_savings = ranges((4000,9000,13000),(8793.82,8793.82,8793.82),(12793.82,17793.82,21793.82))
    plan_b_cost = ranges((14000,18000,23000),(0,0,0),(14000,18000,23000))

    def step(n:int, c:CandidateRecommendation, state:ExecutionState, validation:ValidationType, invalidates=None, projection=None) -> PlanStep:
        return PlanStep(
            step=n, step_type=PlanStepType.CANDIDATE, candidate_id=c.candidate_id, technique_id=c.technique_id, knob=c.knob,
            action=None,current_value=c.current_value,recommended_value=c.recommended_value,execution_state=state,confidence=c.confidence,
            validation=ValidationGate(validation_type=validation,success_metrics=["p95_execution_ms","p99_execution_ms","failure_rate","queue_wait","dbu_cost","aws_cost"] if validation==ValidationType.A_B_CANARY else [],remeasure_days=7 if validation==ValidationType.REMEASURE else None,action_after="REGENERATE_RECOMMENDATIONS" if validation==ValidationType.REMEASURE else None),
            dependencies=[],invalidates=invalidates or [],projected_economics=projection,
        )

    plan_a_steps = [
        step(1,cand[TunableKnob.WAREHOUSE_SIZE],ExecutionState.READY_FOR_CANARY,ValidationType.A_B_CANARY,[TunableKnob.MAX_CLUSTERS,TunableKnob.MIN_CLUSTERS,TunableKnob.AUTO_STOP_MINUTES],projection=step_running[1]),
        step(2,cand[TunableKnob.MAX_CLUSTERS],ExecutionState.PENDING_REEVALUATION,ValidationType.HITL,projection=step_running[2]),
        step(3,cand[TunableKnob.MIN_CLUSTERS],ExecutionState.PENDING_REEVALUATION,ValidationType.HITL,projection=step_running[3]),
        step(4,cand[TunableKnob.AUTO_STOP_MINUTES],ExecutionState.PENDING_REEVALUATION,ValidationType.HITL,projection=step_running[4]),
        PlanStep(step=5,step_type=PlanStepType.REMEASURE,candidate_id=None,technique_id=None,knob=None,action="REMEASURE_AND_REGENERATE",current_value=None,recommended_value=None,execution_state=ExecutionState.REQUIRED,confidence=conf(1.0,"policy_gate"),validation=ValidationGate(ValidationType.REMEASURE,[],7,"REGENERATE_RECOMMENDATIONS"),dependencies=[],invalidates=[]),
    ]
    plan_b_steps = [
        PlanStep(step=1,step_type=PlanStepType.COMPATIBILITY_VALIDATION,candidate_id=None,technique_id=None,knob=None,action="VALIDATE_SERVERLESS_COMPATIBILITY",current_value=None,recommended_value=None,execution_state=ExecutionState.REQUIRED,confidence=conf(0.75),validation=ValidationGate(ValidationType.COMPATIBILITY,[],None,"CONTINUE_IF_PASS"),dependencies=[],invalidates=[]),
        step(2,cand[TunableKnob.WAREHOUSE_TYPE],ExecutionState.READY_FOR_CANARY,ValidationType.A_B_CANARY,[TunableKnob.WAREHOUSE_SIZE,TunableKnob.MIN_CLUSTERS,TunableKnob.MAX_CLUSTERS,TunableKnob.AUTO_STOP_MINUTES]),
    ]

    scenarios = [
        Scenario(
            scenario_id=plan_a_id, scenario_name="Plan A — Optimize current PRO warehouse", scenario_type=ScenarioType.IN_PLACE_OPTIMIZATION,
            status=ScenarioStatus.RECOMMENDED, candidate_refs=plan_a_ids,
            evidence_refs=[by_metric["queued_query_pct_30d"],by_metric["spill_query_pct_30d"],by_metric["time_at_min_clusters_pct"],by_metric["estimated_avoidable_idle_minutes"]],
            confidence=conf(0.78,"minimum_step_confidence"),
            compatibility=Compatibility(CompatibilityStatus.COMPATIBLE,CompatibilityMode.SYSTEM_TABLE_ONLY,[]),
            portfolio_economics=ScenarioEconomics(EstimationStatus.PROVISIONAL_MODELED_RANGE,forward,plan_a_cost,plan_a_savings,"stateful_bundle_estimator_authoritative_prefix",False,
                                                  contingent_estimated_cost_365d=plan_a_contingent_cost,contingent_savings_365d=plan_a_contingent_savings),
            execution_plan=plan_a_steps,
        ),
        Scenario(
            scenario_id=plan_b_id, scenario_name="Plan B — Migrate to Serverless", scenario_type=ScenarioType.WAREHOUSE_TYPE_MIGRATION,
            status=ScenarioStatus.ALTERNATIVE, candidate_refs=plan_b_ids, evidence_refs=[by_metric["serverless_workspace_eligibility"]],
            confidence=conf(0.61,"minimum_step_and_compatibility_confidence"),
            compatibility=Compatibility(CompatibilityStatus.REQUIRES_VALIDATION,CompatibilityMode.B_READ_ONLY_API_WITH_A_FALLBACK,[
                CompatibilityCheck("serverless_enabled",CheckStatus.PASS,False,[by_metric["serverless_workspace_eligibility"]]),
                CompatibilityCheck("external_legacy_hive_metastore",CheckStatus.PASS,False,[]),
                CompatibilityCheck("s3_access_policy",CheckStatus.UNKNOWN,True,[],"Requires HITL when read-only compatibility evidence cannot establish this workspace prerequisite."),
            ]),
            portfolio_economics=ScenarioEconomics(EstimationStatus.PRE_CANARY_MODELED_RANGE,forward,plan_b_cost,plan_b_savings,"serverless_dbu_schedule_plus_customer_aws_elimination",False),
            execution_plan=plan_b_steps,
        ),
    ]

    assessment = Assessment(
        AssessmentStatus.OPTIMIZATION_AVAILABLE,
        AssessmentSummary(candidate_count=5,actionable_count=3,test_required_count=2,review_count=0,blocked_count=0,no_change_count=0,not_applicable_count=0,insufficient_evidence_count=0),
        EvidenceQuality(EvidenceQualityStatus.SUFFICIENT,conf(0.94),True,True,True,100.0),
    )

    provenance = Provenance(
        contract_version=CONTRACT_VERSION,policy_version=POLICY_VERSION,analyzer_version=ANALYZER_VERSION,optimizer_version=OPTIMIZER_VERSION,
        modeler_version=MODELER_VERSION,estimator_version=ESTIMATOR_VERSION,bundle_estimator_version=BUNDLE_VERSION,reviewer_version=REVIEWER_VERSION,
        decision_graph_version=DECISION_VERSION,technique_catalog_version=CATALOG_VERSION,evidence_fingerprint=evidence_fingerprint,
        source_tables=["system.compute.warehouses","system.compute.warehouse_events","system.query.history","system.billing.usage","system.billing.list_prices"],
        source_regions=[REGION],generated_at="2026-10-01T18:00:00Z",package_hash_full_sha256=None,
    )

    realization = Realization(
        status=RealizationStatus.NOT_APPLIED,
        realization_records=[],
        aggregate=RealizationAggregate(0,0,0,CURRENCY,0.0,0.0),
    )

    # Package envelope: total low = minimum LOW across assessable scenarios;
    # base = BASE from recommended scenario; high = maximum HIGH across scenarios.
    envelope = ScenarioEnvelope(
        minimum_savings=ScenarioEnvelopePoint(
            scenario_id=plan_a_id,bound=BoundName.LOW,
            savings_365d=EconomicsPoint(3000,1000,4000),
            estimated_cost_365d=EconomicsPoint(24000,7793.82,31793.82),
        ),
        recommended_base=ScenarioEnvelopePoint(
            scenario_id=plan_a_id,bound=BoundName.BASE,
            savings_365d=EconomicsPoint(4800,1700,6500),
            estimated_cost_365d=EconomicsPoint(22200,7093.82,29293.82),
        ),
        maximum_savings=ScenarioEnvelopePoint(
            scenario_id=plan_b_id,bound=BoundName.HIGH,
            savings_365d=EconomicsPoint(13000,8793.82,21793.82),
            estimated_cost_365d=EconomicsPoint(14000,0,14000),
        ),
    )
    package_economics = PackageEconomics(
        currency=CURRENCY,baseline_365d=forward,
        estimated_savings_365d=Range3(4000,6500,21793.82),
        estimated_cost_365d=Range3(14000,29293.82,31793.82),
        scenario_envelope=envelope,included_scenario_ids=[plan_a_id,plan_b_id],excluded_scenarios=[],
        semantics=PackageEconomicsSemantics("SCENARIO_ENVELOPE",False,False),
    )

    package = RecommendationPackage(
        recommendation_package_id="PKG-" + "0"*24,
        contract_version=CONTRACT_VERSION,
        warehouse=WarehouseIdentity(WORKSPACE,WAREHOUSE,"BI - Sales","sales-bi@databricks.example",REGION,CurrentEpoch(epoch_id,"2026-07-12T14:32:00Z",81,True)),
        current_config=current_config,
        workload_profile=WorkloadProfile(
            primary_query_source="DASHBOARD",
            query_sources={"DASHBOARD":QuerySourceStat(7000,58.333),"JOB":QuerySourceStat(3000,25.0),"NOTEBOOK":QuerySourceStat(1000,8.333),"GENIE":QuerySourceStat(500,4.167),"ALERT":QuerySourceStat(300,2.5)},
            client_applications={"POWER_BI":ClientApplicationStat(4200),"TABLEAU":ClientApplicationStat(1200),"SQL_EDITOR":ClientApplicationStat(200)},
            activity=WorkloadActivity(12000,30,400.0),
            workload_characteristics=WorkloadCharacteristics(WorkloadLatencyClass.INTERACTIVE,ConcurrencyPattern.BURSTY,ActivityPattern.BUSINESS_HOURS),
        ),
        cost_baseline=CostBaseline(CURRENCY,actual,forward,ForecastInfo("runrate-v2.7",conf(0.91),["current epoch remains representative","AWS modeled from regional on-demand registry"])),
        assessment=assessment,evidence=evidence,candidates=candidates,scenarios=scenarios,
        recommended_plan=RecommendedPlan(plan_a_id,"CURRENT_TYPE_OPTIMIZATION_HAS_VALIDATED_EVIDENCE","RUN_SIZE_CANARY",True,"Validate one-size-down first; then remeasure and regenerate invalidated downstream recommendations."),
        package_economics=package_economics,realization=realization,provenance=provenance,
    )
    package = dataclasses.replace(package,recommendation_package_id=package.expected_package_id())
    package = dataclasses.replace(package,provenance=dataclasses.replace(package.provenance,package_hash_full_sha256=package.full_package_hash()))
    package.validate()
    assert package.provenance.evidence_fingerprint == package.evidence_fingerprint_expected()

    return package

def main() -> None:
    package = build_package()
    out = Path(__file__).with_name("recommendation-package.example.json")
    out.write_text(json.dumps(package.to_dict(),indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(out)
    print(package.recommendation_package_id)

if __name__ == "__main__":
    main()
