"""Estimator economics: the counterfactual baseline, the realized economics of an application and its measurement.

``make_cf_id`` (the counterfactual's identity over ``counterfactual_basis``, what the evidence states about it),
``expected_annual_for_exact_step`` (the publisher's expected savings for an exactly applied step) and
``calculate_economics`` (realized savings against the counterfactual, annualization when the evidence is
representative, the expected-savings denominator and the realization classification) come from the single-file
Realizer. ``counterfactual_record``, ``measurement_facts`` and ``correction_facts`` are its validation and
measurement statements that build the counterfactual, measurement and billing-correction payloads, in its order; the
engine records them.

The realization is measured once per active application episode: a candidate-scope application is the closest thing to
a step realization, a scenario-scope application to a plan realization, and the snapshot's latest measurement to a
package realization. Separate step, plan and package realization levels do not exist in the single-file Realizer and
are not added here.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.analysis import candidate_by_id, candidate_steps, config_hash, package_config, scenario_by_id
from realizer.common.utils import decimal, money, parse_utc, ratio_string
from realizer.config.settings import MODELER_GATEWAY_VERSION, WORKLOAD_SHIFT, volume_shifted
from realizer.contracts import realization as rc
from realizer.contracts.realization import CorrectionFacts, CounterfactualRecord, MeasurementFacts, make_measurement_id
from realizer.estimator.performance import NON_SAVINGS_ROLES, performance_economics
from realizer.governor.policy import RealizerPolicy, authority_eligible, readiness


RUN_RATE_STATES = frozenset({rc.RealizationState.REALIZED.value, rc.RealizationState.UNDER_REALIZED.value, rc.RealizationState.MONITORING.value})


def counterfactual_basis(observation: Mapping[str, Any]) -> Dict[str, Any]:
    """What the evidence states about the counterfactual, as the CF id hashes it and its payload records it: the window
    (its start falling back to the applied time and its end to the observed time, as the measurement id's do), the
    replay target, the measurement's assumptions (source and applied value, cost basis, restart cost; ``None`` without a
    measurement) and ``evidence_fingerprint``, the hash of the observation's economics and measurement (costs,
    components, replay inputs, windows). The observation's ``workload`` is informational and is not part of it."""
    measurement = observation.get("measurement")
    replay = (measurement or {}).get("replay") or {}
    assumptions = None if not measurement else {
        "source_value": measurement.get("source_value"), "applied_value": measurement.get("applied_value"),
        "cost_basis": measurement.get("cost_basis"), "restart_cost_cluster_minutes": replay.get("restart_cost_cluster_minutes"),
    }
    return {
        "window": [observation.get("window_start_utc") or observation.get("applied_at_utc"),
                   observation.get("window_end_utc") or observation.get("observed_at_utc")],
        "replay_target": replay.get("replayed_target_minutes"),
        "assumptions": assumptions,
        "evidence_fingerprint": rc.sha256_hex({"economics": observation.get("economics"), "measurement": measurement}),
    }


def make_cf_id(app_id: str, observation: Mapping[str, Any], package: Mapping[str, Any], policy: RealizerPolicy, method: str) -> str:
    """The CF id of the application's counterfactual: the application, the package's (old) configuration, the method the
    record states, the counterfactual basis (``counterfactual_basis``), the modeler version and the policy snapshot."""
    return rc.content_id("CF", {
        "app": app_id,
        "old_config_hash": config_hash(package_config(package)),
        "method": method,
        **counterfactual_basis(observation),
        "modeler_version": MODELER_GATEWAY_VERSION,
        "policy_snapshot_id": policy.snapshot_id,
    })


# How far above the published high a realization may read before it is flagged for review (the replay measures the
# full mechanical effect, the published high's track, so a little above it is noise; far above it is not).
REVIEW_ABOVE_HIGH = Decimal("1.25")
REOPTIMIZE_BELOW_LOW = Decimal("0.5")


def _savings_block(savings: Mapping[str, Any], component: Optional[str]) -> Optional[Mapping[str, Any]]:
    """The savings range of *component* (``total`` or ``dbu``; the total when not given). A missing component is
    ``None``: the DBU range never stands in for a total."""
    block = savings.get(component or "total")
    return block if isinstance(block, Mapping) else None


def expected_annual_for_exact_step(package: Mapping[str, Any], step: Mapping[str, Any], component: Optional[str] = None) -> Tuple[Optional[Decimal], str]:
    c = candidate_by_id(package, step.get("candidate_id"))
    if not c:
        return None, "UNAVAILABLE"
    econ = c.get("independent_economics") or {}
    block = _savings_block(econ.get("savings_365d") or {}, component)
    value = None if block is None else block.get("base")
    return decimal(value), "PUBLISHER_CANDIDATE" if value is not None else "UNAVAILABLE"


def published_range(block: Optional[Mapping[str, Any]]) -> Optional[Tuple[Decimal, Decimal, Decimal]]:
    """``(low, base, high)`` of a published savings range, or None when any is missing."""
    if not block:
        return None
    values = tuple(decimal(block.get(k)) for k in ("low", "base", "high"))
    return None if any(v is None for v in values) else values  # type: ignore[return-value]


def step_range(package: Mapping[str, Any], step: Mapping[str, Any], component: Optional[str]) -> Optional[Tuple[Decimal, Decimal, Decimal]]:
    c = candidate_by_id(package, step.get("candidate_id"))
    return None if not c else published_range(_savings_block((c.get("independent_economics") or {}).get("savings_365d") or {}, component))


def _components(savings: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """The expected annual savings per component (the base of each range; ``None`` when not priced), kept separate."""
    out: Dict[str, Optional[str]] = {}
    for component in ("dbu", "aws", "total"):
        block = savings.get(component)
        out[component] = money(block.get("base")) if isinstance(block, Mapping) else None
    return out


def _candidate_savings(package: Mapping[str, Any], step: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    c = candidate_by_id(package, (step or {}).get("candidate_id"))
    return ((c or {}).get("independent_economics") or {}).get("savings_365d") or {}


def _scenario_step(package: Mapping[str, Any], scenario_id: Any, number: Any) -> Optional[Mapping[str, Any]]:
    return next((st for st in candidate_steps(package, scenario_id) if int(st.get("step", 0)) == int(number if number is not None else -1)), None)


# The Publisher's annual net cluster-hours saved for every auto-stop value it evaluated (package evidence).
OPTION_GRID_METRIC = "auto_stop_annual_cluster_hours_by_value"


def _option_grid(package: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[int, Decimal]:
    """The candidate's auto-stop option grid: ``{value: annual net cluster-hours}`` (empty without one)."""
    evidence = package.get("evidence") or {}
    refs = [evidence.get(r) for r in candidate.get("evidence_refs") or []] or list(evidence.values())
    item = next((e for e in refs if isinstance(e, Mapping) and e.get("metric") == OPTION_GRID_METRIC), None)
    grid: Dict[int, Decimal] = {}
    for value, row in ((item or {}).get("value") or {}).items():
        hours = decimal((row or {}).get("net_cluster_hours")) if isinstance(row, Mapping) else None
        if hours is not None:
            grid[int(value)] = hours
    return grid


def _grid_hours(grid: Mapping[int, Decimal], value: int) -> Optional[Decimal]:
    """The net cluster-hours of *value*: its grid entry, else linear between the two grid values around it (None
    outside the grid)."""
    if value in grid:
        return grid[value]
    below = [v for v in grid if v < value]
    above = [v for v in grid if v > value]
    if not below or not above:
        return None
    lo, hi = max(below), min(above)
    return grid[lo] + (grid[hi] - grid[lo]) * Decimal(value - lo) / Decimal(hi - lo)


def option_grid_savings(package: Mapping[str, Any], step: Mapping[str, Any], applied: Any) -> Optional[Dict[str, Dict[str, Decimal]]]:
    """The expected savings of an auto-stop step applied at *applied* instead of its recommended value: the step's
    published range per component, scaled by the applied value's net cluster-hours over the recommended value's (the
    Publisher's own option grid on the package's evidence). None without a grid value for both, or a non-positive one."""
    candidate = candidate_by_id(package, step.get("candidate_id"))
    if (candidate is None or step.get("knob") != "auto_stop_minutes" or applied is None or int(applied) == 0
            or step.get("recommended_value") is None):
        return None
    grid = _option_grid(package, candidate)
    target, actual = _grid_hours(grid, int(step.get("recommended_value"))), _grid_hours(grid, int(applied))
    if target is None or actual is None or target <= 0:
        return None
    factor = actual / target
    savings = (candidate.get("independent_economics") or {}).get("savings_365d") or {}
    out: Dict[str, Dict[str, Decimal]] = {}
    for component, block in savings.items():
        rng = published_range(block) if isinstance(block, Mapping) else None
        if rng is not None:
            out[component] = {k: v * factor for k, v in zip(("low", "base", "high"), rng)}
    return out or None


def denominator(package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any], component: str) -> Dict[str, Any]:
    """The expected annual savings an application is measured against: ``savings`` (the per-component ranges the
    denominator comes from, ``{}`` when none), ``source`` and ``informational`` (a range reported, never classified
    against). See the module's tests for the rules: a candidate step its isolated economics; a scenario-scope prefix of
    the plan the running savings of its last applied step; one scenario step its candidate economics; a migration
    scenario its (pre-canary) range, informational."""
    conformance = app.get("implementation_conformance")
    if app.get("application_scope") == "SCENARIO":
        if conformance != rc.ImplementationConformance.EXACT.value:
            return {"savings": {}, "source": "UNAVAILABLE", "informational": False}
        scenario = scenario_by_id(package, app.get("scenario_id")) or {}
        if scenario.get("scenario_type") == "WAREHOUSE_TYPE_MIGRATION":
            savings = (scenario.get("portfolio_economics") or {}).get("savings_365d") or {}
            return {"savings": savings, "source": "PUBLISHER_SCENARIO", "informational": True}
        applied = sorted(int(ref["step"]) for ref in app.get("applied_step_refs") or [] if ref.get("step") is not None)
        eligible = [int(st.get("step", 0)) for st in candidate_steps(package, app.get("scenario_id")) if authority_eligible(st)]
        if len(applied) == 1:
            return {"savings": _candidate_savings(package, _scenario_step(package, app.get("scenario_id"), applied[0])),
                    "source": "PUBLISHER_CANDIDATE", "informational": False}
        if applied and applied == eligible[:len(applied)]:
            last = _scenario_step(package, app.get("scenario_id"), applied[-1]) or {}
            savings = (last.get("projected_economics") or {}).get("running_savings_365d") or {}
            return {"savings": savings, "source": "PUBLISHER_SCENARIO", "informational": False}
        return {"savings": {}, "source": "UNAVAILABLE", "informational": False}
    step = _scenario_step(package, app.get("scenario_id"), app.get("step"))
    if step is None or not authority_eligible(step):
        return {"savings": {}, "source": "UNAVAILABLE", "informational": False}
    if conformance == rc.ImplementationConformance.EXACT.value:
        return {"savings": _candidate_savings(package, step), "source": "PUBLISHER_CANDIDATE", "informational": False}
    if conformance == rc.ImplementationConformance.MODIFIED.value:
        # A modified auto-stop: the package's option grid prices the applied value on the package's own evidence.
        grid = option_grid_savings(package, step, app.get("observed_value"))
        if grid is not None:
            return {"savings": grid, "source": "REESTIMATED_APPLIED_SCOPE", "informational": False, "method": "PACKAGE_OPTION_GRID"}
    return {"savings": {}, "source": "UNAVAILABLE", "informational": False}


def calculate_economics(
    package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any],
    policy: RealizerPolicy, validation_passed: bool, counterfactual_claim_eligible: bool = True,
) -> Dict[str, Any]:
    if app.get("step_role") in NON_SAVINGS_ROLES and validation_passed:
        # A performance fix or a capacity guardrail is judged on queueing and p95; its cost is never a saving.
        return performance_economics(package, app, observation)
    econ = observation.get("economics") or {}
    cf = decimal(econ.get("counterfactual_old_cost"))
    actual = decimal(econ.get("observed_new_cost"))
    if cf is None or actual is None:
        return {
            "available": False,
            "currency": econ.get("currency"),
            "observed_economic_delta": None,
            "successful_realized_savings": None,
            "annualized_realized_savings": None,
            "expected_same_scope_annual_savings": None,
            "denominator_source": "UNAVAILABLE",
            "realization_ratio": None,
            "classification_detail": "ECONOMICS_UNAVAILABLE",
        }
    realized = cf - actual
    period_days = Decimal(int(econ["period_days"]))
    representative = readiness(observation, policy)["ready"]
    annualized = realized * Decimal("365") / period_days if representative and period_days > 0 else None

    expected: Optional[Decimal] = None
    expected_range: Optional[Tuple[Decimal, Decimal, Decimal]] = None
    expected_components: Optional[Dict[str, Optional[str]]] = None
    denom_source = "UNAVAILABLE"
    denom_method: Optional[str] = None
    informational = False
    # A live measurement compares like with like: DBU only when it has no AWS price, else the total. A caller-supplied
    # observation that gives a DBU component and no AWS component states DBU costs, so it is compared with the DBU
    # range; any other is compared with the total.
    measured = observation.get("measurement") or {}
    dbu_only = econ.get("dbu_realized") is not None and econ.get("aws_economic_realized") is None
    component = {"DBU_ONLY": "dbu", "DBU_AND_MODELED_AWS": "total"}.get(str(measured.get("cost_basis")), "dbu" if dbu_only else "total")
    supplied = decimal(econ.get("expected_annual_savings_actual_scope"))
    # A caller's re-estimate for the applied scope stands for a scenario-scope application and an applied step, never for
    # a contrary one.
    reestimable = app.get("application_scope") == "SCENARIO" or app.get("implementation_conformance") in (
        rc.ImplementationConformance.EXACT.value, rc.ImplementationConformance.MODIFIED.value)
    if supplied is not None and reestimable:
        expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
    elif supplied is None:
        chosen = denominator(package, app, observation, component)
        block = _savings_block(chosen["savings"], component)
        expected = None if block is None else decimal(block.get("base"))
        if expected is not None:
            denom_source, informational, denom_method = chosen["source"], chosen["informational"], chosen.get("method")
            expected_range = published_range(block)
            expected_components = _components(chosen["savings"])

    ratio: Optional[Decimal] = None
    if annualized is not None and expected is not None and expected > 0:
        ratio = annualized / expected

    if not validation_passed:
        state, detail = rc.RealizationState.VALIDATION_FAILED.value, "HARD_GUARDRAIL_FAILED"
        successful = Decimal("0")
    elif informational and counterfactual_claim_eligible:
        # A migration's range is pre-canary: the realization is reported against it, never classified.
        state, detail = rc.RealizationState.MONITORING.value, "MIGRATION_RANGE_INFORMATIONAL"
        successful = realized
    elif not counterfactual_claim_eligible:
        # Retain source actual/counterfactual diagnostics but do not convert an
        # indefensible/correlated-only counterfactual into successful realized
        # value. This is the fail-closed REAL-INV-009 path.
        state, detail = rc.RealizationState.BLOCKED.value, "COUNTERFACTUAL_NOT_CLAIM_ELIGIBLE"
        successful = Decimal("0")
    elif volume_shifted((observation.get("measurement") or {}).get("volume_ratio")):
        # The query volume moved outside the comparable band after the change: the counterfactual prices the actual
        # post-change days, so the saving is reported, but the package's expectation was priced at the old workload and
        # is not a fair yardstick. Never under-realized, never a re-optimization; the denominator stays the package's.
        state, detail = rc.RealizationState.MONITORING.value, WORKLOAD_SHIFT
        successful = realized
    elif ratio is None:
        state, detail = rc.RealizationState.MONITORING.value, "VALIDATED_VALUE_UNCLASSIFIED"
        successful = realized
    elif expected_range is not None and expected_range[0] > 0 and not informational:
        # Against the published range: at or above its low is realized (the replay measures the full mechanical effect,
        # so comparing it with the base would always read high); far above its high is flagged for review.
        low, _, high = expected_range
        assert annualized is not None
        successful = realized
        if annualized >= low:
            state = rc.RealizationState.REALIZED.value
            detail = "REALIZED_ABOVE_RANGE_REVIEW" if annualized > high * REVIEW_ABOVE_HIGH else "REALIZED"
        else:
            state = rc.RealizationState.UNDER_REALIZED.value
            detail = "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED" if annualized < low * REOPTIMIZE_BELOW_LOW else "UNDER_REALIZED"
    elif ratio >= policy.realized_ratio_min:
        state, detail = rc.RealizationState.REALIZED.value, "REALIZED"
        successful = realized
    else:
        state = rc.RealizationState.UNDER_REALIZED.value
        detail = "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED" if ratio < policy.under_realized_ratio_min else "UNDER_REALIZED"
        successful = realized

    if state not in RUN_RATE_STATES:
        # A failed or blocked measurement keeps its observed delta but publishes no run-rate and no ratio.
        annualized, ratio = None, None
    measured = observation.get("measurement")
    measured_detail = {} if not measured else {"measurement": {
        k: measured.get(k) for k in ("method", "knob", "source_value", "applied_value", "cost_basis", "replay", "cross_check",
                                     "post_window", "pre_window", "volume_ratio", "other_knob_changes", "holds_other_knobs")}}
    return {**measured_detail,
        "available": True,
        "currency": str(econ["currency"]),
        "economic_basis": str(econ.get("economic_basis") or "ECONOMIC_COST"),
        "counterfactual_old_cost": money(cf),
        "observed_new_cost": money(actual),
        "realized_savings_period": money(realized),
        "observed_economic_delta": money(realized),
        "successful_realized_savings": money(successful),
        "annualized_realized_savings": money(annualized),
        "expected_same_scope_annual_savings": money(expected),
        "denominator_source": denom_source,
        "denominator_method": denom_method,
        "expected_range": None if expected_range is None else {"component": component, **dict(zip(("low", "base", "high"), (money(v) for v in expected_range)))},
        "expected_components": expected_components,
        "classification_basis": "INFORMATIONAL" if informational or detail == WORKLOAD_SHIFT else "PUBLISHED_RANGE" if expected_range is not None and expected_range[0] > 0 else "RATIO_POLICY",
        "review_required": detail == "REALIZED_ABOVE_RANGE_REVIEW",
        "realization_ratio": ratio_string(ratio),
        "realization_state": state,
        "classification_detail": detail,
        "reoptimization_recommended": detail == "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED",
        "dbu_realized": money(econ.get("dbu_realized")),
        "aws_economic_realized": money(econ.get("aws_economic_realized")),
        "aws_cash_realized": money(econ.get("aws_cash_realized")),
        "aws_commitment_freed": money(econ.get("aws_commitment_freed")),
        "aws_evidence_type": econ.get("aws_evidence_type"),
        "period_days": rc.canonical_decimal(period_days),
    }


def counterfactual_record(
    application_episode_id: str, observation: Mapping[str, Any], package: Mapping[str, Any], policy: RealizerPolicy,
    claim_eligible: bool, method: str,
) -> CounterfactualRecord:
    """The counterfactual baseline of the measurement: its CF id, and the COUNTERFACTUAL_RECORDED payload when the
    observation carries the counterfactual (old-configuration) cost."""
    econ_obs = observation.get("economics") or {}
    if econ_obs.get("counterfactual_old_cost") is not None:
        cf_id = make_cf_id(application_episode_id, observation, package, policy, method)
        basis = counterfactual_basis(observation)
        cf_payload = {
            "counterfactual_id": cf_id,
            "method": method,
            "claim_eligible": claim_eligible,
            "workload_basis": "ACTUAL_POST_CHANGE_WORKLOAD",
            "counterfactual_old_cost": money(econ_obs.get("counterfactual_old_cost")),
            "modeler_version": MODELER_GATEWAY_VERSION,
            "window": dict(zip(("start_utc", "end_utc"), basis["window"])),
            "replay_target": basis["replay_target"],
            "assumptions": basis["assumptions"],
            "evidence_fingerprint": basis["evidence_fingerprint"],
        }
        return CounterfactualRecord(counterfactual_id=cf_id, method=method, claim_eligible=claim_eligible, payload=cf_payload)
    cf_id = make_cf_id(application_episode_id, observation, package, policy, method)
    return CounterfactualRecord(counterfactual_id=cf_id, method=method, claim_eligible=claim_eligible, payload=None)


def measurement_facts(
    application_episode_id: str, observation: Mapping[str, Any], counterfactual: CounterfactualRecord,
    economic: Mapping[str, Any], attribution: Mapping[str, Any], *, effective: str, observed_at: str,
) -> MeasurementFacts:
    """The measurement of available economics: the MEAS id and the measurement payload fields (window, counterfactual,
    economics, attribution, realization state, correction status)."""
    basis = str(economic.get("economic_basis") or "ECONOMIC_COST")
    meas_id = make_measurement_id(application_episode_id, observation, counterfactual.counterfactual_id, basis)
    return MeasurementFacts(
        measurement_id=meas_id,
        window_start_utc=observation.get("window_start_utc") or effective,
        window_end_utc=observation.get("window_end_utc") or observed_at,
        counterfactual_id=counterfactual.counterfactual_id,
        counterfactual_method=counterfactual.method,
        economics=economic,
        attribution=attribution,
        realization_state=economic.get("realization_state"),
        correction_status=(observation.get("correction") or {}).get("type", "ORIGINAL"),
    )


# The reason a correction records when the observation carries no correction object: the window was measured before and
# the economics collected for it now differ.
REMEASURED_REASON = "WINDOW_REMEASURED_WITH_DIFFERENT_ECONOMICS"

# What a measurement states about its window besides its identity: equal in all of them, a measurement repeats one.
MEASURED_FACTS = ("economics", "attribution", "realization_state")


def same_window(entry: rc.RealizationLedgerEntry, application_episode_id: str, measurement: MeasurementFacts) -> bool:
    """Whether the measurement *entry* measured the same window as *measurement*: the same application episode, window
    start and window end (compared as times). The measurement id is not the window: it hashes the evidence, so a window
    restated with other costs has a new one."""
    payload = entry.payload
    try:
        return (entry.application_episode_id == application_episode_id
                and parse_utc(payload.get("window_start_utc")) == parse_utc(measurement.window_start_utc)
                and parse_utc(payload.get("window_end_utc")) == parse_utc(measurement.window_end_utc))
    except (TypeError, ValueError):
        return False


def earlier_measurements(measurement: MeasurementFacts, history: Sequence[rc.RealizationLedgerEntry]) -> Tuple[rc.RealizationLedgerEntry, ...]:
    """The measurements of the ledger *history* (in ledger order) recorded before *measurement* first was: all of them when
    it is new. Reprocessing an observation thus compares with what its first run compared with."""
    earlier = []
    for entry in history:
        if entry.event_type != rc.EventType.ECONOMIC_MEASUREMENT_RECORDED:
            continue
        if entry.measurement_id == measurement.measurement_id:
            break
        earlier.append(entry)
    return tuple(earlier)


def earlier_window_measurement(
    measurement: MeasurementFacts, application_episode_id: str, history: Sequence[rc.RealizationLedgerEntry],
) -> Optional[rc.RealizationLedgerEntry]:
    """The latest earlier measurement (``earlier_measurements``) of the same window (``same_window``); ``None`` when the
    window was not measured before."""
    return next((e for e in reversed(earlier_measurements(measurement, history)) if same_window(e, application_episode_id, measurement)), None)


def _same(entry: rc.RealizationLedgerEntry, measurement: MeasurementFacts, fields: Sequence[str]) -> bool:
    return all(rc.canonical_json(entry.payload.get(k)) == rc.canonical_json(getattr(measurement, k)) for k in fields)


def repeats_earlier_measurement(
    measurement: MeasurementFacts, application_episode_id: str, history: Sequence[rc.RealizationLedgerEntry],
) -> bool:
    """Whether *measurement* says nothing new: the window was measured before with the same economics, attribution and
    realization state (``MEASURED_FACTS``). Such a measurement is a duplicate and is not recorded, whatever its id (a correction object's version alone changes the id)."""
    prior = earlier_window_measurement(measurement, application_episode_id, history)
    return prior is not None and _same(prior, measurement, MEASURED_FACTS)


def correction_facts(
    observation: Mapping[str, Any], measurement: MeasurementFacts, application_episode_id: str,
    history: Sequence[rc.RealizationLedgerEntry],
) -> Optional[CorrectionFacts]:
    """The billing correction of *measurement* (``None`` when there is none to record): the BILLING_CORRECTION_RECORDED
    payload and the ledger entry it corrects.

    The comparison with the earlier measurement is made here, against the ledger *history* the run started from, because
    the collector cannot see the ledger. When the same window was measured earlier (``earlier_window_measurement``), the
    latest such measurement decides: the same economics record no correction; other economics are a restatement of that
    window, and the correction names its measurement, whether or not the observation carries a correction object. When
    the window was not measured earlier, only an observation's correction object is a correction, and it corrects the
    entry the observation names, else the latest earlier measurement. A correction object keeps its key, type, version
    and reason, and a key and version the history already holds is not recorded again; without one, the key is the new
    measurement id, the type ``RESTATEMENT`` and the reason ``REMEASURED_REASON``.
    """
    corr = observation.get("correction") or {}
    prior_window = earlier_window_measurement(measurement, application_episode_id, history)
    if prior_window is not None:
        if _same(prior_window, measurement, ("economics",)):
            return None
        corrects_entry_id = prior_window.ledger_entry_id
    elif corr:
        earlier = earlier_measurements(measurement, history)
        corrects_entry_id = corr.get("corrects_entry_id") or (earlier[-1].ledger_entry_id if earlier else None)
    else:
        return None
    if corr and any(e.event_type == rc.EventType.BILLING_CORRECTION_RECORDED
                    and (e.payload.get("correction_key"), e.payload.get("version")) == (corr.get("key"), corr.get("version")) for e in history):
        return None
    correction_payload = {
        "correction_key": corr.get("key") or measurement.measurement_id,
        "type": str(corr.get("type") or "RESTATEMENT").upper(),
        "version": corr.get("version"),
        "reason": corr.get("reason") if corr else REMEASURED_REASON,
        "measurement_id": measurement.measurement_id,
        "corrects_entry_id": corrects_entry_id,
    }
    return CorrectionFacts(payload=correction_payload, corrects_entry_id=corrects_entry_id)
