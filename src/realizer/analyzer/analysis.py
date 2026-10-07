"""Analyzer logic: normalize one observation against its source package, hash configurations, view the package's plan
and read the history a run starts from.

The configuration vocabulary (knobs, size and type spellings), the source-package view (``package_config``,
``scenario_by_id``, ``candidate_steps`` ...) and the observation normalization are moved unchanged from the
single-file Realizer. ``analyze_observation`` and ``config_drift`` are the engine's own first steps, in the same order
and with the same failure modes, gathered here.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.common.errors import InputError
from realizer.common.utils import decimal, utc_iso
from realizer.config.settings import CONFIG_KNOBS, HASHED_CONFIG_KNOBS
from realizer.contracts import realization as rc
from realizer.contracts.analysis import ObservationAnalysis, RealizationHistory
from realizer.contracts.realization import make_evidence_id
from realizer.packager.ledger import latest_event


def config_hash(config: Mapping[str, Any]) -> str:
    """The hash of the configuration a decision is made on (``HASHED_CONFIG_KNOBS``): the channel and Photon are recorded
    in the observed configuration but never change it."""
    return "SHA256:" + rc.sha256_hex({k: normalize_value(k, config.get(k)) for k in HASHED_CONFIG_KNOBS})


def normalize_value(knob: str, value: Any) -> Any:
    """One spelling per configuration value, the Publisher's: a size upper-cased with ``-`` as ``_`` (``X-Large`` is
    ``X_LARGE``), a warehouse type or channel upper-cased, surrounding spaces dropped. Numbers and booleans pass."""
    if not isinstance(value, str):
        return value
    text = value.strip().upper()
    return text.replace("-", "_") if knob == "warehouse_size" else text if knob in {"warehouse_type", "warehouse_channel"} else value


def photon_bool_from_package(package: Mapping[str, Any]) -> Optional[bool]:
    status = str(((package.get("current_config") or {}).get("photon") or {}).get("status") or "UNKNOWN").upper()
    if status == "ENABLED":
        return True
    if status == "DISABLED":
        return False
    return None


def package_config(package: Mapping[str, Any]) -> Dict[str, Any]:
    c = package["current_config"]
    config = {k: normalize_value(k, c.get(k)) for k in CONFIG_KNOBS if k != "enable_photon"}
    config["enable_photon"] = photon_bool_from_package(package)
    return config


def observed_config(observation: Mapping[str, Any], package: Mapping[str, Any]) -> Dict[str, Any]:
    raw = dict(observation.get("config") or {})
    base = package_config(package)
    # Observation is a full current snapshot semantically; for test/replay
    # convenience omitted fields inherit source state, not recommended state.
    return {k: normalize_value(k, raw[k]) if k in raw else base.get(k) for k in CONFIG_KNOBS}


def scenario_by_id(package: Mapping[str, Any], scenario_id: Optional[str]) -> Optional[Mapping[str, Any]]:
    if scenario_id is None:
        return None
    return next((s for s in package.get("scenarios", []) if s.get("scenario_id") == scenario_id), None)


def recommended_scenario(package: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    return scenario_by_id(package, (package.get("recommended_plan") or {}).get("scenario_id"))

def candidate_by_id(package: Mapping[str, Any], candidate_id: Optional[str]) -> Optional[Mapping[str, Any]]:
    if candidate_id is None:
        return None
    return next((c for c in package.get("candidates", []) if c.get("candidate_id") == candidate_id), None)


def candidate_steps(package: Mapping[str, Any], scenario_id: Optional[str] = None) -> List[Mapping[str, Any]]:
    scenario = scenario_by_id(package, scenario_id) if scenario_id else recommended_scenario(package)
    if scenario is None:
        return []
    return [x for x in scenario.get("execution_plan", []) if x.get("step_type") == "CANDIDATE"]


# The Publisher's blocker on a candidate that rolls back a canary whose guard tripped.
CANARY_ROLLBACK_BLOCKER = "CANARY_GUARDRAIL_BREACHED"


def step_role(package: Mapping[str, Any], step: Mapping[str, Any]) -> str:
    """What a CANDIDATE step is for (``StepRole``), from its candidate: ``SAFETY_ROLLBACK`` when the candidate carries
    the canary-rollback blocker; ``PERFORMANCE`` when its objectives are a performance fix and it is priced as a cost
    increase (or not priced); ``CAPACITY`` when it is priced at exactly zero (a cost-guardrail cut); ``SAVINGS``
    otherwise."""
    candidate = candidate_by_id(package, step.get("candidate_id")) or {}
    if CANARY_ROLLBACK_BLOCKER in (candidate.get("blockers") or []):
        return rc.StepRole.SAFETY_ROLLBACK.value
    savings = ((candidate.get("independent_economics") or {}).get("savings_365d") or {})
    # The role is the step's intent, its sign: the DBU range gives it when the package has no total (the denominator
    # never falls back that way).
    block = savings.get("total") or savings.get("dbu") or {}
    base = decimal(block.get("base")) if isinstance(block, Mapping) else None
    objectives = set(candidate.get("objectives") or [])
    if "PERFORMANCE" in objectives and "COST" not in objectives and (base is None or base < 0):
        return rc.StepRole.PERFORMANCE.value
    if base is not None and base == 0:
        return rc.StepRole.CAPACITY.value
    return rc.StepRole.SAVINGS.value


def plan_steps(package: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Every scenario's CANDIDATE steps, the recommended plan's first, then the other scenarios in package order."""
    recommended = (package.get("recommended_plan") or {}).get("scenario_id")
    others = [s.get("scenario_id") for s in package.get("scenarios") or [] if s.get("scenario_id") != recommended]
    return [step for sid in [recommended] + others if sid is not None for step in candidate_steps(package, sid)]


def scenario_remeasure_days(package: Mapping[str, Any], scenario_id: Optional[str] = None) -> Optional[Any]:
    """The remeasurement window (days) of the scenario's REMEASURE step; ``None`` when the scenario has none."""
    scenario = scenario_by_id(package, scenario_id) if scenario_id else recommended_scenario(package)
    if scenario is None:
        return None
    return next((((x.get("validation") or {}).get("remeasure_days")) for x in scenario.get("execution_plan", []) if x.get("step_type") == "REMEASURE"), None)


def analyze_observation(package: Mapping[str, Any], observation: Mapping[str, Any]) -> ObservationAnalysis:
    """The normalized view of *observation*: its UTC times, observed and source configurations with their hashes and
    the observed-configuration evidence record (REVID-*).

    Computed in the engine's original order, so a malformed observation raises the same exception: a missing time
    (``ValueError``), a non-mapping ``config`` or ``current_config``, a missing ``warehouse`` key, a value the
    canonical hash cannot serialize.
    """
    obs_time = utc_iso(observation.get("observed_at_utc") or observation.get("recorded_at_utc"))
    recorded = utc_iso(observation.get("recorded_at_utc") or obs_time)
    effective = utc_iso(observation.get("applied_at_utc") or observation.get("effective_at_utc") or obs_time)
    obs_cfg = observed_config(observation, package)
    source_cfg = package_config(package)
    source_hash, observed_hash = config_hash(source_cfg), config_hash(obs_cfg)
    evidence_payload = {"config": obs_cfg, "observed_at_utc": obs_time, "source": observation.get("source", "NORMALIZED_OBSERVATION")}
    revid = make_evidence_id("OBSERVED_CONFIGURATION", package, observation, evidence_payload)
    evidence_record = {
        "evidence_id": revid,
        "evidence_type": "OBSERVED_CONFIGURATION",
        "workspace_id": package["warehouse"]["workspace_id"],
        "warehouse_id": package["warehouse"]["warehouse_id"],
        "source": observation.get("source", "NORMALIZED_OBSERVATION"),
        "observation_window": observation.get("window") or {"end_utc": obs_time},
        "captured_at_utc": recorded,
        "quality": str(observation.get("evidence_quality", "SUFFICIENT")).upper(),
        "payload_hash": "SHA256:" + rc.sha256_hex(evidence_payload),
        "payload": evidence_payload,
    }
    return ObservationAnalysis(
        raw=observation,
        observed_at_utc=obs_time,
        recorded_at_utc=recorded,
        effective_at_utc=effective,
        observed_config=obs_cfg,
        source_config=source_cfg,
        source_config_hash=source_hash,
        observed_config_hash=observed_hash,
        evidence_id=revid,
        evidence_record=evidence_record,
    )


def instant(value: Any) -> dt.datetime:
    """A UTC ISO time as an aware datetime, for comparing times (their text does not order with fractional seconds)."""
    return dt.datetime.fromisoformat(utc_iso(value).replace("Z", "+00:00"))


# The lifecycle events that end an application episode: once one is recorded on the episode at or after its application,
# the episode is closed and never absorbs a new application.
CLOSING_EVENTS = (rc.EventType.DRIFT_DETECTED, rc.EventType.ROLLBACK_OBSERVED)


def closing_event(entries: Sequence[rc.RealizationLedgerEntry], application: Optional[rc.RealizationLedgerEntry], *,
                  first: bool = False) -> Optional[rc.RealizationLedgerEntry]:
    """The latest (*first*: the earliest) drift or observed rollback recorded on *application*'s episode at or after the
    application (*entries* in recorded order); ``None`` while the episode is open."""
    if application is None:
        return None
    start = instant(application.recorded_at_utc)
    closing = [e for e in entries if e.event_type in CLOSING_EVENTS and e.application_episode_id == application.application_episode_id
               and instant(e.recorded_at_utc) >= start]
    return (closing[0] if first else closing[-1]) if closing else None


def summarize_history(entries: Sequence[rc.RealizationLedgerEntry]) -> RealizationHistory:
    """The history view of a loaded ledger (recorded order): its latest application, whether that application's episode
    is closed, and its latest measurement."""
    previous = latest_event(entries, rc.EventType.APPLICATION_OBSERVED, rc.EventType.MODIFIED_APPLICATION_OBSERVED)
    return RealizationHistory(
        entries=tuple(entries),
        previous_application=previous,
        prior_measurement=latest_event(entries, rc.EventType.ECONOMIC_MEASUREMENT_RECORDED),
        closure=closing_event(entries, previous),
    )


def applied_sources(package: Mapping[str, Any], app_payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The source value (the package's, in the Publisher's spelling) of each knob an application changed: the step's knob
    for a step application, every applied step's knob for a scenario-scope one."""
    if app_payload.get("application_scope") == "SCENARIO":
        steps = {step.get("step"): step for step in candidate_steps(package, app_payload.get("scenario_id"))}
        applied = [steps[ref.get("step")] for ref in app_payload.get("applied_step_refs") or [] if ref.get("step") in steps]
        pairs = [(step.get("knob"), step.get("current_value")) for step in applied]
    else:
        pairs = [(app_payload.get("knob"), app_payload.get("source_value"))]
    return {str(knob): normalize_value(str(knob), value) for knob, value in pairs if knob is not None}


def back_at_source(package: Mapping[str, Any], app_payload: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """Whether the observed configuration rolled the application back: every knob it changed is at its source value
    again. A package's own safety-rollback step is an application like any other, so rolling it back means returning to
    the value it replaced."""
    sources = applied_sources(package, app_payload)
    return bool(sources) and all(observed.get(knob) == value for knob, value in sources.items())


def check_rollback_flag(package: Mapping[str, Any], history: RealizationHistory, observation: ObservationAnalysis) -> None:
    """Refuse (``InputError``) an observation whose ``rollback_observed`` flag contradicts its configuration.

    The configuration governs whether a change was rolled back; the flag is only checked against it. A flag set while the
    configuration is not back at the source value of the previous application (with no application yet: while a knob
    of the plan's steps differs from the package's configuration) is refused before anything for the observation is
    written. A flag left unset on a configuration that is back at the source value is not refused: it is a rollback.
    """
    if not observation.raw.get("rollback_observed"):
        return
    previous = history.previous_application
    if previous is not None:
        rolled_back = back_at_source(package, previous.payload, observation.observed_config)
    else:
        source = package_config(package)
        knobs = {str(step.get("knob")) for step in plan_steps(package) if step.get("knob") is not None}
        rolled_back = all(observation.observed_config.get(knob) == source.get(knob) for knob in knobs)
    if not rolled_back:
        raise InputError("observation rollback_observed contradicts its configuration, which is not back at the source value of the applied change")


def config_drift(history: RealizationHistory, observed_hash: str, observed_at_utc: str) -> bool:
    """Whether the observed configuration differs from the previous application's and is newer than its record."""
    previous = history.previous_application
    return bool(previous and previous.payload.get("observed_config_hash") != observed_hash and observed_at_utc > previous.recorded_at_utc)
