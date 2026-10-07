"""Snapshot projection: the realization state of a package, folded from its ledger.

``project_snapshot`` is moved whole from the single-file Realizer: it folds lifecycle precedence (the drift or observed
rollback that closed the active application episode, else applied), plan progress, validation, realization and maturity state and regeneration status
over every entry, so splitting it would risk the snapshot identity (RSNAP-*) drifting. It reads the ledger entries it is
given and the immutable source package; it decides nothing new and writes nothing.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from realizer.analyzer.analysis import candidate_steps, episodes
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
from realizer.packager.to_date import measured_window, realized_to_date
from realizer.packager.ledger import in_ledger_order, latest_event, ledger_checkpoint, source_scope


def project_snapshot(package: Mapping[str, Any], entries: Sequence[rc.RealizationLedgerEntry]) -> rc.RealizationPackage:
    ordered = in_ledger_order(entries)
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
    # Only an application of a plan step is an application; an independent change never is, whatever its step.
    auth_apps = [e for e in app_events if e.event_type != rc.EventType.INDEPENDENT_CHANGE_OBSERVED and e.payload.get("authority_eligible")]
    # The application shown is the open episode of the plan's earliest applied step (the one measured; several are open
    # when the plan's steps were applied one after another), else the latest application. Its own validation and
    # measurements are the ones shown: an earlier episode's are history.
    lineage = episodes(ordered)
    open_apps = [v["application"] for v in lineage.values() if not v["closers"] and v["application"].payload.get("authority_eligible")]
    app = min(open_apps, key=lambda e: int(e.payload.get("step") or 0)) if open_apps else (auth_apps[-1] if auth_apps else None)
    own = [e for e in ordered if app is not None and e.application_episode_id == app.application_episode_id]
    validation = latest_event(own, rc.EventType.VALIDATION_COMPLETED)
    measurement = latest_event(own, rc.EventType.ECONOMIC_MEASUREMENT_RECORDED)
    to_date = realized_to_date([w for w in (measured_window(m.payload.get("window_start_utc"), m.payload.get("window_end_utc"),
                                                               (m.payload.get("economics") or {}).get("realized_savings_period"))
                                            for m in own if m.event_type == rc.EventType.ECONOMIC_MEASUREMENT_RECORDED
                                            and (m.payload.get("economics") or {}).get("available")) if w is not None])
    # The active episode's state: the drift or observed rollback that closed it, if any. An earlier episode's closure
    # (a drift or rollback before the configuration was applied again) is history, never the active episode's state.
    closers = (lineage.get(app.application_episode_id) or {}).get("closers") or [] if app is not None else []
    closure = closers[-1] if closers else None
    # The applied interval ends at the first closing event (a rollback after a drift ends nothing more), at the time it
    # took effect (an entry recorded before that time was stated carries only its effective time).
    first_closure = closers[0] if closers else None
    closed_at = (first_closure.payload.get("effective_from_utc") or first_closure.effective_at_utc) if first_closure is not None else None
    drift = closure if closure is not None and closure.event_type == rc.EventType.DRIFT_DETECTED else None
    rollback = closure if closure is not None and closure.event_type == rc.EventType.ROLLBACK_OBSERVED else None
    regen = latest_event(ordered, rc.EventType.REGENERATION_REQUESTED, rc.EventType.REPLACEMENT_PACKAGE_LINKED)
    superseded = latest_event(ordered, rc.EventType.SOURCE_PACKAGE_SUPERSEDED)

    if rollback:
        app_state = rc.ApplicationState.ROLLED_BACK.value
    elif drift:
        app_state = rc.ApplicationState.DRIFTED.value
    elif app:
        app_state = rc.ApplicationState.APPLIED.value
    else:
        app_state = rc.ApplicationState.NOT_APPLIED.value

    projected_scenario_id = app.payload.get("scenario_id") if app else (package.get("recommended_plan") or {}).get("scenario_id")
    steps = candidate_steps(package, projected_scenario_id)
    # The steps applied: those the open episodes hold while any is open, else every application's.
    applied_refs = {(e.payload.get("scenario_id"), e.payload.get("step")) for e in (open_apps or auth_apps) if e.payload.get("implementation_conformance") in {"EXACT", "MODIFIED"}}
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
    elif latest_event(own, rc.EventType.VALIDATION_STARTED):
        val_state = rc.ValidationState.RUNNING.value
    elif latest_event(own, rc.EventType.VALIDATION_READY):
        val_state = rc.ValidationState.READY.value

    if app_state == rc.ApplicationState.ROLLED_BACK.value:
        realization_state = rc.RealizationState.ROLLED_BACK.value
    elif app_state == rc.ApplicationState.DRIFTED.value:
        realization_state = rc.RealizationState.DRIFTED.value
    elif app and app.payload.get("implementation_conformance") == rc.ImplementationConformance.CONTRARY.value:
        realization_state = rc.RealizationState.INVALIDATED.value  # the opposite of the recommendation: regenerate
    elif validation and not validation.payload.get("hard_guardrails_pass"):
        realization_state = rc.RealizationState.VALIDATION_FAILED.value
    elif app and app.payload.get("step_role") == rc.StepRole.SAFETY_ROLLBACK.value:
        realization_state = rc.RealizationState.SAFETY_ACTION_APPLIED.value  # recorded, never measured or claimed
    elif measurement:
        realization_state = str(measurement.payload.get("realization_state") or rc.RealizationState.MONITORING.value)
    elif app:
        realization_state = rc.RealizationState.VALIDATING.value if val_state in {"READY", "RUNNING"} else rc.RealizationState.APPLIED.value
    else:
        realization_state = rc.RealizationState.NOT_APPLIED.value

    if not app:
        maturity = rc.MaturityState.NOT_APPLIED.value
    elif measurement and validation and validation.payload.get("readiness", {}).get("ready"):
        maturity = rc.MaturityState.MATURE.value if to_date is not None and to_date["mature"] else rc.MaturityState.REPRESENTATIVE.value
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
    if to_date is not None and attr.get("attribution_claim_status") == "CLAIMABLE":
        # A claim covers what was realized to date, not one window: an episode closed by a rollback or a drift keeps
        # its claim for the interval it was in force.
        attr = {**attr, "recommendation_attributed_realized_savings": to_date["realized_to_date"]}

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
        **source_scope(package),
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
        "open_episodes": [{"application_episode_id": e.application_episode_id, "step": e.payload.get("step"), "knob": e.payload.get("knob"),
                           "effective_from_utc": e.payload.get("effective_from_utc")} for e in open_apps],
        "application_scope": app.payload.get("application_scope", "CANDIDATE") if app else None,
        "applied_step_refs": app.payload.get("applied_step_refs", []) if app else [],
        "implementation_conformance": app.payload.get("implementation_conformance") if app else "NOT_APPLIED",
        "modification_relation": app.payload.get("modification_relation") if app else None,
        "scenario_id": app.payload.get("scenario_id") if app else None,
        "step": app.payload.get("step") if app else None,
        "knob": app.payload.get("knob") if app else None,
        "step_role": app.payload.get("step_role") if app else None,
        "compatibility_status": app.payload.get("compatibility_status") if app else None,
        "annotations": list(app.payload.get("annotations") or []) if app else [],
        "source_value": app.payload.get("source_value") if app else None,
        "recommended_value": app.payload.get("recommended_value") if app else None,
        "observed_applied_value": app.payload.get("observed_value") if app else None,
        "observed_config": app.payload.get("observed_config") if app else None,
        "effective_from_utc": app.payload.get("effective_from_utc") if app else None,
        "effective_to_utc": closed_at,
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
    if measurement is not None and econ_section.get("available"):
        # Realized to date and the trailing twelve months come from every measurement of the episode (to_date.py); an
        # episode a rollback or a drift closed keeps what it realized and has no run-rate or ratio any more.
        econ_section = {**econ_section,
                        "realized_savings_since_application": None if to_date is None else to_date["realized_to_date"],
                        "mature_ttm_realized_savings": None if to_date is None else to_date["mature_ttm_realized_savings"],
                        "realized_to_date": None if to_date is None else {k: to_date[k] for k in ("first_day", "last_day", "days_covered", "uncovered_days")}}
        if app_state in (rc.ApplicationState.ROLLED_BACK.value, rc.ApplicationState.DRIFTED.value):
            econ_section.update({"annualized_realized_savings": None, "realization_ratio": None})
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
