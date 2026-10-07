"""Realizer Contract v1.0.0: reconciliation states (step conformance, application and plan progress, source-package
disposition, regeneration), and the Reconciler's hand-off (``ReconciliationResult``).

The enums describe how the observed configuration relates to the immutable PKG plan and where its lifecycle stands.
They are part of the public Realizer Contract and are re-exported by ``realizer.contracts.realization``, which presents
the complete v1.0.0 surface. The hand-off dataclasses are internal values passed from the Reconciler to the engine; they
are not public contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # annotations only: realization.py imports this module for the enums
    from realizer.common.utils import Deferred
    from realizer.contracts.lineage import LineageEvent
    from realizer.contracts.realization import EventType


class SourcePackageDisposition(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class ImplementationConformance(str, Enum):
    EXACT = "EXACT"
    MODIFIED = "MODIFIED"
    NOT_APPLIED = "NOT_APPLIED"
    CONTRARY = "CONTRARY"
    UNRELATED = "UNRELATED"


class ApplicationState(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    DRIFTED = "DRIFTED"


class PlanProgressState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class StepRole(str, Enum):
    """What a plan step is for, which decides how its application is judged: a saving (realized against its published
    range), a performance fix or a capacity guardrail (judged on queueing and p95, its cost never a saving), or a safety
    rollback (recorded, never measured or claimed)."""
    SAVINGS = "SAVINGS"
    PERFORMANCE = "PERFORMANCE"
    CAPACITY = "CAPACITY"
    SAFETY_ROLLBACK = "SAFETY_ROLLBACK"


class RegenerationStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"


@dataclass(frozen=True)
class PlanMatch:
    """How the observed configuration relates to the plan of the selected scenario.

    ``selected_scenario_id`` is the observation's ``applied_scenario_id``, else the recommended plan's scenario.
    ``step_matches`` holds one match per CANDIDATE step of that scenario, in plan order (``choose_application``'s
    dictionaries: conformance, modification relation, authority, source/recommended/observed values ...); ``primary``
    is the first authority-eligible step applied exactly, modified or contrary (``None`` when there is none), and
    ``authority_changes`` every such step. ``scenario_scope`` is true when the observation is one scenario-scope
    application: more than one authority change, a warehouse-type migration scenario, or a scenario other than the
    recommended one.
    """
    selected_scenario_id: Any
    step_matches: Tuple[Mapping[str, Any], ...]
    primary: Optional[Mapping[str, Any]]
    authority_changes: Tuple[Mapping[str, Any], ...]
    scenario_scope: bool


@dataclass(frozen=True)
class PlannedApplication:
    """One application event to record: the scenario-scope application or one changed plan step (APP-* episode)."""
    event_type: "EventType"
    application_episode_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ActiveApplication:
    """The application episode this observation continues or opens, which validation and measurement attach to.

    ``is_previous`` is true when it is the previous run's application (configuration drift, or the same configuration
    observed again), false when it is an application this run records.
    """
    application_episode_id: str
    payload: Mapping[str, Any]
    is_previous: bool


@dataclass(frozen=True)
class RegenerationRequest:
    """A request for fresh Publisher analysis from the actual configuration (RERUN-*): its id, the application episode
    it belongs to and the payload recorded in the ledger and written as the RERUN file."""
    regeneration_request_id: str
    application_episode_id: Optional[str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReconciliationResult:
    """Reconciler output: the lifecycle facts of one observation, in the order they are recorded.

    Lifecycle events, each ``None`` (or empty) when it does not occur: ``drift`` (DRIFT_DETECTED payload, on the
    previous application's episode ``drift_episode_id``), ``applications`` (the scenario-scope application first, then
    the changed steps in plan order), ``intent`` (INTENT_RECORDED payload, on ``intent_episode_id``),
    ``rollback_requested`` and ``rollback_observed`` (on ``rollback_episode_id``) and ``replacement_package_id``
    (SOURCE_PACKAGE_SUPERSEDED, then REPLACEMENT_PACKAGE_LINKED). ``active`` is the application episode in force after
    them.

    Then exactly one of: ``terminal_regeneration`` (configuration drift or a contrary application: the run ends with a
    regeneration request), ``validation_due`` (an active application whose evidence is ready: the validation episode
    follows) or neither. ``upstream_regeneration`` is the regeneration an invalidating or modified application requests
    after its measurement once the remeasurement window has passed; it is held lazily because the run reaches it only
    after validation and measurement.

    ``lineage_events`` are the events that follow from the warehouse's lineages taken together and from a policy
    change: ownership of a change several lineages applied, supersession, successor links, a rollback by a later
    package, and a PolicyDiff on the active episode. Each names the lineage whose ledger it goes to (the run's own or
    another); they are recorded after the run's own events, and only when nothing halted.

    ``halt`` is the exception reconciliation raised, if any. Everything above it was decided before the exception, so
    the events present are those recorded before the failure; the engine records them and then raises ``halt``.
    ``match`` is ``None`` for a lineage-only run (no observation) and when matching itself failed.
    """
    match: Optional[PlanMatch] = None
    drift: Optional[Mapping[str, Any]] = None
    drift_episode_id: Optional[str] = None
    applications: Tuple[PlannedApplication, ...] = ()
    active: Optional[ActiveApplication] = None
    intent: Optional[Mapping[str, Any]] = None
    intent_episode_id: Optional[str] = None
    rollback_requested: Optional[Mapping[str, Any]] = None
    rollback_observed: Optional[Mapping[str, Any]] = None
    rollback_episode_id: Optional[str] = None
    replacement_package_id: Any = None
    terminal_regeneration: Optional[RegenerationRequest] = None
    validation_due: bool = False
    upstream_regeneration: Optional["Deferred[Optional[RegenerationRequest]]"] = None
    halt: Optional[Exception] = None
    lineage_events: Tuple["LineageEvent", ...] = ()


# ``__all__`` is the public Realizer Contract surface (the enums, re-exported by ``realization.py``); the hand-off
# dataclasses above are internal values and are imported by name.
__all__ = [
    "SourcePackageDisposition", "ImplementationConformance", "ApplicationState", "PlanProgressState",
    "RegenerationStatus", "StepRole",
]
