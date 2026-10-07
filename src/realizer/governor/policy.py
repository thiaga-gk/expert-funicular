"""Realizer policy: the thresholds in force and the checks that apply them to one observation.

The role of the publisher's ``Thresholds``: ``RealizerPolicy`` holds every policy number (its field values are the
single-file Realizer's defaults), and the checks below decide what the evidence permits: whether it is sufficient
(``readiness``), whether the hard performance guardrails hold (``validate_performance``), whether a counterfactual
baseline can support a claim (``counterfactual_eligible``), whether the financial-quality gates pass
(``economic_quality_result``), whether a plan step may carry realization authority (``authority_eligible``) and whether
a new savings estimate is materially better (``savings_improvement_material``). Each check is pure. They are moved
unchanged from the single-file Realizer, with every threshold kept inline where it is applied.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from realizer.common.errors import InputError
from realizer.common.utils import decimal
from realizer.config.settings import (CANARY_GUARD_WINDOW_DAYS, NON_AUTHORITATIVE_EXECUTION_STATES, POLICY_VERSION, WORKLOAD_SHIFT,
                                      guard_thresholds, volume_shifted)
from realizer.contracts.observation import CLAIM_ELIGIBLE_COUNTERFACTUAL_METHODS
from realizer.contracts import realization as rc


@dataclass(frozen=True)
class RealizerPolicy:
    minimum_days: int = 7
    minimum_queries: int = 100
    require_representative_regime: bool = True
    # None: the package's cost-profile limit (canary_p95_regression_pct) governs; a value can only tighten it.
    max_normalized_p95_runtime_regression_pct: Optional[Decimal] = None
    realized_ratio_min: Decimal = Decimal("0.80")
    under_realized_ratio_min: Decimal = Decimal("0.50")
    remeasure_days_after_invalidating_change: int = 7
    minimum_attributable_spend_pct: Decimal = Decimal("95")
    minimum_price_coverage_pct: Decimal = Decimal("100")
    required_currency_consistency_pct: Decimal = Decimal("100")
    minimum_material_savings_delta_pct: Decimal = Decimal("5")
    require_confirmed_origin_for_hard_kpi: bool = True
    policy_version: str = POLICY_VERSION

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "RealizerPolicy":
        """The policy with *value*'s overrides (``None`` or empty: the defaults). Raises ``InputError`` for a value that
        is not a mapping, an unknown key, a value of the wrong type, a negative number, a percentage above 100, or ratio
        thresholds out of order (0 <= under-realized minimum <= realized minimum)."""
        if not value:
            return cls()
        if not isinstance(value, Mapping):
            raise InputError("a policy file must be a JSON object")
        fields = cls.__dataclass_fields__
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise InputError(f"unknown policy setting(s): {', '.join(map(str, unknown))}")
        kwargs: Dict[str, Any] = {}
        for name, v in value.items():
            if name in _DECIMAL_SETTINGS:
                if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                    raise InputError(f"policy setting {name} must be a number")
                try:
                    v = Decimal(str(v))
                except ArithmeticError:
                    raise InputError(f"policy setting {name} must be a number") from None
                if not v.is_finite() or v < 0 or (name in _PERCENT_SETTINGS and v > 100):
                    raise InputError(f"policy setting {name} is out of range")
            elif name in _INT_SETTINGS:
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise InputError(f"policy setting {name} must be a non-negative integer")
            elif name in _BOOL_SETTINGS:
                if not isinstance(v, bool):
                    raise InputError(f"policy setting {name} must be true or false")
            elif not isinstance(v, str) or not v:
                raise InputError(f"policy setting {name} must be a non-empty string")
            kwargs[name] = v
        policy = cls(**kwargs)
        if not policy.under_realized_ratio_min <= policy.realized_ratio_min:
            raise InputError("policy under_realized_ratio_min must not exceed realized_ratio_min")
        return policy

    @property
    def snapshot_id(self) -> str:
        return rc.content_id("PSNAP", {"contract": "1.0.0", "policy": policy_to_dict(self)})


_DECIMAL_SETTINGS = frozenset({
    "max_normalized_p95_runtime_regression_pct", "realized_ratio_min", "under_realized_ratio_min",
    "minimum_attributable_spend_pct", "minimum_price_coverage_pct", "required_currency_consistency_pct",
    "minimum_material_savings_delta_pct",
})
_PERCENT_SETTINGS = frozenset({"minimum_attributable_spend_pct", "minimum_price_coverage_pct", "required_currency_consistency_pct"})
_INT_SETTINGS = frozenset({"minimum_days", "minimum_queries", "remeasure_days_after_invalidating_change"})
_BOOL_SETTINGS = frozenset({"require_representative_regime", "require_confirmed_origin_for_hard_kpi"})


def policy_to_dict(policy: RealizerPolicy) -> Dict[str, Any]:
    out = asdict(policy)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            out[k] = rc.canonical_decimal(v)
    return out


def package_currency(package: Mapping[str, Any]) -> Optional[str]:
    """The currency the package's economics are stated in (its package economics block)."""
    return (package.get("package_economics") or {}).get("currency")


def authority_eligible(step: Mapping[str, Any]) -> bool:
    return str(step.get("execution_state")) not in NON_AUTHORITATIVE_EXECUTION_STATES


def readiness(observation: Mapping[str, Any], policy: RealizerPolicy, performance: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Whether the evidence is sufficient to validate: elapsed days, query sample, representative regime, evidence
    quality and, given the performance verdict, guardrail evidence (no rule UNKNOWN). An input the observation could
    not establish (``null``) fails its check. A regime that is not representative because the measured query volume
    moved outside the comparable band states the volume ratio and the reason ``WORKLOAD_SHIFT``."""
    elapsed = int(observation["elapsed_days"])
    queries = observation["query_count"]
    representative = observation["representative_regime"]
    quality = str(observation["evidence_quality"]).upper()
    checks = {
        "minimum_days": {"observed": elapsed, "threshold": policy.minimum_days, "pass": elapsed >= policy.minimum_days},
        "minimum_queries": {"observed": queries, "threshold": policy.minimum_queries, "pass": queries is not None and queries >= policy.minimum_queries},
        "representative_regime": {"observed": representative, "required": policy.require_representative_regime, "pass": representative is True or not policy.require_representative_regime},
        "evidence_quality": {"observed": quality, "required": "SUFFICIENT", "pass": quality == "SUFFICIENT"},
    }
    volume = (observation.get("measurement") or {}).get("volume_ratio")
    if representative is not True and volume_shifted(volume):
        checks["representative_regime"].update({"volume_ratio": volume, "reason": WORKLOAD_SHIFT})
    if performance is not None:
        unknown = [r["rule_id"] for r in performance["rules"] if r["result"] == "UNKNOWN"]
        checks["guardrail_evidence"] = {"unknown_rules": unknown, "pass": not unknown}
    return {"ready": all(x["pass"] for x in checks.values()), "checks": checks}


def _rule(rule_id: str, metric: str, observed: Any, operator: str, threshold: Any, result: str, policy: RealizerPolicy, **extra: Any) -> Dict[str, Any]:
    canon = lambda v: None if v is None or isinstance(v, bool) else rc.canonical_decimal(Decimal(str(v)))
    return {"rule_id": rule_id, "metric": metric, "observed": observed if isinstance(observed, bool) or observed is None else canon(observed),
            "operator": operator, "threshold": threshold if isinstance(threshold, bool) else canon(threshold), "result": result,
            **{k: canon(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v for k, v in extra.items()},
            "policy_snapshot_id": policy.snapshot_id}


def p95_limit_pct(policy: RealizerPolicy, limits: Mapping[str, float]) -> Decimal:
    """The p95 regression limit: the package's cost-profile value, tightened by the Realizer policy when it sets one."""
    limit = Decimal(str(limits["canary_p95_regression_pct"]))
    return limit if policy.max_normalized_p95_runtime_regression_pct is None else min(limit, policy.max_normalized_p95_runtime_regression_pct)


def _two_part(rule_id: str, metric: str, pre: Mapping[str, Any], post: Mapping[str, Any], safe: float, margin_pct: Decimal,
              count_field: str, min_count: float, policy: RealizerPolicy, *, unknown_below_sample: bool) -> Dict[str, Any]:
    """The Publisher canary guard's two-part rule: FAIL when the value after the change is above its safe level AND more
    than ``margin_pct`` above the value before, with the minimum sample. Without the values the rule is UNKNOWN; below the
    sample it is UNKNOWN for a rate over all queries (``unknown_below_sample``) and PASS for a count of affected queries
    (too few affected queries to matter)."""
    now, before, count = decimal(post.get(metric)), decimal(pre.get(metric)), decimal(post.get(count_field))
    margin = 1 + margin_pct / 100
    if now is None or before is None or count is None:
        result = "UNKNOWN"
    elif count < Decimal(str(min_count)):
        result = "UNKNOWN" if unknown_below_sample else "PASS"
    else:
        result = "FAIL" if now > Decimal(str(safe)) and now > before * margin else "PASS"
    return _rule(rule_id, metric, now, "NOT(GT_SAFE_AND_GT_PREVIOUS_X_MARGIN)", safe, result, policy,
                 previous=before, margin_pct=margin_pct, sample=count, min_sample=min_count)


def validate_performance(observation: Mapping[str, Any], policy: RealizerPolicy, package: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The hard guardrails of the observation, per rule PASS, FAIL or UNKNOWN; state FAILED when any rule fails, UNKNOWN
    when none fails but one lacks evidence (not ready yet, never failed), PASSED otherwise.

    The limits are the package's cost-profile values with the Publisher's defaults (``guard_thresholds``); the Realizer
    policy can only tighten the p95 limit. Measured guard metrics (``performance.guards``) are judged like the
    Publisher's canary guard: the failure rate always; for an auto-stop change the share of queries that gained a cold
    start against the profile's cold-start budget (Serverless: none); for any other knob the queued share, the spill share
    (size) and the p95 execution time on the statement sources seen before and after. A caller-supplied observation
    (no guard metrics) states a p95 regression percentage, reliability and correctness instead."""
    limits = guard_thresholds(((package or {}).get("cost_profile") or {}).get("applied_thresholds"))
    perf = observation.get("performance") or {}
    guards = perf.get("guards")
    p95_limit = p95_limit_pct(policy, limits)
    rules: List[Dict[str, Any]] = []
    if guards:
        pre, post = guards.get("pre") or {}, guards.get("post") or {}
        knob = (observation.get("measurement") or {}).get("knob")
        margin = Decimal(str(limits["canary_p95_regression_pct"]))
        rules.append(_two_part("REAL-FAIL-001", "failure_cancel_pct", pre, post, limits["failure_cancel_pct_downsize_safe"], margin,
                               "queries", limits["min_query_sample_size"], policy, unknown_below_sample=True))
        if knob == "auto_stop_minutes":
            cold = guards.get("cold_start") or {}
            budget = None if str((observation.get("config") or {}).get("warehouse_type")).upper() == "SERVERLESS" else limits["cold_start_added_share_max_pct"]
            values = [cold.get(k) for k in ("pre_restarts", "pre_queries", "post_restarts", "post_queries")]
            if budget is None:
                rules.append(_rule("REAL-COLD-001", "added_cold_start_share_pct", None, "LTE", None, "PASS", policy, reason="NO_COLD_START_BUDGET"))
            elif any(v is None for v in values) or not values[1] or values[3] < limits["min_query_sample_size"]:
                rules.append(_rule("REAL-COLD-001", "added_cold_start_share_pct", None, "LTE", budget, "UNKNOWN", policy))
            else:
                added = (Decimal(values[2]) / Decimal(values[3]) - Decimal(values[0]) / Decimal(values[1])) * 100
                rules.append(_rule("REAL-COLD-001", "added_cold_start_share_pct", added, "LTE", budget,
                                   "PASS" if added <= Decimal(str(budget)) else "FAIL", policy))
        else:
            rules.append(_two_part("REAL-QUEUE-001", "queued_query_pct", pre, post, limits["queue_query_pct_safe"], margin,
                                   "queued_queries", limits["severity_min_affected_queries"], policy, unknown_below_sample=False))
            if knob == "warehouse_size":
                rules.append(_two_part("REAL-SPILL-001", "spill_query_pct", pre, post, limits["spill_query_pct_downsize_safe"], margin,
                                       "spill_queries", limits["severity_min_affected_queries"], policy, unknown_below_sample=False))
            now, before, shared = (decimal(post.get("p95_execution_ms_shared_sources")), decimal(pre.get("p95_execution_ms_shared_sources")),
                                   decimal(post.get("shared_source_queries")))
            if now is None or not before or shared is None or shared < Decimal(str(limits["canary_p95_min_queries"])):
                result = "UNKNOWN"
            else:
                result = "FAIL" if now > before * (1 + p95_limit / 100) else "PASS"
            rules.append(_rule("REAL-P95-001", "p95_execution_ms_shared_sources", now, "LTE_PREVIOUS_X_MARGIN", p95_limit, result, policy,
                               previous=before, sample=shared, min_sample=limits["canary_p95_min_queries"]))
    else:
        p95 = decimal(perf.get("normalized_p95_runtime_regression_pct"))
        reliability = perf.get("reliability_pass")
        correctness = perf.get("correctness_pass", True)
        rules.append(_rule("REAL-PERF-P95-001", "normalized_p95_runtime_regression_pct", p95, "LTE", p95_limit,
                           "UNKNOWN" if p95 is None else "PASS" if p95 <= p95_limit else "FAIL", policy))
        rules.append(_rule("REAL-REL-001", "reliability_pass", reliability, "EQ", True,
                           "UNKNOWN" if reliability is None else "PASS" if reliability is True else "FAIL", policy))
        rules.append(_rule("REAL-CORRECT-001", "correctness_pass", correctness, "NE", False, "FAIL" if correctness is False else "PASS", policy))
    results = {r["result"] for r in rules}
    state = "FAILED" if "FAIL" in results else "UNKNOWN" if "UNKNOWN" in results else "PASSED"
    return {"state": state, "hard_guardrails_pass": state == "PASSED", "rules": rules}


def canary_window_open(app: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
    """Whether the applied step is a canary-type change still inside the Publisher's guard window: its realization is
    provisional until the window ends."""
    return str(app.get("execution_state")) == "READY_FOR_CANARY" and int(observation.get("elapsed_days") or 0) < CANARY_GUARD_WINDOW_DAYS


def counterfactual_eligible(observation: Mapping[str, Any]) -> Tuple[bool, str]:
    """``(claim eligible, method)``: only the policy's claim-eligible methods with a counterfactual cost support a claim."""
    econ = observation.get("economics") or {}
    method = str(econ.get("counterfactual_method") or "NO_DEFENSIBLE_COUNTERFACTUAL")
    if econ.get("counterfactual_old_cost") is None:
        return False, "NO_DEFENSIBLE_COUNTERFACTUAL"
    return method in CLAIM_ELIGIBLE_COUNTERFACTUAL_METHODS, method


def economic_quality_result(observation: Mapping[str, Any], policy: RealizerPolicy, package_currency: Optional[str]) -> Dict[str, Any]:
    """The financial-quality gates of the observation's evidence. A gate whose input is ``null`` does not pass; the
    currency gate also requires the economics to be in the package's currency."""
    fq = observation["financial_quality"]
    attributable = decimal(fq["attributable_spend_pct"])
    price_coverage = decimal(fq["price_coverage_pct"])
    currency_pct = decimal(fq["currency_consistency_pct"])
    correction_complete = fq["correction_chain_complete"] is True
    material_cost_available = fq["material_cost_component_available"] is True
    currency = (observation.get("economics") or {}).get("currency")
    gates = {
        "attributable_spend": {
            "observed_pct": None if attributable is None else rc.canonical_decimal(attributable),
            "required_min_pct": rc.canonical_decimal(policy.minimum_attributable_spend_pct),
            "pass": attributable is not None and attributable >= policy.minimum_attributable_spend_pct,
        },
        "price_coverage": {
            "observed_pct": None if price_coverage is None else rc.canonical_decimal(price_coverage),
            "required_min_pct": rc.canonical_decimal(policy.minimum_price_coverage_pct),
            "pass": price_coverage is not None and price_coverage >= policy.minimum_price_coverage_pct,
        },
        "currency_consistency": {
            "observed_pct": None if currency_pct is None else rc.canonical_decimal(currency_pct),
            "required_pct": rc.canonical_decimal(policy.required_currency_consistency_pct),
            "currency": currency,
            "package_currency": package_currency,
            "pass": currency_pct is not None and currency_pct >= policy.required_currency_consistency_pct
            and currency is not None and currency == package_currency,
        },
        "correction_chain": {"complete": correction_complete, "pass": correction_complete},
        "material_cost_components": {"available": material_cost_available, "pass": material_cost_available},
    }
    gates["pass"] = all(v.get("pass") for k, v in gates.items() if k != "pass")
    return gates


def savings_improvement_material(old_expected: Any, new_expected: Any, policy: RealizerPolicy) -> bool:
    old, new = decimal(old_expected), decimal(new_expected)
    if old is None or new is None or old <= 0:
        return False
    pct = (new - old) * Decimal("100") / old
    return pct >= policy.minimum_material_savings_delta_pct
