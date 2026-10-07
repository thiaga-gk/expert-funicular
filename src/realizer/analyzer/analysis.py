"""Analyzer logic: normalize one observation against its source package, hash configurations, view the package's plan
and read the history a run starts from.

The configuration vocabulary (knobs, size and type spellings), the source-package view (``package_config``,
``scenario_by_id``, ``candidate_steps`` ...) and the observation normalization are moved unchanged from the
single-file Realizer. ``analyze_observation`` and ``config_drift`` are the engine's own first steps, in the same order
and with the same failure modes, gathered here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.common.utils import utc_iso
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
    block = savings.get("total") or savings.get("dbu") or {}
    base = block.get("base") if isinstance(block, Mapping) else None
    objectives = set(candidate.get("objectives") or [])
    if "PERFORMANCE" in objectives and "COST" not in objectives and (base is None or base < 0):
        return rc.StepRole.PERFORMANCE.value
    if base is not None and base == 0:
        return rc.StepRole.CAPACITY.value
    return rc.StepRole.SAVINGS.value


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


def summarize_history(entries: Sequence[rc.RealizationLedgerEntry]) -> RealizationHistory:
    """The history view of a loaded ledger (recorded order): its latest application and latest measurement."""
    return RealizationHistory(
        entries=tuple(entries),
        previous_application=latest_event(entries, rc.EventType.APPLICATION_OBSERVED, rc.EventType.MODIFIED_APPLICATION_OBSERVED),
        prior_measurement=latest_event(entries, rc.EventType.ECONOMIC_MEASUREMENT_RECORDED),
    )


def config_drift(history: RealizationHistory, observed_hash: str, observed_at_utc: str) -> bool:
    """Whether the observed configuration differs from the previous application's and is newer than its record."""
    previous = history.previous_application
    return bool(previous and previous.payload.get("observed_config_hash") != observed_hash and observed_at_utc > previous.recorded_at_utc)
