"""Reviewer validation: the validation episode of the active application.

The VAL id and the VALIDATION_READY, VALIDATION_STARTED and VALIDATION_COMPLETED payloads, built from the Governor's
readiness and performance verdicts over the observation window. Moved unchanged from the single-file Realizer's
observation processing, in its order. The episode states the verdicts; it adds no check of its own.
"""
from __future__ import annotations

from typing import Any, Dict

from realizer.contracts.analysis import ObservationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import make_val_id


def plan_episode(fields: Dict[str, Any], application_episode_id: str, observed: ObservationAnalysis, governance: GovernanceResult) -> None:
    """Decide the validation episode of *application_episode_id* into *fields*, in recording order: the VAL id, the
    ready payload, the started payload, then (after reading the performance verdict) the completed payload.

    Whatever is in *fields* when a step raises was decided before it (``ValidationEpisode`` takes them as they are).
    """
    observation, policy = observed.raw, governance.policy
    effective, obs_time = observed.effective_at_utc, observed.observed_at_utc
    ready = governance.readiness.get()  # read by the Reconciler already (validation is due), so it cannot fail here
    val_id = make_val_id(application_episode_id, observation, policy)
    fields["validation_episode_id"] = val_id
    fields["ready_payload"] = {"validation_episode_id": val_id, "readiness": ready, "policy_snapshot_id": policy.snapshot_id}
    fields["started_payload"] = {"validation_episode_id": val_id, "window_start_utc": observation.get("window_start_utc") or effective, "window_end_utc": observation.get("window_end_utc") or obs_time}
    validation = governance.performance.get()
    fields["completed_payload"] = {
        "state": validation["state"],
        "validation_episode_id": val_id,
        "readiness": ready,
        "hard_guardrails_pass": validation["hard_guardrails_pass"],
        "rules": validation["rules"],
        "policy_snapshot_id": policy.snapshot_id,
        "window_start_utc": observation.get("window_start_utc") or effective,
        "window_end_utc": observation.get("window_end_utc") or obs_time,
    }
