"""Packager: plan scenarios for the reviewed candidates and assemble the frozen, content-addressed RecommendationPackage per warehouse."""
from __future__ import annotations

import dataclasses
from typing import Dict, List, Sequence

from publisher.analyzer.epochs import epoch_age_days
from publisher.common.hashing import canonical_json
from publisher.common.logging import LOGGER
from publisher.common.time import _iso_z
from publisher.config.thresholds import Thresholds
from publisher.config.versions import (
    ANALYZER_VERSION,
    BUNDLE_MODEL_VERSION,
    DECISION_VERSION,
    ESTIMATOR_VERSION,
    MODEL_VERSION,
    OPTIMIZER_VERSION,
    POLICY_VERSION,
    REVIEWER_VERSION,
    TECHNIQUE_CATALOG_VERSION,
)
from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.package import PackagingResult
from publisher.contracts.request import RunContext
from publisher.contracts.review import CandidateReviewResult, WarehouseOptimization
from publisher.contracts.scenario import Scenario
from publisher.contracts.source import WarehouseEvidence
from publisher.packager.adapter import (
    _actual_cost_breakdown,
    _assessment_contract,
    _candidate_contracts,
    _forecast_confidence,
    _forward_cost_breakdown,
    _photon_config_observation,
    _scenario_contracts,
    _workload_profile,
    compute_reevaluation,
    derive_observed_change,
)
from publisher.packager.estimator.package import _package_economics
from publisher.packager.evidence import _build_evidence_registry
from publisher.packager.execution_plan import _recommended_plan
from publisher.packager.scenarios import ScenarioPlanner


def _build_locked_recommendation_package(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    scenarios: Sequence[Scenario],
    thresholds: Thresholds,
) -> rc.RecommendationPackage:
    """Build one immutable Reporter JSON Contract package from reviewed decisions."""
    ep = e.current_epoch
    start_time = _iso_z(ep.epoch_start)
    contract_epoch_id = rc.make_epoch_id(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, start_time=start_time,
        warehouse_type=ep.config.warehouse_type, warehouse_size=ep.config.warehouse_size,
        min_clusters=ep.config.min_clusters, max_clusters=ep.config.max_clusters,
        auto_stop_minutes=ep.config.auto_stop_minutes, warehouse_channel=ep.config.warehouse_channel,
    )
    evidence_registry, finding_map, compatibility_map, shared_cost_refs = _build_evidence_registry(
        e, candidates, scenarios, contract_epoch_id
    )
    candidate_contracts, candidates_by_internal_id = _candidate_contracts(
        e, candidates, contract_epoch_id, evidence_registry, finding_map, shared_cost_refs
    )
    scenario_contracts, _ = _scenario_contracts(
        e, scenarios, contract_epoch_id, candidates_by_internal_id, candidate_contracts, compatibility_map
    )
    reevaluation = compute_reevaluation(e, thresholds, start_time=start_time, epoch_age=epoch_age_days(ep, e.analysis_time))
    recommended = _recommended_plan(scenario_contracts, reevaluation, candidate_contracts)
    package_economics = _package_economics(e, scenario_contracts, recommended.scenario_id)
    evidence_fingerprint = rc.make_evidence_fingerprint(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, epoch_id=contract_epoch_id,
        evidence_records=[rc.to_primitive(x) for x in evidence_registry.values()],
    )
    current_age = epoch_age_days(ep, e.analysis_time)
    actual = _actual_cost_breakdown(e)
    forward = _forward_cost_breakdown(e)
    forecast_assumptions = [
        "Current configuration epoch is the decision baseline.",
        ("Forward DBU cost averages whole run-to-run billing cycles of this periodic current-epoch workload."
         if (e.forecast_metadata or {}).get("forecast_model") == "CADENCE" else
         "Forward DBU cost uses the deterministic current-epoch run-rate model."),
        "AWS forward cost is modeled from observed current-epoch resource time and the configured regional price registry when available.",
        "DBU cost is priced at the account's list price; negotiated discounts lower real savings in proportion.",
        "AWS cost is priced at on-demand EC2 rates; Spot, Reserved Instances and Savings Plans are not modeled.",
    ]
    source_regions = sorted({x for x in e.source_region.split(",") if x}) or ["unknown"]
    provenance = rc.Provenance(
        contract_version=rc.CONTRACT_VERSION, policy_version=POLICY_VERSION, analyzer_version=ANALYZER_VERSION,
        optimizer_version=OPTIMIZER_VERSION, modeler_version=MODEL_VERSION, estimator_version=ESTIMATOR_VERSION,
        bundle_estimator_version=BUNDLE_MODEL_VERSION, reviewer_version=REVIEWER_VERSION,
        decision_graph_version=DECISION_VERSION, technique_catalog_version=TECHNIQUE_CATALOG_VERSION,
        evidence_fingerprint=evidence_fingerprint,
        source_tables=[
            "system.compute.warehouses", "system.compute.warehouse_events", "system.query.history",
            "system.billing.usage", "system.billing.list_prices",
        ],
        source_regions=source_regions, generated_at=_iso_z(e.analysis_time), package_hash_full_sha256=None,
    )
    package = rc.RecommendationPackage(
        recommendation_package_id="PKG-" + "0" * rc.HASH_HEX_LENGTH,
        contract_version=rc.CONTRACT_VERSION,
        warehouse=rc.WarehouseIdentity(
            workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, warehouse_name=e.warehouse_name,
            owner=e.attribution.owner if e.attribution else None, region=e.source_region,
            current_epoch=rc.CurrentEpoch(contract_epoch_id, start_time, current_age, True),
        ),
        current_config=rc.CurrentConfig(
            warehouse_type=ep.config.warehouse_type, warehouse_size=ep.config.warehouse_size,
            min_clusters=ep.config.min_clusters, max_clusters=ep.config.max_clusters,
            auto_stop_minutes=ep.config.auto_stop_minutes, warehouse_channel=ep.config.warehouse_channel,
            photon=_photon_config_observation(e),
        ),
        workload_profile=_workload_profile(e, thresholds),
        cost_baseline=rc.CostBaseline(
            currency=e.currency, actual_365d=actual, forward_365d=forward,
            forecast=rc.ForecastInfo(MODEL_VERSION, _forecast_confidence(e, thresholds), forecast_assumptions),
        ),
        assessment=_assessment_contract(e, candidate_contracts, scenario_contracts, thresholds),
        evidence=evidence_registry, candidates=candidate_contracts, scenarios=scenario_contracts,
        recommended_plan=recommended, package_economics=package_economics,
        realization=rc.Realization(
            status=rc.RealizationStatus.NOT_APPLIED, realization_records=[],
            aggregate=rc.RealizationAggregate(0, 0, 0, e.currency, 0.0, 0.0),
        ),
        provenance=provenance,
        observed_change=derive_observed_change(e),
        carried_forward_plan=None,  # Stage 2 (carried-forward preview) is reserved and not produced yet
    )
    package = dataclasses.replace(package, recommendation_package_id=package.expected_package_id())
    package = dataclasses.replace(
        package, provenance=dataclasses.replace(package.provenance, package_hash_full_sha256=package.full_package_hash())
    )
    package.validate()
    return package


def plan(reviewed: CandidateReviewResult, thresholds: Thresholds) -> List[WarehouseOptimization]:
    """Scenarios for each warehouse whose candidates passed the candidate gate, in order."""
    results: List[WarehouseOptimization] = []
    for w in reviewed.accepted:
        e, candidates = w.evidence, w.candidates
        planner = ScenarioPlanner(thresholds)
        scenarios, recommended = planner.scenarios(e, candidates)
        status_counts: Dict[str, int] = {}
        for candidate in candidates:
            status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1
        LOGGER.info(
            "warehouse.decisions workspace_id=%s warehouse_id=%s statuses=%s scenarios=%d recommended_scenario=%s",
            e.workspace_id, e.warehouse_id, canonical_json(status_counts), len(scenarios), recommended or "NONE",
        )
        results.append(WarehouseOptimization(e.warehouse_id, e.workspace_id, e, candidates, scenarios, recommended))
    return results


def assemble(
    analysis: FleetAnalysis, optimizations: Sequence[WarehouseOptimization], thresholds: Thresholds
) -> List[rc.RecommendationPackage]:
    """Assemble one locked public package per reviewed, planned warehouse."""
    evidence = analysis.evidence
    evidence_by_key = {(e.workspace_id, e.warehouse_id): e for e in evidence}
    packages: List[rc.RecommendationPackage] = []
    for result in optimizations:
        e = evidence_by_key[(result.workspace_id, result.warehouse_id)]
        package = _build_locked_recommendation_package(e, result.candidates, result.scenarios, thresholds)
        packages.append(package)
        LOGGER.info(
            "contract.v1.package.valid workspace_id=%s warehouse_id=%s package_id=%s candidates=%d scenarios=%d",
            e.workspace_id, e.warehouse_id, package.recommendation_package_id, len(package.candidates), len(package.scenarios),
        )
    LOGGER.info(
        "pipeline.done warehouses=%d packages=%d candidates=%d scenarios=%d",
        len(evidence), len(packages), sum(len(p.candidates) for p in packages), sum(len(p.scenarios) for p in packages),
    )
    return packages


def run(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult, candidates: CandidateReviewResult) -> PackagingResult:
    """Scenarios, stateful scenario estimates, selection and the canonical package for every reviewed warehouse."""
    optimizations = plan(candidates, context.thresholds)
    return PackagingResult(optimizations, assemble(analysis, optimizations, context.thresholds))
