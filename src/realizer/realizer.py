#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.0.0: the engine.

Working copy of the frozen single-file oracle (``legacy/realizer_single_file/realizer.py``) with the same behavior.
The realizer reorganization (docs/architecture/realizer-repo-reorg-design.md, section 22) moves its parts into the
capability subpackages phase by phase; so far the contracts (``realizer.contracts``), settings and loading
(``realizer.config``), the shared helpers (``realizer.common``), the Analyzer (``realizer.analyzer``), the Governor
(``realizer.governor``), the Reconciler (``realizer.reconciler``), the Estimator (``realizer.estimator``) and the
Reviewer (``realizer.reviewer``) are extracted, and this module imports them. ``realize`` runs the Analyzer, the
Governor and the Reconciler; the engine records the lifecycle events the Reconciler decides and, when validation is
due, runs the Estimator and the Reviewer and records the validation episode and the measurement they decide.

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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.analyzer import analyzer
from realizer.analyzer.analysis import candidate_steps
from realizer.analyzer.collector.collector import demo_observation, load_observation
from realizer.common.logging import LOGGER, setup_logging
from realizer.common.utils import utc_iso, write_json
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
)
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import make_rpkg_id
from realizer.contracts.reconciliation import ReconciliationResult, RegenerationRequest
from realizer.contracts.request import RealizationRequest, RunContext
from realizer.governor import governor
from realizer.estimator import estimator
from realizer.governor.policy import RealizerPolicy, authority_eligible
from realizer.packager.ledger import ledger_path, load_ledger
from realizer.reconciler import reconciler
from realizer.reconciler.lifecycle import regeneration_request
from realizer.reviewer import reviewer

# The engine keeps every name it had; the extracted ones stay importable from here (``realizer.realizer.<name>``).
from realizer.analyzer.analysis import normalize_scalar, observed_config, photon_bool_from_package, recommended_scenario  # noqa: F401,E402
from realizer.governor.policy import counterfactual_eligible, policy_to_dict, savings_improvement_material, validate_performance  # noqa: F401,E402
from realizer.packager.ledger import latest_event  # noqa: F401,E402
from realizer.common.utils import load_json  # noqa: F401,E402
from realizer.config.settings import CONFIG_KNOBS, SIZE_ORDER  # noqa: F401,E402
from realizer.contracts.realization import make_app_id, make_evidence_id  # noqa: F401,E402
from realizer.reconciler.matching import _directional_relation, choose_application, compare_step  # noqa: F401,E402
from realizer.estimator.economics import calculate_economics, expected_annual_for_exact_step, make_cf_id  # noqa: F401,E402
from realizer.estimator.attribution import attribution_result, authoritative_cumulative_realization  # noqa: F401,E402
# (and the names the engine imported for them before they moved)
from decimal import Decimal  # noqa: F401,E402
from typing import Tuple  # noqa: F401,E402
from realizer.analyzer.analysis import candidate_by_id, config_hash, package_config, scenario_by_id  # noqa: F401,E402
from realizer.common.utils import decimal, money, ratio_string  # noqa: F401,E402
from realizer.contracts.realization import make_measurement_id, make_val_id  # noqa: F401,E402
from realizer.governor.policy import economic_quality_result, readiness  # noqa: F401,E402

# ---------------------------------------------------------------------------
# Canonical helpers / identities
# ---------------------------------------------------------------------------


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
    # did. The Governor and the Reconciler never fail: a check keeps its exception until the consumer needs the result,
    # and a reconciliation failure is raised by process_observation after the events decided before it.
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    reconciliation = reconciler.run(context, analysis, governance)
    if analysis.observation is not None:
        process_observation(context, analysis, governance, reconciliation)

    entries = load_ledger(lpath)
    if replay_through:
        cut = utc_iso(replay_through)
        entries = [e for e in entries if e.recorded_at_utc <= cut]
    snapshot = project_snapshot(package, entries)
    payload = snapshot.to_dict()
    write_json(output_dir / "realization" / "packages" / f"{rpkg}.json", payload)
    write_json(output_dir / "realization" / "snapshots" / f"{snapshot.snapshot_id}.json", payload)
    return snapshot


def process_observation(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult, reconciliation: ReconciliationResult,
) -> None:
    package, output_dir = context.request.package, context.request.output_dir
    observed = analysis.observation
    assert observed is not None  # the caller runs this only for a run with an observation
    rpkg = context.realization_package_id
    lpath = ledger_path(output_dir, rpkg)
    obs_time, recorded, effective = observed.observed_at_utc, observed.recorded_at_utc, observed.effective_at_utc
    revid = observed.evidence_id
    write_json(output_dir / "realization" / "evidence" / f"{revid}.json", observed.evidence_record)

    # Record the lifecycle events the Reconciler decided, in order: drift, the application events, intent, rollback,
    # supersession. If reconciliation failed, the events decided before the failure are recorded and then it is raised.
    if reconciliation.drift is not None:
        drift = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.DRIFT_DETECTED,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=reconciliation.drift,
            evidence_refs=[revid], application_episode_id=reconciliation.drift_episode_id,
        )
        append_if_absent(lpath, drift)
    for planned in reconciliation.applications:
        event = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=planned.event_type,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=planned.payload,
            evidence_refs=[revid], application_episode_id=planned.application_episode_id,
        )
        append_if_absent(lpath, event)
    if reconciliation.intent is not None:
        ie = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.INTENT_RECORDED,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=reconciliation.intent,
            evidence_refs=[], application_episode_id=reconciliation.intent_episode_id,
        )
        append_if_absent(lpath, ie)
    if reconciliation.rollback_requested is not None:
        rb = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ROLLBACK_REQUESTED,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=reconciliation.rollback_requested,
            evidence_refs=[revid], application_episode_id=reconciliation.rollback_episode_id,
        )
        append_if_absent(lpath, rb)
    if reconciliation.rollback_observed is not None:
        rb = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ROLLBACK_OBSERVED,
            effective_at_utc=effective, recorded_at_utc=recorded, payload=reconciliation.rollback_observed,
            evidence_refs=[revid], application_episode_id=reconciliation.rollback_episode_id,
        )
        append_if_absent(lpath, rb)
    if reconciliation.replacement_package_id:
        se = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.SOURCE_PACKAGE_SUPERSEDED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"replacement_package_id": reconciliation.replacement_package_id}, evidence_refs=[]
        )
        append_if_absent(lpath, se)
        le = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.REPLACEMENT_PACKAGE_LINKED,
            effective_at_utc=effective, recorded_at_utc=recorded,
            payload={"replacement_package_id": reconciliation.replacement_package_id}, evidence_refs=[]
        )
        append_if_absent(lpath, le)
    if reconciliation.halt is not None:
        raise reconciliation.halt

    if reconciliation.terminal_regeneration is not None:
        append_regeneration(package, output_dir, reconciliation.terminal_regeneration, recorded, effective)
        return
    if not reconciliation.validation_due:
        return

    # Validation is due: the Estimator measures the active application, the Reviewer assembles its validation episode.
    # The episode is recorded first (ready, started, completed), then the counterfactual, the measurement and its
    # billing correction. A stage that failed is raised after the facts it decided before the failure, the episode's
    # before the estimate's: the same exception after the same entries as before.
    active = reconciliation.active
    assert active is not None  # validation is due only for an active application
    estimate = estimator.run(context, analysis, governance, reconciliation)
    review = reviewer.run(context, analysis, governance, reconciliation, estimate)
    episode = review.validation
    app_id = active.application_episode_id
    for event_type, payload in (
        (rc.EventType.VALIDATION_READY, episode.ready_payload if episode else None),
        (rc.EventType.VALIDATION_STARTED, episode.started_payload if episode else None),
        (rc.EventType.VALIDATION_COMPLETED, episode.completed_payload if episode else None),
    ):
        if payload is None:
            break
        entry = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=event_type,
            effective_at_utc=obs_time, recorded_at_utc=recorded, payload=payload,
            evidence_refs=[revid], application_episode_id=app_id, validation_episode_id=episode.validation_episode_id,
        )
        append_if_absent(lpath, entry)
    if review.halt is not None:
        raise review.halt
    assert episode is not None  # a review that did not halt has its episode
    val_id = episode.validation_episode_id

    counterfactual = estimate.counterfactual
    if counterfactual is not None and counterfactual.payload is not None:
        ce = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.COUNTERFACTUAL_RECORDED,
            effective_at_utc=obs_time, recorded_at_utc=recorded, payload=counterfactual.payload,
            evidence_refs=[revid], application_episode_id=app_id, validation_episode_id=val_id,
        )
        append_if_absent(lpath, ce)
    measurement = estimate.measurement
    if measurement is not None:
        measurement_payload = {
            "measurement_id": measurement.measurement_id,
            "window_start_utc": measurement.window_start_utc,
            "window_end_utc": measurement.window_end_utc,
            "counterfactual_id": measurement.counterfactual_id,
            "counterfactual_method": measurement.counterfactual_method,
            "recognition_status": review.recognition_status,
            "economics": measurement.economics,
            "attribution": measurement.attribution,
            "realization_state": measurement.realization_state,
            "mature_ttm_realized_savings": measurement.mature_ttm_realized_savings,
            "correction_status": measurement.correction_status,
        }
        me = make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=rc.EventType.ECONOMIC_MEASUREMENT_RECORDED,
            effective_at_utc=obs_time, recorded_at_utc=recorded, payload=measurement_payload,
            evidence_refs=[revid], application_episode_id=app_id,
            validation_episode_id=val_id, measurement_id=measurement.measurement_id,
        )
        append_if_absent(lpath, me)
        correction = estimate.correction
        if correction is not None:
            co = make_ledger_entry(
                package=package, rpkg_id=rpkg, event_type=rc.EventType.BILLING_CORRECTION_RECORDED,
                effective_at_utc=obs_time, recorded_at_utc=recorded, payload=correction.payload,
                evidence_refs=[revid], application_episode_id=app_id,
                validation_episode_id=val_id, measurement_id=measurement.measurement_id,
                corrects_entry_id=correction.corrects_entry_id,
            )
            append_if_absent(lpath, co)
    if estimate.halt is not None:
        raise estimate.halt

    # Invalidating or modified authoritative changes require fresh Publisher
    # analysis once the governed remeasurement/readiness window has passed.
    upstream = reconciliation.upstream_regeneration.get() if reconciliation.upstream_regeneration is not None else None
    if upstream is not None:
        append_regeneration(package, output_dir, upstream, recorded, effective)


def request_regeneration(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], output_dir: Path,
    policy: RealizerPolicy, recorded: str, effective: str, *, reason: str,
) -> None:
    append_regeneration(package, output_dir, regeneration_request(package, app_payload, observation, policy, reason=reason), recorded, effective)


def append_regeneration(package: Mapping[str, Any], output_dir: Path, request: RegenerationRequest, recorded: str, effective: str) -> None:
    """Record a regeneration request (REGENERATION_REQUESTED on its application episode), then write its RERUN file."""
    rpkg = make_rpkg_id(package["recommendation_package_id"])
    event = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.REGENERATION_REQUESTED,
        effective_at_utc=effective, recorded_at_utc=recorded, payload=request.payload,
        application_episode_id=request.application_episode_id,
    )
    append_if_absent(ledger_path(output_dir, rpkg), event)
    write_json(output_dir / "realization" / "regeneration" / f"{request.regeneration_request_id}.json", request.payload)


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
