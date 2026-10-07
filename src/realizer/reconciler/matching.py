"""Reconciler matching: how the observed configuration relates to the published plan.

Step matching (``compare_step``, with ``_directional_relation`` for ordered knobs), plan matching
(``choose_application``: one match per CANDIDATE step of the selected scenario and the primary authoritative change)
and the plan-authority view of one observation (``match``: the selected scenario, its authority changes and whether
the observation is one scenario-scope application). Moved unchanged from the single-file Realizer, in its order.
"""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from realizer.analyzer.analysis import candidate_steps, normalize_value, recommended_scenario, scenario_by_id
from realizer.config.settings import SIZE_ORDER
from realizer.contracts import realization as rc
from realizer.contracts.reconciliation import PlanMatch
from realizer.governor.policy import authority_eligible


def _directional_relation(source: Any, recommended: Any, observed: Any) -> Tuple[rc.ImplementationConformance, Optional[str]]:
    try:
        s, r, o = Decimal(str(source)), Decimal(str(recommended)), Decimal(str(observed))
    except Exception:
        return rc.ImplementationConformance.UNRELATED, None
    if o == s:
        return rc.ImplementationConformance.NOT_APPLIED, None
    if o == r:
        return rc.ImplementationConformance.EXACT, None
    d_rec, d_obs = r - s, o - s
    if d_rec == 0:
        return rc.ImplementationConformance.UNRELATED, None
    if (d_rec > 0 and d_obs > 0) or (d_rec < 0 and d_obs < 0):
        relation = "LESS_AGGRESSIVE" if abs(d_obs) < abs(d_rec) else "MORE_AGGRESSIVE" if abs(d_obs) > abs(d_rec) else "ALTERNATE_SAME_INTENT"
        return rc.ImplementationConformance.MODIFIED, relation
    return rc.ImplementationConformance.CONTRARY, None


# Auto-stop 0 means the warehouse never stops: for the direction of a change it is longer than any auto-stop value.
NEVER_STOP_MINUTES = 10 ** 9


def _auto_stop_minutes(value: Any) -> Any:
    return NEVER_STOP_MINUTES if value == 0 else value


def compare_step(step: Mapping[str, Any], obs_config: Mapping[str, Any]) -> Tuple[Optional[rc.ImplementationConformance], Optional[str]]:
    """How the observed value of the step's knob relates to the step: EXACT, NOT_APPLIED, MODIFIED (same direction,
    with its relation), CONTRARY or UNRELATED; ``None`` when the knob was not observed. Package and observed values are
    compared in the Publisher's spelling (``normalize_value``)."""
    knob = str(step.get("knob"))
    source = normalize_value(knob, step.get("current_value"))
    recommended = normalize_value(knob, step.get("recommended_value"))
    observed = obs_config.get(knob)
    if observed is None:
        return None, None
    if observed == recommended:
        return rc.ImplementationConformance.EXACT, None
    if observed == source:
        return rc.ImplementationConformance.NOT_APPLIED, None
    if knob == "auto_stop_minutes":
        return _directional_relation(*(_auto_stop_minutes(x) for x in (source, recommended, observed)))
    if knob in {"min_clusters", "max_clusters"}:
        return _directional_relation(source, recommended, observed)
    if knob == "warehouse_size":
        s, r, o = (SIZE_ORDER.get(str(x).upper()) for x in (source, recommended, observed))
        if None in {s, r, o}:
            return rc.ImplementationConformance.UNRELATED, None
        return _directional_relation(s, r, o)
    if knob == "enable_photon":
        # No third ordinary boolean target exists. Mixed/unknown evidence must be
        # normalized upstream rather than guessed here.
        if isinstance(observed, bool):
            return rc.ImplementationConformance.CONTRARY, None
        return None, None
    if knob in {"warehouse_type", "warehouse_channel"}:
        # Semantic enums do not have a universal ordinal. A third state is a
        # modified alternate transition only when the observation explicitly
        # labels it as such; absent that evidence, classify as unrelated.
        return rc.ImplementationConformance.UNRELATED, None
    return rc.ImplementationConformance.UNRELATED, None


def choose_application(
    package: Mapping[str, Any], obs_config: Mapping[str, Any], *, scenario_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    primary: Optional[Dict[str, Any]] = None
    scenario = scenario_by_id(package, scenario_id) if scenario_id else recommended_scenario(package)
    sid = None if scenario is None else scenario.get("scenario_id")
    for step in candidate_steps(package, sid):
        conf, relation = compare_step(step, obs_config)
        item = {
            "scenario_id": sid,
            "step": int(step.get("step", 0)),
            "candidate_id": step.get("candidate_id"),
            "technique_id": step.get("technique_id"),
            "knob": step.get("knob"),
            "execution_state": step.get("execution_state"),
            "authority_eligible": authority_eligible(step),
            "source_value": step.get("current_value"),
            "recommended_value": step.get("recommended_value"),
            "observed_value": obs_config.get(step.get("knob")),
            "implementation_conformance": None if conf is None else conf.value,
            "modification_relation": relation,
            "invalidates": list(step.get("invalidates") or []),
            "dependencies": copy.deepcopy(step.get("dependencies") or []),
            "remeasure_days": ((step.get("validation") or {}).get("remeasure_days")),
        }
        results.append(item)
        if primary is None and item["authority_eligible"] and conf in {
            rc.ImplementationConformance.EXACT,
            rc.ImplementationConformance.MODIFIED,
            rc.ImplementationConformance.CONTRARY,
        }:
            primary = item
    return results, primary


def applied_before_package(package: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
    """Whether the observation's change was applied before the package was generated: the Publisher already saw that
    state, so the package cannot have caused it."""
    applied, generated = observation.get("applied_at_utc"), (package.get("provenance") or {}).get("generated_at")
    if not applied or not generated:
        return False
    return dt.datetime.fromisoformat(str(applied).replace("Z", "+00:00")) < dt.datetime.fromisoformat(str(generated).replace("Z", "+00:00"))


def match(package: Mapping[str, Any], observation: Mapping[str, Any], obs_config: Mapping[str, Any]) -> PlanMatch:
    """Match the observed configuration (*obs_config*, the normalized configuration of *observation*) to the plan of
    the selected scenario: the selected scenario, the step matches, the authority changes, then the scenario-scope
    decision. A change applied before the package was generated is never an application of it: every changed step is
    an independent change (reason ``APPLIED_BEFORE_PACKAGE``) and nothing carries authority."""
    selected_scenario_id = observation.get("applied_scenario_id") or (package.get("recommended_plan") or {}).get("scenario_id")
    selected_scenario = scenario_by_id(package, selected_scenario_id)
    step_results, primary = choose_application(package, obs_config, scenario_id=selected_scenario_id)
    if applied_before_package(package, observation):
        step_results = [{**item, "authority_eligible": False, "independent_reason": "APPLIED_BEFORE_PACKAGE"} for item in step_results]
        return PlanMatch(selected_scenario_id=selected_scenario_id, step_matches=tuple(step_results), primary=None,
                         authority_changes=(), scenario_scope=False)
    authority_changes = [
        item for item in step_results
        if item.get("authority_eligible") and item.get("implementation_conformance") in {
            rc.ImplementationConformance.EXACT.value,
            rc.ImplementationConformance.MODIFIED.value,
            rc.ImplementationConformance.CONTRARY.value,
        }
    ]
    scenario_scope = bool(
        len(authority_changes) > 1
        or (selected_scenario and selected_scenario.get("scenario_type") == "WAREHOUSE_TYPE_MIGRATION")
        or selected_scenario_id != (package.get("recommended_plan") or {}).get("scenario_id")
    )
    return PlanMatch(
        selected_scenario_id=selected_scenario_id,
        step_matches=tuple(step_results),
        primary=primary,
        authority_changes=tuple(authority_changes),
        scenario_scope=scenario_scope,
    )
