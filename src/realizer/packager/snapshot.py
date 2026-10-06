"""Snapshot projection: the realization state of a package, folded from its ledger.

``project_snapshot`` is moved whole and unchanged from the single-file Realizer: it folds lifecycle precedence
(rollback, then drift, then applied), plan progress, validation, realization and maturity state and regeneration status
over every entry, so splitting it would risk the snapshot identity (RSNAP-*) drifting. It reads the ledger entries it is
given and the immutable source package; it decides nothing new and writes nothing.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from realizer.analyzer.analysis import candidate_steps
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
from realizer.contracts.realization import make_rpkg_id
from realizer.governor.policy import authority_eligible
from realizer.packager.ledger import latest_event, ledger_checkpoint


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
