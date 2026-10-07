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
from typing import Any, Dict, Mapping, Optional, Tuple

from realizer.common.errors import InputError
from realizer.common.utils import decimal
from realizer.config.settings import NON_AUTHORITATIVE_EXECUTION_STATES, POLICY_VERSION
from realizer.contracts.observation import CLAIM_ELIGIBLE_COUNTERFACTUAL_METHODS
from realizer.contracts import realization as rc


@dataclass(frozen=True)
class RealizerPolicy:
    minimum_days: int = 7
    minimum_queries: int = 100
    require_representative_regime: bool = True
    max_normalized_p95_runtime_regression_pct: Decimal = Decimal("5")
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


def readiness(observation: Mapping[str, Any], policy: RealizerPolicy) -> Dict[str, Any]:
    """Whether the evidence is sufficient to validate: elapsed days, query sample, representative regime and evidence
    quality. An input the observation could not establish (``null``) fails its check."""
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
    return {"ready": all(x["pass"] for x in checks.values()), "checks": checks}


def validate_performance(observation: Mapping[str, Any], policy: RealizerPolicy) -> Dict[str, Any]:
    perf = observation.get("performance") or {}
    p95 = decimal(perf.get("normalized_p95_runtime_regression_pct"))
    reliability = perf.get("reliability_pass")
    correctness = perf.get("correctness_pass", True)
    p95_pass = p95 is not None and p95 <= policy.max_normalized_p95_runtime_regression_pct
    reliability_pass = reliability is True
    correctness_pass = correctness is not False
    passed = p95_pass and reliability_pass and correctness_pass
    return {
        "state": "PASSED" if passed else "FAILED",
        "hard_guardrails_pass": passed,
        "rules": [
            {
                "rule_id": "REAL-PERF-P95-001",
                "metric": "normalized_p95_runtime_regression_pct",
                "observed": None if p95 is None else rc.canonical_decimal(p95),
                "operator": "LTE",
                "threshold": rc.canonical_decimal(policy.max_normalized_p95_runtime_regression_pct),
                "result": "PASS" if p95_pass else "FAIL",
                "policy_snapshot_id": policy.snapshot_id,
            },
            {
                "rule_id": "REAL-REL-001",
                "metric": "reliability_pass",
                "observed": reliability,
                "operator": "EQ",
                "threshold": True,
                "result": "PASS" if reliability_pass else "FAIL",
                "policy_snapshot_id": policy.snapshot_id,
            },
            {
                "rule_id": "REAL-CORRECT-001",
                "metric": "correctness_pass",
                "observed": correctness,
                "operator": "NE",
                "threshold": False,
                "result": "PASS" if correctness_pass else "FAIL",
                "policy_snapshot_id": policy.snapshot_id,
            },
        ],
    }


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
