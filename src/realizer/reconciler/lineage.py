"""Reconciler across lineages: what the warehouse's realization lineages, taken together, mean for each of them.

An output directory can hold several lineages for one warehouse (later Publisher runs of the same epoch, cost-profile or
scope alternatives, the package that follows a configuration change, a canary rollback after its canary). Each run
decides, from its own lineage (its ledger and the applications it is about to record) and every other lineage the
Analyzer read, the events each lineage's ledger is missing:

* **Ownership.** One change (a knob set to a value, over an interval) belongs to one lineage: of the lineages whose
  plan step the change realizes (an authoritative application, EXACT or MODIFIED), the one whose package was generated
  most recently before the change; at equal generation time EXACT before MODIFIED, then the lowest package id. Every
  other such lineage records ``APPLICATION_CLAIMED_BY_OTHER_LINEAGE`` on its application episode, naming the owner; its
  realization stays observable, never claimable.
* **Supersession.** A lineage is superseded (``SOURCE_PACKAGE_SUPERSEDED`` and ``REPLACEMENT_PACKAGE_LINKED``) by the
  newest later package that is either of the same epoch (the Publisher saw the same configuration and recommended
  again: the older package was never applied before it) or a successor whose configuration change applied none of the
  lineage's steps (the configuration changed under it). A successor whose change applied the lineage's own step
  replaces it only when its expected savings are a material improvement (``savings_improvement_material``, the 5 percent
  churn rule): new package generation alone never stops a valid realization.
* **Succession.** A later package whose observed change starts from the lineage's epoch
  (``observed_change.previous_epoch_id``) is linked as its successor (``SUCCESSOR_PACKAGE_LINKED``, the first one).
* **Rollback by a later package.** When a later package's canary rollback step (step role ``SAFETY_ROLLBACK``) is
  applied, returning a knob to the value it had before an earlier lineage's application, and that package's observed
  change starts from the earlier lineage's epoch, the earlier lineage records ``ROLLBACK_OBSERVED`` at the time the
  rollback was applied.

Every decision is a function of the packages and the recorded applications only, and every event's times come from
the facts it follows from (``LineageEvent``), so realizing the same packages and observations in any order leaves the
same final decisions. The Reconciler decides; the Packager appends each event to its lineage's ledger.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.analysis import normalize_value, package_config, plan_steps, scenario_by_id
from realizer.common.utils import decimal, utc_iso
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.lineage import LineageEvent
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import RealizerPolicy, authority_eligible, savings_improvement_material
from realizer.reconciler.matching import APPLIED_CONFORMANCES, compare_step

AUTHORITATIVE_EVENTS = (rc.EventType.APPLICATION_OBSERVED, rc.EventType.MODIFIED_APPLICATION_OBSERVED)
# Entries that end an application's interval in its own lineage.
INTERVAL_END_EVENTS = (rc.EventType.DRIFT_DETECTED, rc.EventType.ROLLBACK_OBSERVED)
OWNERSHIP_RULE = "MOST_RECENT_PACKAGE_BEFORE_CHANGE"


def _time(value: Any) -> dt.datetime:
    return dt.datetime.fromisoformat(utc_iso(value).replace("Z", "+00:00"))


def _latest(*values: str) -> str:
    """The latest of several UTC times, as the ledger writes them."""
    return utc_iso(max(_time(v) for v in values))


@dataclass(frozen=True)
class Claim:
    """One knob of an authoritative application of a lineage: the change (knob, observed value), from when, and how
    it relates to the lineage's step."""
    realization_package_id: str
    recommendation_package_id: str
    generated: dt.datetime
    generated_at_utc: str
    application_episode_id: str
    knob: str
    value: Any
    source_value: Any
    conformance: str
    step_role: Optional[str]
    effective: dt.datetime
    effective_from_utc: str
    recorded_at_utc: str
    observed_config: Mapping[str, Any]
    end: Optional[dt.datetime]

    def overlaps(self, other: "Claim") -> bool:
        """Half-open intervals [effective, end) intersect (an open interval never ends)."""
        return (other.end is None or self.effective < other.end) and (self.end is None or other.effective < self.end)


@dataclass(frozen=True)
class Lineage:
    """One lineage as the decisions read it: its package's identity, epoch and generation time, and its claims."""
    realization_package_id: str
    recommendation_package_id: str
    package: Mapping[str, Any]
    epoch_id: Optional[str]
    previous_epoch_id: Optional[str]
    generated: dt.datetime
    generated_at_utc: str
    claims: Tuple[Claim, ...]


def generated_at(package: Mapping[str, Any]) -> str:
    """When the package was generated (its lineage starts then); the epoch start when the package has no time."""
    return utc_iso((package.get("provenance") or {}).get("generated_at") or ((package.get("warehouse") or {}).get("current_epoch") or {}).get("start_time")
                   or "1970-01-01T00:00:00Z")


def expected_savings(package: Mapping[str, Any]) -> Optional[Decimal]:
    """The recommended plan's expected annual savings (the scenario's total base, else its DBU base); ``None`` when the
    package does not price it."""
    scenario = scenario_by_id(package, (package.get("recommended_plan") or {}).get("scenario_id")) or {}
    savings = (scenario.get("portfolio_economics") or {}).get("savings_365d") or {}
    block = savings.get("total") or savings.get("dbu") or {}
    return decimal(block.get("base")) if isinstance(block, Mapping) else None


def _step_knob(package: Mapping[str, Any], scenario_id: Any, step: Any) -> Optional[Mapping[str, Any]]:
    scenario = scenario_by_id(package, scenario_id) or {}
    return next((s for s in scenario.get("execution_plan") or [] if s.get("step_type") == "CANDIDATE" and s.get("step") == step), None)


def _claims_of(rpkg: str, package: Mapping[str, Any], applications: Iterable[Tuple[str, Mapping[str, Any], str, str]],
               ends: Sequence[dt.datetime]) -> Tuple[Claim, ...]:
    """The claims of a lineage's authoritative applications (event type, payload, episode id, recorded time), the first
    application of each episode and knob only. A scenario-scope application claims each knob of its applied steps."""
    generated = generated_at(package)
    out: Dict[Tuple[str, str], Claim] = {}
    for event_type, payload, episode, recorded in applications:
        if event_type not in {e.value for e in AUTHORITATIVE_EVENTS} or not payload.get("authority_eligible") or not episode:
            continue
        observed = payload.get("observed_config") or {}
        if payload.get("application_scope") == "SCENARIO":
            parts = []
            for ref in payload.get("applied_step_refs") or []:
                step = _step_knob(package, ref.get("scenario_id"), ref.get("step"))
                if step is not None and ref.get("conformance") in APPLIED_CONFORMANCES:
                    knob = str(step.get("knob"))
                    parts.append((knob, observed.get(knob), step.get("current_value"), ref.get("conformance"), None))
        elif payload.get("implementation_conformance") in APPLIED_CONFORMANCES and payload.get("knob"):
            knob = str(payload["knob"])
            parts = [(knob, payload.get("observed_value"), payload.get("source_value"), payload["implementation_conformance"], payload.get("step_role"))]
        else:
            parts = []
        effective = _time(payload.get("effective_from_utc") or recorded)
        end = min((t for t in ends if t > effective), default=None)
        for knob, value, source, conformance, role in parts:
            key = (episode, knob)
            if key in out:
                continue
            out[key] = Claim(
                realization_package_id=rpkg, recommendation_package_id=package["recommendation_package_id"],
                generated=_time(generated), generated_at_utc=generated, application_episode_id=episode, knob=knob,
                value=normalize_value(knob, value), source_value=normalize_value(knob, source), conformance=str(conformance),
                step_role=role, effective=effective, effective_from_utc=utc_iso(effective), recorded_at_utc=utc_iso(recorded),
                observed_config=dict(observed), end=end,
            )
    return tuple(out.values())


def lineage_of(rpkg: str, package: Mapping[str, Any], entries: Sequence[rc.RealizationLedgerEntry],
               planned: Iterable[Tuple[str, Mapping[str, Any], str, str]] = (), planned_ends: Sequence[str] = ()) -> Lineage:
    """The decision view of a lineage from its ledger *entries* and, for the run's own lineage, the applications and
    interval ends the run is about to record (*planned*, *planned_ends*)."""
    recorded = [(e.event_type.value, e.payload, e.application_episode_id, e.recorded_at_utc) for e in entries]
    ends = [_time(e.effective_at_utc) for e in entries if e.event_type in INTERVAL_END_EVENTS] + [_time(t) for t in planned_ends]
    current = (package.get("warehouse") or {}).get("current_epoch") or {}
    generated = generated_at(package)
    return Lineage(
        realization_package_id=rpkg, recommendation_package_id=package["recommendation_package_id"], package=package,
        epoch_id=current.get("epoch_id"), previous_epoch_id=(package.get("observed_change") or {}).get("previous_epoch_id"),
        generated=_time(generated), generated_at_utc=generated, claims=_claims_of(rpkg, package, [*recorded, *planned], ends),
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _owner_rank(claim: Claim) -> Tuple[float, int, str]:
    """Most recently generated first, then EXACT before MODIFIED, then the lowest package id."""
    return (-claim.generated.timestamp(), 0 if claim.conformance == rc.ImplementationConformance.EXACT.value else 1, claim.recommendation_package_id)


def ownership(lineages: Sequence[Lineage]) -> List[LineageEvent]:
    """``APPLICATION_CLAIMED_BY_OTHER_LINEAGE`` for every claim of a change another lineage owns.

    Two claims are of the same change when they set the same knob to the same value over intersecting intervals. The
    owner is chosen among the claims of packages generated before the change (a package generated after it already
    started from the applied value)."""
    claims = [c for lineage in lineages for c in lineage.claims]
    events = []
    for claim in claims:
        rivals = [o for o in claims if o.realization_package_id != claim.realization_package_id and o.knob == claim.knob
                  and o.value == claim.value and claim.overlaps(o)]
        candidates = [c for c in [claim, *rivals] if c.generated <= c.effective]
        if not rivals or not candidates:
            continue
        owner = min(candidates, key=_owner_rank)
        if owner.realization_package_id == claim.realization_package_id:
            continue
        events.append(LineageEvent(
            realization_package_id=claim.realization_package_id, event_type=rc.EventType.APPLICATION_CLAIMED_BY_OTHER_LINEAGE,
            effective_at_utc=claim.effective_from_utc, recorded_at_utc=_latest(claim.recorded_at_utc, owner.recorded_at_utc),
            application_episode_id=claim.application_episode_id,
            payload={
                "knob": claim.knob, "observed_value": claim.value, "effective_from_utc": claim.effective_from_utc,
                "implementation_conformance": claim.conformance,
                "owner_realization_package_id": owner.realization_package_id,
                "owner_recommendation_package_id": owner.recommendation_package_id,
                "owner_generated_at_utc": owner.generated_at_utc,
                "owner_implementation_conformance": owner.conformance,
                "owner_application_episode_id": owner.application_episode_id,
                "rule": OWNERSHIP_RULE,
            },
        ))
    return events


def change_applies_steps(earlier: Mapping[str, Any], later: Mapping[str, Any]) -> bool:
    """Whether the configuration *later* was generated from applies (EXACT or MODIFIED) an authority-eligible step of
    *earlier*'s plan."""
    config = package_config(later)
    for step in plan_steps(earlier):
        conformance, _ = compare_step(step, config)
        if authority_eligible(step) and conformance is not None and conformance.value in APPLIED_CONFORMANCES:
            return True
    return False


def supersession(lineages: Sequence[Lineage], policy: RealizerPolicy) -> List[LineageEvent]:
    """``SOURCE_PACKAGE_SUPERSEDED`` and ``REPLACEMENT_PACKAGE_LINKED`` naming the newest later package that replaces
    each lineage (same epoch; or a successor whose change applied none of its steps, or that is a material improvement)."""
    events = []
    for lineage in lineages:
        replacing = []
        for later in lineages:
            if later is lineage or later.generated <= lineage.generated:
                continue
            if later.epoch_id is not None and later.epoch_id == lineage.epoch_id:
                replacing.append((later, "NEWER_PACKAGE_SAME_EPOCH"))
            elif later.previous_epoch_id is not None and later.previous_epoch_id == lineage.epoch_id:
                if not change_applies_steps(lineage.package, later.package):
                    replacing.append((later, "CONFIGURATION_CHANGED"))
                elif savings_improvement_material(expected_savings(lineage.package), expected_savings(later.package), policy):
                    replacing.append((later, "MATERIAL_IMPROVEMENT"))
        if not replacing:
            continue
        later, reason = min(replacing, key=lambda x: (-x[0].generated.timestamp(), x[0].recommendation_package_id))
        payload = {
            "replacement_package_id": later.recommendation_package_id,
            "replacement_realization_package_id": later.realization_package_id,
            "replacement_generated_at_utc": later.generated_at_utc,
            "reason": reason,
        }
        for event_type in (rc.EventType.SOURCE_PACKAGE_SUPERSEDED, rc.EventType.REPLACEMENT_PACKAGE_LINKED):
            events.append(LineageEvent(realization_package_id=lineage.realization_package_id, event_type=event_type,
                                       effective_at_utc=later.generated_at_utc, recorded_at_utc=later.generated_at_utc, payload=payload))
    return events


def succession(lineages: Sequence[Lineage]) -> List[LineageEvent]:
    """``SUCCESSOR_PACKAGE_LINKED`` naming each lineage's first successor: the earliest later package whose observed
    change starts from the lineage's epoch."""
    events = []
    for lineage in lineages:
        successors = [x for x in lineages if x is not lineage and x.previous_epoch_id is not None
                      and x.previous_epoch_id == lineage.epoch_id and x.generated > lineage.generated]
        if not successors:
            continue
        first = min(successors, key=lambda x: (x.generated.timestamp(), x.recommendation_package_id))
        events.append(LineageEvent(
            realization_package_id=lineage.realization_package_id, event_type=rc.EventType.SUCCESSOR_PACKAGE_LINKED,
            effective_at_utc=first.generated_at_utc, recorded_at_utc=first.generated_at_utc,
            payload={
                "successor_package_id": first.recommendation_package_id,
                "successor_realization_package_id": first.realization_package_id,
                "successor_generated_at_utc": first.generated_at_utc,
                "successor_epoch_id": first.epoch_id,
                "previous_epoch_id": lineage.epoch_id,
            },
        ))
    return events


def rollbacks(lineages: Sequence[Lineage]) -> List[LineageEvent]:
    """``ROLLBACK_OBSERVED`` on an earlier lineage's application when a later package's applied canary rollback returns
    the knob to the value it had before that application, and the later package's observed change starts from the
    earlier lineage's epoch."""
    events = []
    for later in lineages:
        for rollback in later.claims:
            if rollback.step_role != rc.StepRole.SAFETY_ROLLBACK.value or later.previous_epoch_id is None:
                continue
            for earlier in lineages:
                if earlier is later or earlier.epoch_id != later.previous_epoch_id:
                    continue
                for claim in earlier.claims:
                    if claim.knob != rollback.knob or claim.effective >= rollback.effective or claim.source_value != rollback.value:
                        continue
                    events.append(LineageEvent(
                        realization_package_id=earlier.realization_package_id, event_type=rc.EventType.ROLLBACK_OBSERVED,
                        effective_at_utc=rollback.effective_from_utc, recorded_at_utc=_latest(claim.recorded_at_utc, rollback.recorded_at_utc),
                        application_episode_id=claim.application_episode_id,
                        payload={
                            "observed_config": dict(rollback.observed_config), "reason": "CANARY_ROLLBACK",
                            "knob": rollback.knob, "observed_value": rollback.value, "effective_from_utc": rollback.effective_from_utc,
                            "rollback_recommendation_package_id": later.recommendation_package_id,
                            "rollback_realization_package_id": later.realization_package_id,
                            "rollback_application_episode_id": rollback.application_episode_id,
                        },
                    ))
    return events


def decide(lineages: Sequence[Lineage], policy: RealizerPolicy) -> Tuple[LineageEvent, ...]:
    """Every cross-lineage event of the lineages, in a fixed order (by lineage, then time, then event)."""
    if len(lineages) < 2:
        return ()
    events = ownership(lineages) + supersession(lineages, policy) + succession(lineages) + rollbacks(lineages)
    order = {e: n for n, e in enumerate(rc.EventType)}
    return tuple(sorted(events, key=lambda e: (e.realization_package_id, _time(e.recorded_at_utc), order[e.event_type], rc.canonical_json(e.payload))))


def resolve(context: RunContext, analysis: RealizationAnalysis, result: ReconciliationResult) -> Tuple[LineageEvent, ...]:
    """The cross-lineage events of the run's warehouse: its own lineage (the ledger it started from plus the
    applications, drift and rollback this run records) and every other lineage the Analyzer read."""
    request = context.request
    observed = analysis.observation
    recorded = observed.recorded_at_utc if observed is not None else None
    planned = [(p.event_type.value, p.payload, p.application_episode_id, recorded) for p in result.applications] if recorded else []
    ends = [observed.effective_at_utc] if observed is not None and (result.drift is not None or result.rollback_observed is not None) else []
    own = lineage_of(context.realization_package_id, request.package, analysis.history.entries, planned, ends)
    others = [lineage_of(r.realization_package_id, r.package, r.entries) for r in analysis.warehouse.lineages]
    return decide([own, *others], context.policy)
