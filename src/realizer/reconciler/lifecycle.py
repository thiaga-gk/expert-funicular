"""Reconciler lifecycle: what the observation means for the application episode and the source package.

Configuration drift, reuse of the active application episode, the scenario-scope application, the per-step
application events, recorded intent, rollback, supersession, and how the run ends: a regeneration request (drift, or a
contrary application), a validation episode when the evidence is ready, and the regeneration an invalidating or
modified application requests once its remeasurement window has passed. Moved unchanged from the single-file Realizer's
observation processing, in its order; the regeneration request is its RERUN id and payload (``regeneration_request``),
which the engine records and writes.

``plan`` decides the events in the order they are recorded and puts each into a ``LifecyclePlan`` as soon as it is
decided, so that a failure leaves exactly the events decided before it (see ``ReconciliationResult.halt``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from realizer.analyzer.analysis import config_hash, observed_config
from realizer.common.utils import Deferred
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import make_app_id, make_rpkg_id
from realizer.contracts.reconciliation import (
    ActiveApplication,
    PlanMatch,
    PlannedApplication,
    ReconciliationResult,
    RegenerationRequest,
)
from realizer.contracts.request import RunContext


@dataclass
class LifecyclePlan:
    """The reconciliation of one observation while it is being decided; ``result()`` freezes it.

    Fields are filled in recording order, so whatever is set when a step raises is what was decided before it.
    """
    match: Optional[PlanMatch] = None
    drift: Optional[Mapping[str, Any]] = None
    drift_episode_id: Optional[str] = None
    applications: List[PlannedApplication] = field(default_factory=list)
    active: Optional[ActiveApplication] = None
    intent: Optional[Mapping[str, Any]] = None
    intent_episode_id: Optional[str] = None
    rollback_requested: Optional[Mapping[str, Any]] = None
    rollback_observed: Optional[Mapping[str, Any]] = None
    rollback_episode_id: Optional[str] = None
    replacement_package_id: Any = None
    terminal_regeneration: Optional[RegenerationRequest] = None
    validation_due: bool = False
    upstream_regeneration: Optional[Deferred[Optional[RegenerationRequest]]] = None
    halt: Optional[Exception] = None

    def result(self) -> ReconciliationResult:
        return ReconciliationResult(
            match=self.match,
            drift=self.drift,
            drift_episode_id=self.drift_episode_id,
            applications=tuple(self.applications),
            active=self.active,
            intent=self.intent,
            intent_episode_id=self.intent_episode_id,
            rollback_requested=self.rollback_requested,
            rollback_observed=self.rollback_observed,
            rollback_episode_id=self.rollback_episode_id,
            replacement_package_id=self.replacement_package_id,
            terminal_regeneration=self.terminal_regeneration,
            validation_due=self.validation_due,
            upstream_regeneration=self.upstream_regeneration,
            halt=self.halt,
        )


def plan(state: LifecyclePlan, context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult) -> None:
    """Decide the lifecycle events of this run's observation into *state* (whose ``match`` is already set), in order.

    The readiness check is the last thing decided: it is read only for an active application that neither drifted nor
    was applied contrary, after every lifecycle event, as before.
    """
    package, policy = context.request.package, context.policy
    rpkg = context.realization_package_id
    observed = analysis.observation
    assert observed is not None and state.match is not None  # reconciler.run plans only a matched observation
    observation = observed.raw
    effective = observed.effective_at_utc
    obs_cfg = observed.observed_config
    source_hash, observed_hash = observed.source_config_hash, observed.observed_config_hash
    revid = observed.evidence_id
    selected_scenario_id = state.match.selected_scenario_id
    step_results, primary = state.match.step_matches, state.match.primary
    authority_changes, scenario_scope = state.match.authority_changes, state.match.scenario_scope
    previous_app = analysis.history.previous_application
    material_config_drift = analysis.config_drift_observed

    if material_config_drift:
        state.drift = {
            "state": "DRIFTED",
            "drift_type": "CONFIGURATION",
            "previous_observed_config_hash": previous_app.payload.get("observed_config_hash"),
            "observed_config_hash": observed_hash,
            "observed_config": obs_cfg,
            "material": True,
            "required_action": "REGENERATE",
        }
        state.drift_episode_id = previous_app.application_episode_id

    # Record every materially changed candidate relationship; only authority-
    # eligible primary scope can enter recommendation realization. Repeated
    # observations of the same already-applied configuration remain in the same
    # application episode rather than creating a new APP on every daily run.
    active: Optional[ActiveApplication] = _previous(previous_app) if material_config_drift else None
    if not material_config_drift and previous_app and primary and (
        previous_app.payload.get("observed_config_hash") == observed_hash
        and previous_app.payload.get("scenario_id") == primary.get("scenario_id")
        and (scenario_scope or previous_app.payload.get("step") == primary.get("step"))
    ):
        active = _previous(previous_app)
    state.active = active

    # Multiple compatible authoritative changes (or a migration alternative)
    # are one scenario-scope application episode. This avoids fabricating
    # candidate allocation for inseparable economics.
    if scenario_scope and authority_changes and active is None:
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
        state.applications.append(PlannedApplication(event_type=et, application_episode_id=app_id, payload=payload))
        active = ActiveApplication(application_episode_id=app_id, payload=payload, is_previous=False)
        state.active = active

    for item in step_results:
        conf = item.get("implementation_conformance")
        if conf in {None, rc.ImplementationConformance.NOT_APPLIED.value}:
            continue
        # A knob without a step is identified by its knob, so two such changes are two independent events.
        scope_key = str(item.get("scenario_id")) if item.get("step") is not None else f"{item.get('scenario_id')}|{item.get('knob')}"
        app_id = make_app_id(rpkg, scope_key, int(item.get("step") or 0), source_hash, observed_hash, revid)
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
        # A value the plan cannot relate to its step (an unknown size, a third type) is never an application.
        authority = bool(item["authority_eligible"]) and conf != rc.ImplementationConformance.UNRELATED.value
        if conf == rc.ImplementationConformance.UNRELATED.value and "independent_reason" not in payload:
            payload["independent_reason"] = "UNRELATED_VALUE"
        if material_config_drift and authority:
            continue
        if scenario_scope and authority:
            continue
        if (
            previous_app is not None
            and active is not None and active.is_previous
            and authority
            and primary
            and item.get("step") == primary.get("step")
        ):
            # Stable state observation: reuse the active APP. Measurement and
            # validation events below still record the new observation window.
            continue
        if not authority:
            et = rc.EventType.INDEPENDENT_CHANGE_OBSERVED
        elif conf == rc.ImplementationConformance.MODIFIED.value:
            et = rc.EventType.MODIFIED_APPLICATION_OBSERVED
        else:
            et = rc.EventType.APPLICATION_OBSERVED
        state.applications.append(PlannedApplication(event_type=et, application_episode_id=app_id, payload=payload))
        if authority and primary and item.get("step") == primary.get("step"):
            active = ActiveApplication(application_episode_id=app_id, payload=payload, is_previous=False)
            state.active = active

    intent = str(observation.get("intent_state") or "UNKNOWN").upper()
    if intent != "UNKNOWN":
        state.intent = {"intent_state": intent, "actor": observation.get("actor"), "reference": observation.get("intent_reference")}
        state.intent_episode_id = active.application_episode_id if active else None

    # Explicit rollback can be requested/observed independently of a new PKG.
    rollback_episode_id = active.application_episode_id if active else previous_app.application_episode_id if previous_app else None
    if observation.get("rollback_requested"):
        state.rollback_episode_id = rollback_episode_id
        state.rollback_requested = {"reason": observation.get("rollback_reason")}
    if observation.get("rollback_observed"):
        state.rollback_episode_id = rollback_episode_id
        state.rollback_observed = {"observed_config": obs_cfg, "reason": observation.get("rollback_reason")}

    # Replacement/supersession events are explicit; package generation alone is
    # not inferred from observation time.
    replacement_pkg = observation.get("replacement_package_id")
    if replacement_pkg:
        state.replacement_package_id = replacement_pkg

    if material_config_drift and previous_app:
        drift_app_payload = dict(previous_app.payload)
        drift_app_payload["observed_config"] = obs_cfg
        drift_app_payload["observed_config_hash"] = observed_hash
        state.terminal_regeneration = regeneration_request(package, drift_app_payload, observation, policy, reason="CONFIGURATION_DRIFT")
        return

    if not active:
        return

    app_payload = active.payload
    # Contrary application is evidence but not a realization of the original
    # recommendation. Request regeneration from actual state instead.
    if app_payload.get("implementation_conformance") == rc.ImplementationConformance.CONTRARY.value:
        state.terminal_regeneration = regeneration_request(package, app_payload, observation, policy, reason="CONTRARY_IMPLEMENTATION")
        return

    state.validation_due = bool(governance.readiness.get()["ready"])
    if state.validation_due:
        # Invalidating or modified authoritative changes request fresh Publisher analysis after the measurement; the
        # run reaches that point only after validation and measurement, so it is held (and raises) until then.
        state.upstream_regeneration = Deferred.of(upstream_regeneration_request, package, app_payload, observation, policy)


def _previous(previous_app: rc.RealizationLedgerEntry) -> ActiveApplication:
    """The previous run's application as the active episode (drift, or the same configuration observed again)."""
    return ActiveApplication(application_episode_id=previous_app.application_episode_id, payload=previous_app.payload, is_previous=True)


def regeneration_request(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], policy: Any, *, reason: str,
) -> RegenerationRequest:
    """The regeneration request (RERUN-*) for *app_payload*'s actual configuration: its id and payload, which the engine
    records as REGENERATION_REQUESTED on the application's episode and writes as the RERUN file."""
    rpkg = make_rpkg_id(package["recommendation_package_id"])
    actual_cfg = app_payload.get("observed_config") or observed_config(observation, package)
    rerun_id = rc.content_id("RERUN", {
        "source_package": package["recommendation_package_id"],
        "rpkg": rpkg,
        "actual_config_hash": config_hash(actual_cfg),
        "reason": reason,
        "policy_snapshot_id": policy.snapshot_id,
    })
    payload: Dict[str, Any] = {
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
    return RegenerationRequest(regeneration_request_id=rerun_id, application_episode_id=app_payload.get("application_episode_id"), payload=payload)


def upstream_regeneration_request(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], policy: Any,
) -> Optional[RegenerationRequest]:
    """The regeneration an invalidating or modified authoritative application requests once the governed
    remeasurement window (the step's ``remeasure_days``, else the policy's) has elapsed; ``None`` otherwise."""
    if app_payload.get("invalidates") or app_payload.get("implementation_conformance") == rc.ImplementationConformance.MODIFIED.value:
        days = int(observation.get("elapsed_days", 0) or 0)
        required_days = int(app_payload.get("remeasure_days") or policy.remeasure_days_after_invalidating_change)
        if days >= required_days:
            return regeneration_request(package, app_payload, observation, policy, reason="UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM")
    return None
