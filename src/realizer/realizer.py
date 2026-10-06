#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.0.0: the engine.

Working copy of the frozen single-file oracle (``legacy/realizer_single_file/realizer.py``) with the same behavior.
The realizer reorganization (docs/architecture/realizer-repo-reorg-design.md, section 22) moves its parts into the
capability subpackages phase by phase; so far the contracts (``realizer.contracts``), settings and loading
(``realizer.config``), the shared helpers (``realizer.common``), the Analyzer (``realizer.analyzer``) and the Governor
(``realizer.governor``) are extracted, and this module imports them. ``realize`` runs the Analyzer and the Governor and
the rest of the engine consumes their results.

The engine never mutates a RecommendationPackage (PKG-*): it appends immutable ledger facts (RLE-*) and projects a
separate RealizationPackage (RPKG-*) / snapshot (RSNAP-*). The execution path is replay/offline friendly: a
validated PKG plus a normalized post-change observation is enough to exercise the entire application -> validation
-> economics -> attribution -> regeneration loop. Live Databricks collection can feed the same normalized
observation contract.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt  # noqa: F401  (the engine keeps its module attributes; tests use realizer.dt)
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer import analyzer
from realizer.analyzer.analysis import (
    candidate_by_id,
    candidate_steps,
    config_hash,
    observed_config,
    package_config,
    recommended_scenario,
    scenario_by_id,
)
from realizer.analyzer.collector.collector import demo_observation, load_observation
from realizer.common.logging import LOGGER, setup_logging
from realizer.common.utils import decimal, money, ratio_string, utc_iso, write_json
from realizer.config.loader import load_package, load_policy_mapping, validate_package_shape
from realizer.config.settings import (
    ANALYZER_GATEWAY_VERSION,
    ATTRIBUTION_VERSION,
    ESTIMATOR_GATEWAY_VERSION,
    EXCLUSIVITY_VERSION,
    MODELER_GATEWAY_VERSION,
    POLICY_VERSION,
    PRODUCT_VERSION,
    PROJECTOR_VERSION,
    REALIZER_VERSION,
    SIZE_ORDER,
)
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import (
    make_app_id,
    make_measurement_id,
    make_rpkg_id,
    make_val_id,
)
from realizer.contracts.request import RealizationRequest, RunContext
from realizer.governor import governor
from realizer.governor.policy import (
    RealizerPolicy,
    authority_eligible,
    economic_quality_result,
    readiness,
)
from realizer.packager.ledger import ledger_path, load_ledger

# The engine keeps every name it had; the extracted ones stay importable from here (``realizer.realizer.<name>``).
from realizer.analyzer.analysis import normalize_scalar, photon_bool_from_package  # noqa: F401,E402
from realizer.governor.policy import counterfactual_eligible, policy_to_dict, savings_improvement_material, validate_performance  # noqa: F401,E402
from realizer.packager.ledger import latest_event  # noqa: F401,E402
from realizer.common.utils import load_json  # noqa: F401,E402
from realizer.config.settings import CONFIG_KNOBS  # noqa: F401,E402
from realizer.contracts.realization import make_evidence_id  # noqa: F401,E402


# ---------------------------------------------------------------------------
# Canonical helpers / identities
# ---------------------------------------------------------------------------


def make_cf_id(app_id: str, observation: Mapping[str, Any], package: Mapping[str, Any], policy: RealizerPolicy) -> str:
    return rc.content_id("CF", {
        "app": app_id,
        "old_config_hash": config_hash(package_config(package)),
        "post_change_workload": observation.get("workload", {}),
        "window": [observation.get("window_start_utc"), observation.get("window_end_utc") or observation.get("observed_at_utc")],
        "modeler_version": MODELER_GATEWAY_VERSION,
        "policy_snapshot_id": policy.snapshot_id,
    })


def ledger_business_key(event_type: rc.EventType, payload: Mapping[str, Any]) -> Dict[str, Any]:
    keys = [
        "application_episode_id", "validation_episode_id", "measurement_id", "scenario_id", "step",
        "effective_from_utc", "window_start_utc", "window_end_utc", "counterfactual_id",
        "regeneration_request_id", "replacement_package_id", "correction_key",
    ]
    return {k: payload.get(k) for k in keys if payload.get(k) is not None}


def make_ledger_entry(
    *, package: Mapping[str, Any], rpkg_id: str, event_type: rc.EventType,
    effective_at_utc: str, recorded_at_utc: str, payload: Mapping[str, Any],
    evidence_refs: Sequence[str] = (), application_episode_id: Optional[str] = None,
    validation_episode_id: Optional[str] = None, measurement_id: Optional[str] = None,
    caused_by_entry_id: Optional[str] = None, corrects_entry_id: Optional[str] = None,
) -> rc.RealizationLedgerEntry:
    payload_dict = copy.deepcopy(dict(payload))
    business_key = ledger_business_key(event_type, {
        **payload_dict,
        "application_episode_id": application_episode_id,
        "validation_episode_id": validation_episode_id,
        "measurement_id": measurement_id,
    })
    eid = rc.content_id("RLE", {
        "kind": "REALIZATION_LEDGER_ENTRY",
        "ledger_major": rc.LEDGER_CONTRACT_VERSION.split(".")[0],
        "rpkg": rpkg_id,
        "event_type": event_type.value,
        "domain_business_key": business_key,
        "payload": payload_dict,
    })
    w = package["warehouse"]
    entry = rc.RealizationLedgerEntry(
        ledger_entry_id=eid,
        ledger_contract_version=rc.LEDGER_CONTRACT_VERSION,
        realization_package_id=rpkg_id,
        recommendation_package_id=package["recommendation_package_id"],
        event_type=event_type,
        effective_at_utc=utc_iso(effective_at_utc),
        recorded_at_utc=utc_iso(recorded_at_utc),
        workspace_id=str(w["workspace_id"]),
        warehouse_id=str(w["warehouse_id"]),
        application_episode_id=application_episode_id,
        validation_episode_id=validation_episode_id,
        measurement_id=measurement_id,
        payload=payload_dict,
        evidence_refs=list(evidence_refs),
        caused_by_entry_id=caused_by_entry_id,
        corrects_entry_id=corrects_entry_id,
        provenance={"product_version": PRODUCT_VERSION, "realizer_version": REALIZER_VERSION, "policy_version": POLICY_VERSION},
    )
    entry.validate()
    return entry


# ---------------------------------------------------------------------------
# Application matching / plan authority
# ---------------------------------------------------------------------------


def _directional_relation(source: Any, recommended: Any, observed: Any) -> Tuple[rc.ImplementationConformance, Optional[str]]:
    try:
        s, r, o = Decimal(str(source)), Decimal(str(recommended)), Decimal(str(observed))
    except Exception:
        return rc.ImplementationConformance.UNRELATED, None
    if o == s:
        return rc.ImplementationConformance.NOT_APPLIED, None
    if o == r:
        return rc.ImplementationConformance.EXACT, None
    d_rec, d_obs = r - s, o - s
    if d_rec == 0:
        return rc.ImplementationConformance.UNRELATED, None
    if (d_rec > 0 and d_obs > 0) or (d_rec < 0 and d_obs < 0):
        relation = "LESS_AGGRESSIVE" if abs(d_obs) < abs(d_rec) else "MORE_AGGRESSIVE" if abs(d_obs) > abs(d_rec) else "ALTERNATE_SAME_INTENT"
        return rc.ImplementationConformance.MODIFIED, relation
    return rc.ImplementationConformance.CONTRARY, None


def compare_step(step: Mapping[str, Any], obs_config: Mapping[str, Any]) -> Tuple[Optional[rc.ImplementationConformance], Optional[str]]:
    knob = str(step.get("knob"))
    source = step.get("current_value")
    recommended = step.get("recommended_value")
    observed = obs_config.get(knob)
    if observed is None:
        return None, None
    if observed == recommended:
        return rc.ImplementationConformance.EXACT, None
    if observed == source:
        return rc.ImplementationConformance.NOT_APPLIED, None
    if knob in {"min_clusters", "max_clusters", "auto_stop_minutes"}:
        return _directional_relation(source, recommended, observed)
    if knob == "warehouse_size":
        s, r, o = (SIZE_ORDER.get(str(x).upper()) for x in (source, recommended, observed))
        if None in {s, r, o}:
            return rc.ImplementationConformance.UNRELATED, None
        return _directional_relation(s, r, o)
    if knob == "enable_photon":
        # No third ordinary boolean target exists. Mixed/unknown evidence must be
        # normalized upstream rather than guessed here.
        if isinstance(observed, bool):
            return rc.ImplementationConformance.CONTRARY, None
        return None, None
    if knob in {"warehouse_type", "warehouse_channel"}:
        # Semantic enums do not have a universal ordinal. A third state is a
        # modified alternate transition only when the observation explicitly
        # labels it as such; absent that evidence, classify as unrelated.
        return rc.ImplementationConformance.UNRELATED, None
    return rc.ImplementationConformance.UNRELATED, None


def expected_annual_for_exact_step(package: Mapping[str, Any], step: Mapping[str, Any]) -> Tuple[Optional[Decimal], str]:
    c = candidate_by_id(package, step.get("candidate_id"))
    if not c:
        return None, "UNAVAILABLE"
    econ = c.get("independent_economics") or {}
    savings = econ.get("savings_365d") or {}
    total = savings.get("total")
    dbu = savings.get("dbu")
    value = (total or dbu or {}).get("base") if isinstance(total or dbu, Mapping) else None
    return decimal(value), "PUBLISHER_CANDIDATE" if value is not None else "UNAVAILABLE"


def choose_application(
    package: Mapping[str, Any], obs_config: Mapping[str, Any], *, scenario_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    primary: Optional[Dict[str, Any]] = None
    scenario = scenario_by_id(package, scenario_id) if scenario_id else recommended_scenario(package)
    sid = None if scenario is None else scenario.get("scenario_id")
    for step in candidate_steps(package, sid):
        conf, relation = compare_step(step, obs_config)
        item = {
            "scenario_id": sid,
            "step": int(step.get("step", 0)),
            "candidate_id": step.get("candidate_id"),
            "technique_id": step.get("technique_id"),
            "knob": step.get("knob"),
            "execution_state": step.get("execution_state"),
            "authority_eligible": authority_eligible(step),
            "source_value": step.get("current_value"),
            "recommended_value": step.get("recommended_value"),
            "observed_value": obs_config.get(step.get("knob")),
            "implementation_conformance": None if conf is None else conf.value,
            "modification_relation": relation,
            "invalidates": list(step.get("invalidates") or []),
            "dependencies": copy.deepcopy(step.get("dependencies") or []),
            "remeasure_days": ((step.get("validation") or {}).get("remeasure_days")),
        }
        results.append(item)
        if primary is None and item["authority_eligible"] and conf in {
            rc.ImplementationConformance.EXACT,
            rc.ImplementationConformance.MODIFIED,
            rc.ImplementationConformance.CONTRARY,
        }:
            primary = item
    return results, primary


# ---------------------------------------------------------------------------
# Ledger persistence (appending; the read side is realizer.packager.ledger)
# ---------------------------------------------------------------------------


def append_if_absent(path: Path, entry: rc.RealizationLedgerEntry) -> bool:
    existing = {x.ledger_entry_id for x in load_ledger(path)}
    if entry.ledger_entry_id in existing:
        LOGGER.info("ledger.duplicate_suppressed rle=%s", entry.ledger_entry_id)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(rc.canonical_json(entry.to_dict()) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    LOGGER.info("ledger.append event=%s rle=%s", entry.event_type.value, entry.ledger_entry_id)
    return True


def ledger_checkpoint(entries: Sequence[rc.RealizationLedgerEntry]) -> Dict[str, Any]:
    ordered = sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))
    ids = [e.ledger_entry_id for e in ordered]
    digest = "SHA256:" + rc.sha256_hex({"ordered_entry_ids": ids})
    return {
        "entry_count": len(ordered),
        "first_entry_id": ids[0] if ids else None,
        "latest_entry_id": ids[-1] if ids else None,
        "latest_recorded_at_utc": ordered[-1].recorded_at_utc if ordered else None,
        "latest_effective_at_utc": max((e.effective_at_utc for e in ordered), default=None),
        "ledger_checkpoint_hash": digest,
    }


# ---------------------------------------------------------------------------
# Economics / attribution (the policy checks are realizer.governor.policy)
# ---------------------------------------------------------------------------


def calculate_economics(
    package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any],
    policy: RealizerPolicy, validation_passed: bool, counterfactual_claim_eligible: bool = True,
) -> Dict[str, Any]:
    econ = observation.get("economics") or {}
    cf = decimal(econ.get("counterfactual_old_cost"))
    actual = decimal(econ.get("observed_new_cost"))
    if cf is None or actual is None:
        return {
            "available": False,
            "currency": econ.get("currency"),
            "observed_economic_delta": None,
            "successful_realized_savings": None,
            "annualized_realized_savings": None,
            "expected_same_scope_annual_savings": None,
            "denominator_source": "UNAVAILABLE",
            "realization_ratio": None,
            "classification_detail": "ECONOMICS_UNAVAILABLE",
        }
    realized = cf - actual
    period_days = decimal(econ.get("period_days")) or Decimal(str(max(1, int(observation.get("elapsed_days", 1) or 1))))
    representative = readiness(observation, policy)["ready"]
    annualized = realized * Decimal("365") / period_days if representative and period_days > 0 else None

    conformance = app.get("implementation_conformance")
    expected: Optional[Decimal] = None
    denom_source = "UNAVAILABLE"
    supplied = decimal(econ.get("expected_annual_savings_actual_scope"))
    if app.get("application_scope") == "SCENARIO":
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
        elif conformance == rc.ImplementationConformance.EXACT.value:
            scn = scenario_by_id(package, app.get("scenario_id"))
            total = (((scn or {}).get("portfolio_economics") or {}).get("savings_365d") or {}).get("total") or {}
            expected = decimal(total.get("base"))
            if expected is not None:
                denom_source = "PUBLISHER_SCENARIO"
    elif conformance == rc.ImplementationConformance.MODIFIED.value:
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
    elif conformance == rc.ImplementationConformance.EXACT.value:
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
        else:
            step = next((st for st in candidate_steps(package, app.get("scenario_id")) if int(st.get("step", 0)) == int(app.get("step", -1))), None)
            if step is not None and authority_eligible(step):
                expected, denom_source = expected_annual_for_exact_step(package, step)

    ratio: Optional[Decimal] = None
    if annualized is not None and expected is not None and expected > 0:
        ratio = annualized / expected

    if not validation_passed:
        state, detail = rc.RealizationState.VALIDATION_FAILED.value, "HARD_GUARDRAIL_FAILED"
        successful = Decimal("0")
    elif not counterfactual_claim_eligible:
        # Retain source actual/counterfactual diagnostics but do not convert an
        # indefensible/correlated-only counterfactual into successful realized
        # value. This is the fail-closed REAL-INV-009 path.
        state, detail = rc.RealizationState.BLOCKED.value, "COUNTERFACTUAL_NOT_CLAIM_ELIGIBLE"
        successful = Decimal("0")
    elif ratio is None:
        state, detail = rc.RealizationState.MONITORING.value, "VALIDATED_VALUE_UNCLASSIFIED"
        successful = realized
    elif ratio >= policy.realized_ratio_min:
        state, detail = rc.RealizationState.REALIZED.value, "REALIZED"
        successful = realized
    else:
        state = rc.RealizationState.UNDER_REALIZED.value
        detail = "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED" if ratio < policy.under_realized_ratio_min else "UNDER_REALIZED"
        successful = realized

    return {
        "available": True,
        "currency": str(econ.get("currency") or "USD"),
        "economic_basis": str(econ.get("economic_basis") or "ECONOMIC_COST"),
        "counterfactual_old_cost": money(cf),
        "observed_new_cost": money(actual),
        "realized_savings_period": money(realized),
        "realized_savings_since_application": money(econ.get("realized_savings_since_application") if econ.get("realized_savings_since_application") is not None else realized),
        "observed_economic_delta": money(realized),
        "successful_realized_savings": money(successful),
        "annualized_realized_savings": money(annualized),
        "mature_ttm_realized_savings": money(econ.get("mature_ttm_realized_savings")),
        "expected_same_scope_annual_savings": money(expected),
        "denominator_source": denom_source,
        "realization_ratio": ratio_string(ratio),
        "realization_state": state,
        "classification_detail": detail,
        "reoptimization_recommended": detail == "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED",
        "dbu_realized": money(econ.get("dbu_realized")),
        "aws_economic_realized": money(econ.get("aws_economic_realized")),
        "aws_cash_realized": money(econ.get("aws_cash_realized")),
        "aws_commitment_freed": money(econ.get("aws_commitment_freed")),
        "aws_evidence_type": econ.get("aws_evidence_type"),
        "period_days": rc.canonical_decimal(period_days),
    }


def attribution_result(app: Mapping[str, Any], observation: Mapping[str, Any], validation_passed: bool, cf_claim_eligible: bool, economic: Mapping[str, Any], policy: RealizerPolicy) -> Dict[str, Any]:
    try:
        origin = rc.OriginAttribution(str(observation.get("origin_attribution") or "UNKNOWN").upper())
    except ValueError:
        origin = rc.OriginAttribution.UNKNOWN
    overlap = int(observation.get("overlap_count", 0) or 0)
    eligible_conf = app.get("implementation_conformance") in {rc.ImplementationConformance.EXACT.value, rc.ImplementationConformance.MODIFIED.value}
    hard: List[str] = []
    quality = economic.get("quality_gates") or economic_quality_result(observation, policy)
    if not quality.get("attributable_spend", {}).get("pass"):
        hard.append("MATERIAL_SPEND_UNRECONCILED")
    if not quality.get("price_coverage", {}).get("pass"):
        hard.append("PRICE_COVERAGE_INCOMPLETE")
    if not quality.get("currency_consistency", {}).get("pass"):
        hard.append("CURRENCY_MISMATCH")
    if not quality.get("correction_chain", {}).get("pass"):
        hard.append("CORRECTION_CHAIN_INCOMPLETE")
    if not quality.get("material_cost_components", {}).get("pass"):
        hard.append("MATERIAL_COST_COMPONENT_UNAVAILABLE")
    if not eligible_conf:
        hard.append("APPLIED_STATE_UNMATCHED")
    if not validation_passed:
        hard.append("HARD_VALIDATION_FAILURE")
    if not cf_claim_eligible:
        hard.append("NO_DEFENSIBLE_COUNTERFACTUAL")
    if overlap > 0:
        hard.append("PORTFOLIO_SCOPE_OVERLAP")
    if origin in {rc.OriginAttribution.INDEPENDENT, rc.OriginAttribution.NOT_ATTRIBUTABLE}:
        hard.append("ORIGIN_NOT_ATTRIBUTABLE")
    confirmed_ok = origin == rc.OriginAttribution.CONFIRMED or not policy.require_confirmed_origin_for_hard_kpi
    claimable = not hard and confirmed_ok and economic.get("available") and quality.get("pass")
    if claimable:
        claim = rc.AttributionClaimStatus.CLAIMABLE
        state = rc.AttributionState.HIGH_CONFIDENCE
    elif economic.get("available") and validation_passed and cf_claim_eligible and origin not in {rc.OriginAttribution.INDEPENDENT, rc.OriginAttribution.NOT_ATTRIBUTABLE}:
        claim = rc.AttributionClaimStatus.OBSERVABLE_ONLY
        state = rc.AttributionState.PARTIAL_ATTRIBUTION if origin == rc.OriginAttribution.INFERRED else rc.AttributionState.CORRELATED_ONLY
    else:
        claim = rc.AttributionClaimStatus.NOT_ATTRIBUTABLE
        state = rc.AttributionState.NOT_ATTRIBUTABLE
    successful = decimal(economic.get("successful_realized_savings")) or Decimal("0")
    return {
        "origin_attribution": origin.value,
        "attribution_scope": str(app.get("application_scope") or observation.get("attribution_scope") or "CANDIDATE"),
        "attribution_state": state.value,
        "attribution_claim_status": claim.value,
        "hard_disqualifiers": hard,
        "overlap_count": overlap,
        "economic_quality_gates": quality,
        "validated_observed_realization": economic.get("successful_realized_savings"),
        "recommendation_attributed_realized_savings": money(successful if claimable else Decimal("0")),
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def project_snapshot(package: Mapping[str, Any], entries: Sequence[rc.RealizationLedgerEntry]) -> rc.RealizationPackage:
    ordered = sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))
    cp = ledger_checkpoint(ordered)
    if not ordered:
        raise ValueError("cannot project empty ledger")
    rpkg = make_rpkg_id(package["recommendation_package_id"])
    snap_id = rc.content_id("RSNAP", {
        "kind": "REALIZATION_SNAPSHOT",
        "rpkg": rpkg,
        "ledger_checkpoint_hash": cp["ledger_checkpoint_hash"],
        "projector_version": PROJECTOR_VERSION,
    })
    app_events = [e for e in ordered if e.event_type in {rc.EventType.APPLICATION_OBSERVED, rc.EventType.MODIFIED_APPLICATION_OBSERVED, rc.EventType.INDEPENDENT_CHANGE_OBSERVED}]
    auth_apps = [e for e in app_events if e.payload.get("authority_eligible")]
    app = auth_apps[-1] if auth_apps else None
    validation = latest_event(ordered, rc.EventType.VALIDATION_COMPLETED)
    measurement = latest_event(ordered, rc.EventType.ECONOMIC_MEASUREMENT_RECORDED)
    drift = latest_event(ordered, rc.EventType.DRIFT_DETECTED)
    rollback = latest_event(ordered, rc.EventType.ROLLBACK_OBSERVED)
    regen = latest_event(ordered, rc.EventType.REGENERATION_REQUESTED, rc.EventType.REPLACEMENT_PACKAGE_LINKED)
    superseded = latest_event(ordered, rc.EventType.SOURCE_PACKAGE_SUPERSEDED)

    if rollback and (not app or rollback.recorded_at_utc >= app.recorded_at_utc):
        app_state = rc.ApplicationState.ROLLED_BACK.value
    elif drift and app and drift.recorded_at_utc >= app.recorded_at_utc:
        app_state = rc.ApplicationState.DRIFTED.value
    elif app:
        app_state = rc.ApplicationState.APPLIED.value
    else:
        app_state = rc.ApplicationState.NOT_APPLIED.value

    projected_scenario_id = app.payload.get("scenario_id") if app else (package.get("recommended_plan") or {}).get("scenario_id")
    steps = candidate_steps(package, projected_scenario_id)
    applied_refs = {(e.payload.get("scenario_id"), e.payload.get("step")) for e in auth_apps if e.payload.get("implementation_conformance") in {"EXACT", "MODIFIED"}}
    eligible_steps = [s for s in steps if authority_eligible(s)]
    if app and app.payload.get("application_scope") == "SCENARIO":
        applied_step_refs = {(x.get("scenario_id"), x.get("step")) for x in app.payload.get("applied_step_refs", [])}
        applied_count = sum(1 for st in eligible_steps if (projected_scenario_id, st.get("step")) in applied_step_refs)
    else:
        applied_count = sum(1 for st in eligible_steps if (projected_scenario_id, st.get("step")) in applied_refs)
    pending_count = sum(1 for s in steps if not authority_eligible(s))
    if applied_count == 0:
        plan_progress = rc.PlanProgressState.NOT_STARTED.value
    elif applied_count == len(eligible_steps) and pending_count == 0:
        plan_progress = rc.PlanProgressState.COMPLETED.value
    else:
        plan_progress = rc.PlanProgressState.PARTIALLY_COMPLETED.value

    val_state = rc.ValidationState.NOT_READY.value
    if validation:
        val_state = rc.ValidationState.PASSED.value if validation.payload.get("hard_guardrails_pass") else rc.ValidationState.FAILED.value
    elif latest_event(ordered, rc.EventType.VALIDATION_STARTED):
        val_state = rc.ValidationState.RUNNING.value
    elif latest_event(ordered, rc.EventType.VALIDATION_READY):
        val_state = rc.ValidationState.READY.value

    if app_state == rc.ApplicationState.ROLLED_BACK.value:
        realization_state = rc.RealizationState.ROLLED_BACK.value
    elif app_state == rc.ApplicationState.DRIFTED.value:
        realization_state = rc.RealizationState.DRIFTED.value
    elif validation and not validation.payload.get("hard_guardrails_pass"):
        realization_state = rc.RealizationState.VALIDATION_FAILED.value
    elif measurement:
        realization_state = str(measurement.payload.get("realization_state") or rc.RealizationState.MONITORING.value)
    elif app:
        realization_state = rc.RealizationState.VALIDATING.value if val_state in {"READY", "RUNNING"} else rc.RealizationState.APPLIED.value
    else:
        realization_state = rc.RealizationState.NOT_APPLIED.value

    if not app:
        maturity = rc.MaturityState.NOT_APPLIED.value
    elif measurement and validation and validation.payload.get("readiness", {}).get("ready"):
        maturity = rc.MaturityState.MATURE.value if measurement.payload.get("mature_ttm_realized_savings") is not None else rc.MaturityState.REPRESENTATIVE.value
    elif measurement:
        maturity = rc.MaturityState.PROVISIONAL.value
    else:
        maturity = rc.MaturityState.COLLECTING.value

    attr = (measurement.payload.get("attribution") if measurement else None) or {
        "origin_attribution": (app.payload.get("origin_attribution") if app else "UNKNOWN"),
        "attribution_state": "NOT_ATTRIBUTABLE",
        "attribution_claim_status": "NOT_ATTRIBUTABLE",
        "validated_observed_realization": None,
        "recommendation_attributed_realized_savings": "0.00",
    }

    replacement = latest_event(ordered, rc.EventType.REPLACEMENT_PACKAGE_LINKED)
    regen_status = rc.RegenerationStatus.NOT_REQUIRED.value
    if regen:
        regen_status = rc.RegenerationStatus.REPLACED.value if regen.event_type == rc.EventType.REPLACEMENT_PACKAGE_LINKED else rc.RegenerationStatus.REQUESTED.value

    source = {
        "recommendation_package_id": package["recommendation_package_id"],
        "publisher_contract_version": package.get("contract_version"),
        "package_hash_full_sha256": (package.get("provenance") or {}).get("package_hash_full_sha256"),
        "epoch_id": ((package.get("warehouse") or {}).get("current_epoch") or {}).get("epoch_id"),
        "recommended_scenario_id": (package.get("recommended_plan") or {}).get("scenario_id"),
        "generated_at_utc": (package.get("provenance") or {}).get("generated_at"),
        "disposition": rc.SourcePackageDisposition.SUPERSEDED.value if superseded else rc.SourcePackageDisposition.ACTIVE.value,
        "superseded_by_package_id": (replacement.payload.get("replacement_package_id") if replacement else None),
    }
    app_section = {
        "state": app_state,
        "plan_progress": {
            "state": plan_progress,
            "authoritative_actionable_steps": len(eligible_steps),
            "applied_steps": applied_count,
            "pending_reevaluation_steps": pending_count,
        },
        "active_episode_id": app.application_episode_id if app else None,
        "application_scope": app.payload.get("application_scope", "CANDIDATE") if app else None,
        "applied_step_refs": app.payload.get("applied_step_refs", []) if app else [],
        "implementation_conformance": app.payload.get("implementation_conformance") if app else "NOT_APPLIED",
        "modification_relation": app.payload.get("modification_relation") if app else None,
        "scenario_id": app.payload.get("scenario_id") if app else None,
        "step": app.payload.get("step") if app else None,
        "knob": app.payload.get("knob") if app else None,
        "source_value": app.payload.get("source_value") if app else None,
        "recommended_value": app.payload.get("recommended_value") if app else None,
        "observed_applied_value": app.payload.get("observed_value") if app else None,
        "observed_config": app.payload.get("observed_config") if app else None,
        "effective_from_utc": app.payload.get("effective_from_utc") if app else None,
        "effective_to_utc": rollback.effective_at_utc if rollback else drift.effective_at_utc if drift else None,
        "effective_time_quality": app.payload.get("effective_time_quality") if app else None,
        "intent_state": app.payload.get("intent_state", "UNKNOWN") if app else "UNKNOWN",
        "actor": app.payload.get("actor") if app else None,
    }
    econ_section = measurement.payload.get("economics") if measurement else {
        "available": False,
        "observed_economic_delta": None,
        "successful_realized_savings": None,
        "annualized_realized_savings": None,
        "mature_ttm_realized_savings": None,
        "expected_same_scope_annual_savings": None,
        "realization_ratio": None,
    }
    result = rc.RealizationPackage(
        contract_version=rc.CONTRACT_VERSION,
        realization_package_id=rpkg,
        snapshot_id=snap_id,
        source_recommendation=source,
        warehouse={
            "workspace_id": package["warehouse"]["workspace_id"],
            "warehouse_id": package["warehouse"]["warehouse_id"],
            "warehouse_name": package["warehouse"].get("warehouse_name"),
            "region": package["warehouse"].get("region"),
        },
        as_of_utc=cp["latest_recorded_at_utc"],
        current_state={
            "realization_state": realization_state,
            "validation_state": val_state,
            "maturity_state": maturity,
            "source_package_disposition": source["disposition"],
        },
        application=app_section,
        validation=(validation.payload if validation else {"state": val_state, "readiness": None, "rules": []}),
        economics=econ_section,
        attribution=attr,
        maturity={"state": maturity},
        drift=(drift.payload if drift else {"state": "NONE"}),
        regeneration={
            "status": regen_status,
            "regeneration_request_id": regen.payload.get("regeneration_request_id") if regen and regen.event_type == rc.EventType.REGENERATION_REQUESTED else None,
            "replacement_package_id": replacement.payload.get("replacement_package_id") if replacement else None,
        },
        history={
            **cp,
            "application_episode_count": len({e.application_episode_id for e in auth_apps if e.application_episode_id}),
            "validation_episode_count": len({e.validation_episode_id for e in ordered if e.validation_episode_id}),
            "measurement_count": len({e.measurement_id for e in ordered if e.measurement_id}),
            "correction_count": sum(1 for e in ordered if e.event_type == rc.EventType.BILLING_CORRECTION_RECORDED),
        },
        provenance={
            "product_version": PRODUCT_VERSION,
            "realizer_version": REALIZER_VERSION,
            "realizer_contract_version": rc.CONTRACT_VERSION,
            "policy_version": POLICY_VERSION,
            "projector_version": PROJECTOR_VERSION,
            "analyzer_gateway_version": ANALYZER_GATEWAY_VERSION,
            "modeler_gateway_version": MODELER_GATEWAY_VERSION,
            "estimator_gateway_version": ESTIMATOR_GATEWAY_VERSION,
            "attribution_version": ATTRIBUTION_VERSION,
            "exclusivity_version": EXCLUSIVITY_VERSION,
        },
    )
    result.validate()
    return result


# ---------------------------------------------------------------------------
# Realization orchestration
# ---------------------------------------------------------------------------


def build_context(request: RealizationRequest) -> RunContext:
    """Validate the source package, apply the policy default and mint the realization package id."""
    validate_package_shape(request.package)
    policy = request.policy or RealizerPolicy()
    return RunContext(request=request, policy=policy, realization_package_id=make_rpkg_id(request.package["recommendation_package_id"]))


def realize(
    package: Mapping[str, Any], observation: Optional[Mapping[str, Any]], output_dir: Path,
    *, policy: Optional[RealizerPolicy] = None, replay_through: Optional[str] = None,
) -> rc.RealizationPackage:
    context = build_context(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
    ))
    rpkg = context.realization_package_id
    lpath = ledger_path(output_dir, rpkg)
    entries = load_ledger(lpath)

    # Stable lineage creation uses Publisher generation time so replay does not
    # depend on the first Realizer wall-clock run.
    generated = (package.get("provenance") or {}).get("generated_at") or ((package.get("warehouse") or {}).get("current_epoch") or {}).get("start_time")
    if not generated:
        generated = "1970-01-01T00:00:00Z"
    created = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.REALIZATION_CREATED,
        effective_at_utc=generated, recorded_at_utc=generated,
        payload={"recommendation_package_id": package["recommendation_package_id"]},
    )
    append_if_absent(lpath, created)

    # The lineage entry is already appended: a malformed observation fails inside the Analyzer after it, as it always
    # did, and the Governor never fails (its checks keep their exceptions until the consumer needs them).
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    if analysis.observation is not None:
        process_observation(context, analysis, governance)

    entries = load_ledger(lpath)
    if replay_through:
        cut = utc_iso(replay_through)
        entries = [e for e in entries if e.recorded_at_utc <= cut]
    snapshot = project_snapshot(package, entries)
    payload = snapshot.to_dict()
    write_json(output_dir / "realization" / "packages" / f"{rpkg}.json", payload)
    write_json(output_dir / "realization" / "snapshots" / f"{snapshot.snapshot_id}.json", payload)
    return snapshot


def process_observation(context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult) -> None:
    package, observation, output_dir, policy = context.request.package, context.request.observation, context.request.output_dir, context.policy
    observed = analysis.observation
    assert observed is not None  # the caller runs this only for a run with an observation
    rpkg = context.realization_package_id
    lpath = ledger_path(output_dir, rpkg)
    obs_time, recorded, effective = observed.observed_at_utc, observed.recorded_at_utc, observed.effective_at_utc
    obs_cfg = observed.observed_config
    source_hash, observed_hash = observed.source_config_hash, observed.observed_config_hash
    revid = observed.evidence_id
    write_json(output_dir / "realization" / "evidence" / f"{revid}.json", observed.evidence_record)

    selected_scenario_id = observation.get("applied_scenario_id") or (package.get("recommended_plan") or {}).get("scenario_id")
    selected_scenario = scenario_by_id(package, selected_scenario_id)
    step_results, primary = choose_application(package, obs_cfg, scenario_id=selected_scenario_id)
    authority_changes = [
        item for item in step_results
        if item.get("authority_eligible") and item.get("implementation_conformance") in {
            rc.ImplementationConformance.EXACT.value,
            rc.ImplementationConformance.MODIFIED.value,
            rc.ImplementationConformance.CONTRARY.value,
        }
    ]
    scenario_scope = bool(
        len(authority_changes) > 1
        or (selected_scenario and selected_scenario.get("scenario_type") == "WAREHOUSE_TYPE_MIGRATION")
        or selected_scenario_id != (package.get("recommended_plan") or {}).get("scenario_id")
    )
    previous_app = analysis.history.previous_application
    material_config_drift = analysis.config_drift_observed
    if material_config_drift:
        drift_payload = {
            "state": "DRIFTED",
            "drift_type": "CONFIGURATION",
            "previous_observed_config_hash": previous_app.payload.get("observed_config_hash"),
            "observed_config_hash": observed_hash,
            "observed_config": obs_cfg,
            "material": True,
            "required_action": "REGENERATE",
        }
        drift = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.DRIFT_DETECTED,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=drift_payload,
            evidence_refs=[revid], application_episode_id=previous_app.application_episode_id,
        )
        append_if_absent(lpath, drift)

    # Record every materially changed candidate relationship; only authority-
    # eligible primary scope can enter recommendation realization. Repeated
    # observations of the same already-applied configuration remain in the same
    # application episode rather than creating a new APP on every daily run.
    active_app_event: Optional[rc.RealizationLedgerEntry] = previous_app if material_config_drift else None
    if not material_config_drift and previous_app and primary and (
        previous_app.payload.get("observed_config_hash") == observed_hash
        and previous_app.payload.get("scenario_id") == primary.get("scenario_id")
        and (scenario_scope or previous_app.payload.get("step") == primary.get("step"))
    ):
        active_app_event = previous_app

    # Multiple compatible authoritative changes (or a migration alternative)
    # are one scenario-scope application episode. This avoids fabricating
    # candidate allocation for inseparable economics.
    if scenario_scope and authority_changes and active_app_event is None:
        scenario_conf = (
            rc.ImplementationConformance.CONTRARY.value
            if any(i["implementation_conformance"] == rc.ImplementationConformance.CONTRARY.value for i in authority_changes)
            else rc.ImplementationConformance.MODIFIED.value
            if any(i["implementation_conformance"] == rc.ImplementationConformance.MODIFIED.value for i in authority_changes)
            else rc.ImplementationConformance.EXACT.value
        )
        app_id = make_app_id(rpkg, str(selected_scenario_id), 0, source_hash, observed_hash, revid)
        invalidates = sorted({k for i in authority_changes for k in (i.get("invalidates") or [])})
        payload = {
            "application_scope": "SCENARIO",
            "scenario_id": selected_scenario_id,
            "step": None,
            "candidate_id": None,
            "technique_id": None,
            "knob": None,
            "execution_state": "SCENARIO_APPLIED",
            "authority_eligible": True,
            "source_value": None,
            "recommended_value": None,
            "observed_value": None,
            "implementation_conformance": scenario_conf,
            "modification_relation": None,
            "invalidates": invalidates,
            "dependencies": [],
            "applied_step_refs": [{"scenario_id": i["scenario_id"], "step": i["step"], "conformance": i["implementation_conformance"]} for i in authority_changes],
            "application_episode_id": app_id,
            "source_config_hash": source_hash,
            "observed_config_hash": observed_hash,
            "observed_config": obs_cfg,
            "effective_from_utc": effective,
            "effective_time_quality": str(observation.get("effective_time_quality") or "OBSERVED_AT_ONLY"),
            "intent_state": str(observation.get("intent_state") or "UNKNOWN").upper(),
            "origin_attribution": str(observation.get("origin_attribution") or "UNKNOWN").upper(),
            "actor": observation.get("actor"),
        }
        et = rc.EventType.MODIFIED_APPLICATION_OBSERVED if scenario_conf == rc.ImplementationConformance.MODIFIED.value else rc.EventType.APPLICATION_OBSERVED
        event = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=et,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=payload,
            evidence_refs=[revid], application_episode_id=app_id,
        )
        append_if_absent(lpath, event)
        active_app_event = event

    for item in step_results:
        conf = item.get("implementation_conformance")
        if conf in {None, rc.ImplementationConformance.NOT_APPLIED.value}:
            continue
        app_id = make_app_id(rpkg, str(item.get("scenario_id")), int(item.get("step") or 0), source_hash, observed_hash, revid)
        payload = {
            **item,
            "application_episode_id": app_id,
            "source_config_hash": source_hash,
            "observed_config_hash": observed_hash,
            "observed_config": obs_cfg,
            "effective_from_utc": effective,
            "effective_time_quality": str(observation.get("effective_time_quality") or "OBSERVED_AT_ONLY"),
            "intent_state": str(observation.get("intent_state") or "UNKNOWN").upper(),
            "origin_attribution": str(observation.get("origin_attribution") or "UNKNOWN").upper(),
            "actor": observation.get("actor"),
        }
        if material_config_drift and item["authority_eligible"]:
            continue
        if scenario_scope and item["authority_eligible"]:
            continue
        if (
            previous_app is not None
            and active_app_event is previous_app
            and item["authority_eligible"]
            and primary
            and item.get("step") == primary.get("step")
        ):
            # Stable state observation: reuse the active APP. Measurement and
            # validation events below still record the new observation window.
            continue
        if not item["authority_eligible"]:
            et = rc.EventType.INDEPENDENT_CHANGE_OBSERVED
        elif conf == rc.ImplementationConformance.MODIFIED.value:
            et = rc.EventType.MODIFIED_APPLICATION_OBSERVED
        else:
            et = rc.EventType.APPLICATION_OBSERVED
        event = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=et,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=payload,
            evidence_refs=[revid], application_episode_id=app_id,
        )
        append_if_absent(lpath, event)
        if item["authority_eligible"] and primary and item.get("step") == primary.get("step"):
            active_app_event = event

    intent = str(observation.get("intent_state") or "UNKNOWN").upper()
    if intent != "UNKNOWN":
        ie = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.INTENT_RECORDED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"intent_state": intent, "actor": observation.get("actor"), "reference": observation.get("intent_reference")},
            evidence_refs=[], application_episode_id=active_app_event.application_episode_id if active_app_event else None,
        )
        append_if_absent(lpath, ie)

    # Explicit rollback can be requested/observed independently of a new PKG.
    if observation.get("rollback_requested"):
        rb = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ROLLBACK_REQUESTED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"reason": observation.get("rollback_reason")},
            evidence_refs=[revid], application_episode_id=(active_app_event or previous_app).application_episode_id if (active_app_event or previous_app) else None,
        )
        append_if_absent(lpath, rb)
    if observation.get("rollback_observed"):
        rb = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ROLLBACK_OBSERVED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"observed_config": obs_cfg, "reason": observation.get("rollback_reason")},
            evidence_refs=[revid], application_episode_id=(active_app_event or previous_app).application_episode_id if (active_app_event or previous_app) else None,
        )
        append_if_absent(lpath, rb)

    # Replacement/supersession events are explicit; package generation alone is
    # not inferred from observation time.
    replacement_pkg = observation.get("replacement_package_id")
    if replacement_pkg:
        se = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.SOURCE_PACKAGE_SUPERSEDED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"replacement_package_id": replacement_pkg}, evidence_refs=[]
        )
        append_if_absent(lpath, se)
        le = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.REPLACEMENT_PACKAGE_LINKED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"replacement_package_id": replacement_pkg}, evidence_refs=[]
        )
        append_if_absent(lpath, le)

    if material_config_drift and previous_app:
        drift_app_payload = dict(previous_app.payload)
        drift_app_payload["observed_config"] = obs_cfg
        drift_app_payload["observed_config_hash"] = observed_hash
        request_regeneration(
            package, drift_app_payload, observation, output_dir, policy, recorded, effective,
            reason="CONFIGURATION_DRIFT",
        )
        return

    if not active_app_event:
        return

    app_payload = active_app_event.payload
    # Contrary application is evidence but not a realization of the original
    # recommendation. Request regeneration from actual state instead.
    if app_payload.get("implementation_conformance") == rc.ImplementationConformance.CONTRARY.value:
        request_regeneration(package, app_payload, observation, output_dir, policy, recorded, effective, reason="CONTRARY_IMPLEMENTATION")
        return

    ready = governance.readiness.get()
    if not ready["ready"]:
        return
    val_id = make_val_id(active_app_event.application_episode_id, observation, policy)
    vr = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.VALIDATION_READY,
        effective_at_utc=obs_time, recorded_at_utc=recorded,
        payload={"validation_episode_id": val_id, "readiness": ready, "policy_snapshot_id": policy.snapshot_id},
        evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id, validation_episode_id=val_id,
    )
    append_if_absent(lpath, vr)
    vs = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.VALIDATION_STARTED,
        effective_at_utc=obs_time, recorded_at_utc=recorded,
        payload={"validation_episode_id": val_id, "window_start_utc": observation.get("window_start_utc") or effective, "window_end_utc": observation.get("window_end_utc") or obs_time},
        evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id, validation_episode_id=val_id,
    )
    append_if_absent(lpath, vs)
    validation = governance.performance.get()
    validation_payload = {
        "state": validation["state"],
        "validation_episode_id": val_id,
        "readiness": ready,
        "hard_guardrails_pass": validation["hard_guardrails_pass"],
        "rules": validation["rules"],
        "policy_snapshot_id": policy.snapshot_id,
        "window_start_utc": observation.get("window_start_utc") or effective,
        "window_end_utc": observation.get("window_end_utc") or obs_time,
    }
    vc = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.VALIDATION_COMPLETED,
        effective_at_utc=obs_time, recorded_at_utc=recorded, payload=validation_payload,
        evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id, validation_episode_id=val_id,
    )
    append_if_absent(lpath, vc)

    cf_claim_eligible, cf_method = governance.counterfactual.get()
    econ_obs = observation.get("economics") or {}
    if econ_obs.get("counterfactual_old_cost") is not None:
        cf_id = make_cf_id(active_app_event.application_episode_id, observation, package, policy)
        cf_payload = {
            "counterfactual_id": cf_id,
            "method": cf_method,
            "claim_eligible": cf_claim_eligible,
            "workload_basis": "ACTUAL_POST_CHANGE_WORKLOAD",
            "counterfactual_old_cost": money(econ_obs.get("counterfactual_old_cost")),
            "modeler_version": MODELER_GATEWAY_VERSION,
        }
        ce = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.COUNTERFACTUAL_RECORDED,
            effective_at_utc=obs_time, recorded_at_utc=recorded, payload=cf_payload,
            evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id, validation_episode_id=val_id,
        )
        append_if_absent(lpath, ce)
    else:
        cf_id = make_cf_id(active_app_event.application_episode_id, observation, package, policy)

    economic = calculate_economics(
        package, app_payload, observation, policy, validation["hard_guardrails_pass"],
        counterfactual_claim_eligible=cf_claim_eligible,
    )
    if economic.get("available"):
        economic["quality_gates"] = governance.economic_quality.get()
        attr = attribution_result(app_payload, observation, validation["hard_guardrails_pass"], cf_claim_eligible, economic, policy)
        basis = str(economic.get("economic_basis") or "ECONOMIC_COST")
        meas_id = make_measurement_id(active_app_event.application_episode_id, observation, cf_id, basis)
        measurement_payload = {
            "measurement_id": meas_id,
            "window_start_utc": observation.get("window_start_utc") or effective,
            "window_end_utc": observation.get("window_end_utc") or obs_time,
            "counterfactual_id": cf_id,
            "counterfactual_method": cf_method,
            "recognition_status": rc.RecognitionStatus.CONFIRMED.value if validation["hard_guardrails_pass"] and cf_claim_eligible else rc.RecognitionStatus.REJECTED.value if not validation["hard_guardrails_pass"] else rc.RecognitionStatus.PROVISIONAL.value,
            "economics": economic,
            "attribution": attr,
            "realization_state": economic.get("realization_state"),
            "mature_ttm_realized_savings": economic.get("mature_ttm_realized_savings"),
            "correction_status": (observation.get("correction") or {}).get("type", "ORIGINAL"),
        }
        me = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ECONOMIC_MEASUREMENT_RECORDED,
            effective_at_utc=obs_time, recorded_at_utc=recorded, payload=measurement_payload,
            evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id,
            validation_episode_id=val_id, measurement_id=meas_id,
        )
        append_if_absent(lpath, me)
        corr = observation.get("correction") or {}
        if corr:
            prior_measurement = analysis.history.prior_measurement
            corrects_entry_id = corr.get("corrects_entry_id") or (prior_measurement.ledger_entry_id if prior_measurement else None)
            correction_payload = {
                "correction_key": corr.get("key") or meas_id,
                "type": str(corr.get("type") or "RESTATEMENT").upper(),
                "version": corr.get("version"),
                "reason": corr.get("reason"),
                "measurement_id": meas_id,
                "corrects_entry_id": corrects_entry_id,
            }
            co = make_ledger_entry(
                package=package, rpkg_id=rpkg, event_type=rc.EventType.BILLING_CORRECTION_RECORDED,
                effective_at_utc=obs_time, recorded_at_utc=recorded, payload=correction_payload,
                evidence_refs=[revid], application_episode_id=active_app_event.application_episode_id,
                validation_episode_id=val_id, measurement_id=meas_id,
                corrects_entry_id=corrects_entry_id,
            )
            append_if_absent(lpath, co)

    # Invalidating or modified authoritative changes require fresh Publisher
    # analysis once the governed remeasurement/readiness window has passed.
    if app_payload.get("invalidates") or app_payload.get("implementation_conformance") == rc.ImplementationConformance.MODIFIED.value:
        days = int(observation.get("elapsed_days", 0) or 0)
        required_days = int(app_payload.get("remeasure_days") or policy.remeasure_days_after_invalidating_change)
        if days >= required_days:
            request_regeneration(package, app_payload, observation, output_dir, policy, recorded, effective, reason="UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM")


def request_regeneration(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], output_dir: Path,
    policy: RealizerPolicy, recorded: str, effective: str, *, reason: str,
) -> None:
    rpkg = make_rpkg_id(package["recommendation_package_id"])
    actual_cfg = app_payload.get("observed_config") or observed_config(observation, package)
    rerun_id = rc.content_id("RERUN", {
        "source_package": package["recommendation_package_id"],
        "rpkg": rpkg,
        "actual_config_hash": config_hash(actual_cfg),
        "reason": reason,
        "policy_snapshot_id": policy.snapshot_id,
    })
    payload = {
        "regeneration_request_id": rerun_id,
        "source_recommendation_package_id": package["recommendation_package_id"],
        "source_realization_package_id": rpkg,
        "scenario_id": app_payload.get("scenario_id"),
        "step": app_payload.get("step"),
        "actual_config": actual_cfg,
        "actual_config_hash": config_hash(actual_cfg),
        "reasons": [reason],
        "impacted_knobs": list(app_payload.get("invalidates") or []),
        "policy_snapshot_id": policy.snapshot_id,
        "status": rc.RegenerationStatus.REQUESTED.value,
    }
    event = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.REGENERATION_REQUESTED,
        effective_at_utc=effective, recorded_at_utc=recorded, payload=payload,
        application_episode_id=app_payload.get("application_episode_id"),
    )
    append_if_absent(ledger_path(output_dir, rpkg), event)
    write_json(output_dir / "realization" / "regeneration" / f"{rerun_id}.json", payload)


# ---------------------------------------------------------------------------
# Portfolio / recommendation stability helpers
# ---------------------------------------------------------------------------


def authoritative_cumulative_realization(
    counterfactual_original_cost: Any, actual_final_cost: Any, incremental_realizations: Sequence[Any] = (),
) -> Dict[str, Any]:
    cf, actual = decimal(counterfactual_original_cost), decimal(actual_final_cost)
    if cf is None or actual is None:
        return {"authoritative": None, "diagnostic_incremental_sum": None, "interaction_delta": None}
    authoritative = cf - actual
    diagnostic = sum((decimal(v) or Decimal("0") for v in incremental_realizations), Decimal("0"))
    return {
        "authoritative": money(authoritative),
        "diagnostic_incremental_sum": money(diagnostic),
        "interaction_delta": money(authoritative - diagnostic),
    }


# ---------------------------------------------------------------------------
# Compatibility projection (legacy Reporter v1.0.0 remains immutable)
# ---------------------------------------------------------------------------


def legacy_compatibility(snapshot: rc.RealizationPackage) -> Dict[str, Any]:
    state = snapshot.current_state["realization_state"]
    actor = snapshot.application.get("actor")
    mapping = {
        "NOT_APPLIED": "NOT_APPLIED",
        "APPLIED": "APPLIED",
        "VALIDATING": "VALIDATING",
        "REALIZED": "REALIZED",
        "UNDER_REALIZED": "REALIZED",
        "VALIDATION_FAILED": "FAILED_VALIDATION",
        "ROLLED_BACK": "ROLLED_BACK",
        "MONITORING": "REALIZED",
    }
    if state not in mapping:
        return {"status": rc.CompatibilityStatus.UNREPRESENTABLE.value, "legacy_realization_status": None, "reasons": [f"REALIZER_STATE_{state}_HAS_NO_LEGACY_MAPPING"]}
    reasons: List[str] = []
    quality = rc.CompatibilityStatus.LOSSLESS
    if state in {"UNDER_REALIZED", "MONITORING"}:
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append(f"{state}_COLLAPSES_TO_LEGACY_REALIZED")
    if snapshot.application.get("state") == "APPLIED" and not snapshot.application.get("actor"):
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN")
    if snapshot.application.get("implementation_conformance") == "MODIFIED":
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_HAS_NO_MODIFIED_CONFORMANCE")
    return {"status": quality.value, "legacy_realization_status": mapping[state], "reasons": reasons}


# ---------------------------------------------------------------------------
# CLI (the demo observation is realizer.analyzer.collector.collector)
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic Databricks SQL Warehouse Realizer v1.0.0")
    p.add_argument("--package", required=True, help="Path to immutable RecommendationPackage JSON")
    p.add_argument("--observation", help="Path to normalized post-change observation JSON")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out")
    p.add_argument("--policy", help="Optional Realizer policy override JSON")
    p.add_argument("--demo", action="store_true", help="Use deterministic synthetic post-change observation")
    p.add_argument("--demo-modified", action="store_true", help="Demo a modified size target when possible")
    p.add_argument("--replay-through", help="Project only ledger entries recorded through this UTC time")
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    package = load_package(args.package)
    observation: Optional[Mapping[str, Any]] = None
    if args.demo or args.demo_modified:
        observation = demo_observation(package, modified=args.demo_modified)
    elif args.observation:
        observation = load_observation(args.observation)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    snapshot = realize(package, observation, Path(args.output_dir), policy=policy, replay_through=args.replay_through)
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(Path(args.output_dir) / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
