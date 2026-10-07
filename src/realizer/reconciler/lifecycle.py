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

from realizer.analyzer.analysis import APPLICATION_EVENTS, applied_sources, back_at_source, config_hash, normalize_value, observed_config, scenario_by_id
from realizer.common.errors import InputError
from realizer.common.logging import LOGGER
from realizer.common.utils import Deferred, parse_utc, utc_iso
from realizer.config.settings import HASHED_CONFIG_KNOBS
from realizer.contracts import realization as rc
from realizer.contracts.analysis import ObservationAnalysis, RealizationAnalysis
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
from realizer.reconciler.matching import DECISION_KNOBS, compatibility


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
    step_regeneration: Optional[RegenerationRequest] = None
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
            step_regeneration=self.step_regeneration,
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
    previous_app, closure = analysis.history.previous_application, analysis.history.closure
    open_apps = analysis.history.open_applications
    # While application episodes are open (one per plan step applied, or one scenario-scope application), the run reads
    # the change against the configuration last recorded (``_plan_progress``): a later step of the plan applied is a
    # new application and the open episodes go on; a step the package holds for re-evaluation applied early is an
    # independent change that requests regeneration; open episodes whose knobs are all back at their source value are
    # rolled back; anything else is a drift, which closes every open episode.
    progress = _plan_progress(package, analysis.history, open_apps, step_results, obs_cfg, observed) if open_apps else None
    if progress is not None:
        changed = progress.changed
        rolled_back = bool(progress.rolled and not progress.unexplained)
        closed_again = False
        material_config_drift = progress.unexplained
        drifted = material_config_drift
        anchor = open_apps[-1]
        closed_at = closing_time(observed, anchor) if progress.diff else None
    else:
        changed = analysis.config_drift_observed   # a later configuration than the previous application's
        # A closed episode (a drift or an observed rollback recorded after its application) never absorbs a new
        # application: its configuration observed again is a new episode. A later configuration is a rollback when every
        # knob the application changed is back at its source value (the warehouse is at the package's own configuration
        # again; an episode already rolled back is not rolled back twice), the closing configuration observed again on a
        # later run records nothing new (a drift still ends the run with its regeneration request), and any other later
        # configuration is a drift, unless it closed the episode before and now applies a plan step: then it is a new
        # application.
        rolled_back = bool(changed and previous_app is not None and (closure is None or closure.event_type == rc.EventType.DRIFT_DETECTED)
                           and back_at_source(package, previous_app.payload, obs_cfg))
        closed_again = bool(changed and closure is not None and not rolled_back and closure.payload.get("observed_config_hash") == observed_hash)
        material_config_drift = bool(changed and not rolled_back and not closed_again and (closure is None or primary is None))
        drifted = material_config_drift or (closed_again and closure.event_type == rc.EventType.DRIFT_DETECTED)
        anchor = previous_app
        # A drift and an observed rollback carry the time they took effect, so a recurring one is a new event.
        closed_at = closing_time(observed, previous_app) if (material_config_drift or rolled_back) and previous_app is not None else None

    if material_config_drift:
        state.drift = {
            "state": "DRIFTED",
            "drift_type": "CONFIGURATION",
            "previous_observed_config_hash": (progress.reference.payload if progress is not None else previous_app.payload).get("observed_config_hash"),
            "observed_config_hash": observed_hash,
            "observed_config": obs_cfg,
            "material": True,
            "required_action": "REGENERATE",
            "effective_from_utc": closed_at,
        }
        state.drift_episode_id = anchor.application_episode_id

    # Record every materially changed candidate relationship; only authority-
    # eligible primary scope can enter recommendation realization. Repeated
    # observations of the same already-applied configuration remain in the same
    # application episode, while it is open, rather than creating a new APP on every daily run.
    active: Optional[ActiveApplication] = _previous(anchor) if drifted else None
    if progress is not None:
        if not drifted and primary is not None:
            # The open episode of the plan's first applied step is the one measured (the live measurement reads it).
            owner = next((a for a in progress.still_open if _covers(a.payload, primary)), None)
            active = _previous(owner) if owner is not None else None
    elif not drifted and closure is None and previous_app and primary and (
        previous_app.payload.get("observed_config_hash") == observed_hash
        and previous_app.payload.get("scenario_id") == primary.get("scenario_id")
        and (scenario_scope or previous_app.payload.get("step") == primary.get("step"))
    ):
        active = _previous(previous_app)
    state.active = active

    # Multiple compatible authoritative changes (or a migration alternative)
    # are one scenario-scope application episode. This avoids fabricating
    # candidate allocation for inseparable economics.
    if progress is None and scenario_scope and authority_changes and active is None:
        scenario_conf = (
            rc.ImplementationConformance.CONTRARY.value
            if any(i["implementation_conformance"] == rc.ImplementationConformance.CONTRARY.value for i in authority_changes)
            else rc.ImplementationConformance.MODIFIED.value
            if any(i["implementation_conformance"] == rc.ImplementationConformance.MODIFIED.value for i in authority_changes)
            else rc.ImplementationConformance.EXACT.value
        )
        app_id = make_app_id(rpkg, str(selected_scenario_id), 0, source_hash, observed_hash, revid)
        invalidates = sorted({k for i in authority_changes for k in (i.get("invalidates") or [])})
        status, annotations = compatibility(scenario_by_id(package, selected_scenario_id))
        payload = {
            "application_scope": "SCENARIO",
            "scenario_id": selected_scenario_id,
            "step": None,
            "candidate_id": None,
            "technique_id": None,
            "knob": None,
            "step_role": None,
            "execution_state": "SCENARIO_APPLIED",
            "authority_eligible": True,
            "source_value": None,
            "recommended_value": None,
            "observed_value": None,
            "implementation_conformance": scenario_conf,
            "modification_relation": None,
            "invalidates": invalidates,
            "dependencies": [],
            "compatibility_status": status,
            "annotations": annotations,
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
        if drifted and authority:
            continue
        if progress is not None:
            # Only what changed since the configuration last recorded is new: a later step of the plan (from the epoch
            # that brought it) or an independent change; a step an open episode holds is that episode.
            if authority and item.get("step") not in progress.steps:
                continue
            if not authority and item.get("knob") not in progress.moved:
                continue
            if authority:
                payload["effective_from_utc"] = closed_at
        elif scenario_scope and authority:
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
        state.intent_episode_id = (active.application_episode_id if active
                                   else previous_app.application_episode_id if (rolled_back or closed_again) and previous_app else None)

    # A rollback request is the caller's recorded intent, at the time it was observed; one still pending on the episode
    # (requested, no rollback observed since) is not requested again. An observed rollback is the configuration's (the
    # caller's ``rollback_observed`` flag was checked against it by the Analyzer), on the episode it rolls back.
    rollback_episode_id = active.application_episode_id if active else previous_app.application_episode_id if previous_app else None
    if observation.get("rollback_requested") and not _rollback_pending(analysis.history.entries, rollback_episode_id):
        state.rollback_episode_id = rollback_episode_id
        state.rollback_requested = {"reason": observation.get("rollback_reason"), "effective_from_utc": observed.observed_at_utc}
    if rolled_back:
        rolled = list(progress.rolled) if progress is not None else [previous_app]
        assert rolled and rolled[-1] is not None
        state.rollback_episode_id = rolled[-1].application_episode_id
        state.rollback_observed = {"observed_config": obs_cfg, "observed_config_hash": observed_hash, "reason": observation.get("rollback_reason"),
                                   "effective_from_utc": closed_at, "rolled_back_episode_ids": sorted(a.application_episode_id for a in rolled)}

    # Replacement/supersession events are explicit; package generation alone is
    # not inferred from observation time.
    replacement_pkg = observation.get("replacement_package_id")
    if replacement_pkg:
        state.replacement_package_id = replacement_pkg

    if progress is not None and progress.pending and not drifted:
        # A step the package holds for re-evaluation applied while the plan is in progress: no credit (an independent
        # change, above) and a fresh analysis is requested; the open episodes go on.
        step_payload = {**anchor.payload, "observed_config": obs_cfg, "observed_config_hash": observed_hash}
        state.step_regeneration = regeneration_request(package, step_payload, observation, policy, reason="PENDING_STEP_APPLIED", evidence_id=revid)

    if drifted and anchor:
        drift_app_payload = dict(anchor.payload)
        drift_app_payload["observed_config"] = obs_cfg
        drift_app_payload["observed_config_hash"] = observed_hash
        state.terminal_regeneration = regeneration_request(package, drift_app_payload, observation, policy, reason="CONFIGURATION_DRIFT", evidence_id=revid)
        return

    if not active:
        return

    app_payload = active.payload
    # Contrary application is evidence but not a realization of the original
    # recommendation. Request regeneration from actual state instead.
    if app_payload.get("implementation_conformance") == rc.ImplementationConformance.CONTRARY.value:
        state.terminal_regeneration = regeneration_request(package, app_payload, observation, policy, reason="CONTRARY_IMPLEMENTATION", evidence_id=revid)
        return

    ready = governance.readiness.get()
    state.validation_due = bool(ready["ready"])
    if not state.validation_due:
        # Nothing is recorded for evidence that is not ready yet; the log says which checks hold it back and why.
        failed = [name + (f"({check['reason']})" if check.get("reason") else "") for name, check in ready["checks"].items() if not check["pass"]]
        LOGGER.info("realizer.not_ready app=%s failed=%s", active.application_episode_id, ",".join(failed))
    if state.validation_due:
        # Invalidating or modified authoritative changes request fresh Publisher analysis after the measurement; the
        # run reaches that point only after validation and measurement, so it is held (and raises) until then.
        state.upstream_regeneration = Deferred.of(upstream_regeneration_request, package, app_payload, observation, policy, evidence_id=revid)


@dataclass(frozen=True)
class PlanProgress:
    """How an observation relates to the configuration last recorded while application episodes are open.

    ``reference`` is that record (the latest application, independent change or observed rollback), ``changed`` whether
    the observation is a later, different configuration, ``moved`` the decision knobs that differ from it (``diff``: those
    of them the configuration hash covers, the channel moves without a priced effect), ``rolled``
    the open episodes whose knobs are all back at their source value, ``still_open`` the others, ``steps`` the plan
    steps applied since (each a new application), ``pending`` the knobs of steps held for re-evaluation applied since,
    and ``unexplained`` whether any changed knob is explained by none of these (a drift)."""
    reference: rc.RealizationLedgerEntry
    changed: bool
    moved: frozenset
    diff: frozenset
    rolled: tuple
    still_open: tuple
    steps: frozenset
    pending: frozenset
    unexplained: bool


# The independent reasons of a changed step the package holds (PENDING_REEVALUATION) or blocks.
HELD_STEP_REASONS = frozenset({"PENDING_REEVALUATION", "BLOCKED_STEP"})
APPLIED_CONFORMANCES = frozenset({rc.ImplementationConformance.EXACT.value, rc.ImplementationConformance.MODIFIED.value})
REFERENCE_EVENTS = (*APPLICATION_EVENTS, rc.EventType.INDEPENDENT_CHANGE_OBSERVED, rc.EventType.ROLLBACK_OBSERVED)


def _covers(app_payload: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Whether an application (a step's, or a scenario-scope one) holds the plan step of *item* at *item*'s value."""
    if app_payload.get("scenario_id") != item.get("scenario_id") or item.get("step") is None:
        return False
    if app_payload.get("application_scope") == "SCENARIO":
        steps = {ref.get("step") for ref in app_payload.get("applied_step_refs") or []}
        return item.get("step") in steps and (app_payload.get("observed_config") or {}).get(str(item.get("knob"))) == item.get("observed_value")
    return app_payload.get("step") == item.get("step") and app_payload.get("observed_value") == item.get("observed_value")


def _plan_progress(package: Mapping[str, Any], history: Any, open_apps: Any, step_results: Any, obs_cfg: Mapping[str, Any],
                   observed: ObservationAnalysis) -> PlanProgress:
    """The observation against the configuration last recorded while *open_apps* are open (``PlanProgress``)."""
    reference = next(e for e in reversed(history.entries) if e.event_type in REFERENCE_EVENTS and e.payload.get("observed_config_hash"))
    ref_cfg = reference.payload.get("observed_config") or {}
    later = parse_utc(observed.observed_at_utc) > parse_utc(reference.recorded_at_utc)
    changed = bool(reference.payload.get("observed_config_hash") != observed.observed_config_hash and later)
    # Every decision knob that moved is recorded; only the configuration's own knobs (not the channel, which has no
    # priced effect) can be a step, a rollback or a drift.
    moved = frozenset(k for k in DECISION_KNOBS if later and obs_cfg.get(k) != ref_cfg.get(k))
    diff = frozenset(k for k in moved if k in HASHED_CONFIG_KNOBS)
    rolled = tuple(a for a in open_apps if diff and set(applied_sources(package, a.payload)) <= diff and back_at_source(package, a.payload, obs_cfg))
    still_open = tuple(a for a in open_apps if a not in rolled)
    rolled_knobs = {k for a in rolled for k in applied_sources(package, a.payload)}
    steps, pending, unexplained = set(), set(), False
    for knob in sorted(diff - rolled_knobs):
        item = next((i for i in step_results if i.get("knob") == knob), None)
        conformance = None if item is None else item.get("implementation_conformance")
        held = item is not None and any(a.payload.get("scenario_id") == item.get("scenario_id")
                                        and (a.payload.get("step") == item.get("step") or a.payload.get("application_scope") == "SCENARIO" and item.get("step") in {r.get("step") for r in a.payload.get("applied_step_refs") or []})
                                        for a in still_open)
        if item is None or held:
            unexplained = True   # a knob no step relates to, or an open step's value changed again
        elif item.get("authority_eligible") and conformance in APPLIED_CONFORMANCES:
            steps.add(item.get("step"))
        elif not item.get("authority_eligible") and item.get("independent_reason") in HELD_STEP_REASONS and conformance not in {None, rc.ImplementationConformance.NOT_APPLIED.value}:
            pending.add(knob)
        else:
            unexplained = True
    return PlanProgress(reference=reference, changed=changed, moved=moved, diff=diff, rolled=rolled, still_open=still_open,
                        steps=frozenset(steps), pending=frozenset(pending), unexplained=unexplained)


def closing_time(observed: ObservationAnalysis, application: rc.RealizationLedgerEntry) -> str:
    """When the drift or rollback *observed* shows took effect: the boundary of the configuration epoch that brought it.

    That is the observation's ``measurement.observed_epoch_start`` when it has a measurement, else its applied (or
    effective) time when that is after the start of *application*'s episode, else its observed time. The episode's
    interval ends strictly after it starts, so a closing time at or before the episode's start (a measured epoch that
    began before the application it would close, or an observation no later than it) is a malformed history and is
    refused (``InputError``) before any lifecycle event of the observation is decided (the run keeps it as ``halt``).
    """
    raw = observed.raw
    start_text = application.payload.get("effective_from_utc") or application.effective_at_utc
    start = parse_utc(start_text)
    measurement = raw.get("measurement")
    if isinstance(measurement, Mapping) and measurement.get("observed_epoch_start"):
        closed = utc_iso(measurement["observed_epoch_start"])
    else:
        stated = raw.get("applied_at_utc") or raw.get("effective_at_utc")
        closed = utc_iso(stated) if stated and parse_utc(stated) > start else observed.observed_at_utc
    if parse_utc(closed) <= start:
        raise InputError(f"the configuration change observed at {observed.observed_at_utc} took effect at {closed}, "
                         f"not after the application episode it closes (effective from {utc_iso(start_text)})")
    return closed


def _rollback_pending(entries: Any, episode_id: Optional[str]) -> bool:
    """Whether the latest rollback event on *episode_id* is a request (no rollback observed since it)."""
    kinds = [e.event_type for e in entries if e.application_episode_id == episode_id
             and e.event_type in (rc.EventType.ROLLBACK_REQUESTED, rc.EventType.ROLLBACK_OBSERVED)]
    return bool(kinds) and kinds[-1] == rc.EventType.ROLLBACK_REQUESTED


def _previous(previous_app: rc.RealizationLedgerEntry) -> ActiveApplication:
    """The previous run's application as the active episode (drift, or the same configuration observed again)."""
    return ActiveApplication(application_episode_id=previous_app.application_episode_id, payload=previous_app.payload, is_previous=True)


def regeneration_request(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], policy: Any, *, reason: str,
    evidence_id: Optional[str] = None,
) -> RegenerationRequest:
    """The regeneration request (RERUN-*) for *app_payload*'s actual configuration: its id and payload, which the engine
    records as REGENERATION_REQUESTED on the application's episode and writes as the RERUN file.

    The payload names the observed-configuration evidence the request was made from (*evidence_id*, ``None`` when the
    caller has none), the observation window (the observation's, else its observed time), the decision knobs whose actual
    value differs from the package's current configuration (``changed_knobs``) and the knobs the applied change
    invalidates (``impacted_knobs``). None of them is part of the request id."""
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
        "changed_knobs": changed_knobs(package, actual_cfg),
        "impacted_knobs": list(app_payload.get("invalidates") or []),
        "evidence_id": evidence_id,
        "observation_window": {
            "start_utc": observation.get("window_start_utc") or observation.get("observed_at_utc"),
            "end_utc": observation.get("window_end_utc") or observation.get("observed_at_utc"),
        },
        "policy_snapshot_id": policy.snapshot_id,
        "status": rc.RegenerationStatus.REQUESTED.value,
    }
    return RegenerationRequest(regeneration_request_id=rerun_id, application_episode_id=app_payload.get("application_episode_id"), payload=payload)


def changed_knobs(package: Mapping[str, Any], actual_cfg: Mapping[str, Any]) -> List[str]:
    """The decision knobs whose actual value differs from the package's current configuration (Photon is an observed
    fact, never a change); a knob the configuration does not state is not changed."""
    source = package["current_config"]
    return [knob for knob in DECISION_KNOBS if actual_cfg.get(knob) is not None and actual_cfg.get(knob) != normalize_value(knob, source.get(knob))]


def upstream_regeneration_request(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], policy: Any, *, evidence_id: Optional[str] = None,
) -> Optional[RegenerationRequest]:
    """The regeneration an invalidating or modified authoritative application requests once the governed
    remeasurement window (the step's ``remeasure_days``, else the policy's) has elapsed; ``None`` otherwise."""
    if app_payload.get("invalidates") or app_payload.get("implementation_conformance") == rc.ImplementationConformance.MODIFIED.value:
        days = int(observation.get("elapsed_days", 0) or 0)
        required_days = int(app_payload.get("remeasure_days") or policy.remeasure_days_after_invalidating_change)
        if days >= required_days:
            return regeneration_request(package, app_payload, observation, policy, reason="UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM", evidence_id=evidence_id)
    return None
