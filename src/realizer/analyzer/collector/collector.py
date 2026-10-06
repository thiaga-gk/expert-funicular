"""Collector: where an observation comes from.

The Realizer has no live collection. A run takes a normalized post-change observation, and this module supplies the two
ways the engine and its CLI obtain one: replay of a normalized observation file, and the deterministic demo observation,
whose synthetic economics are sized to the applied step's published savings. Nothing here queries Databricks.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from realizer.analyzer.analysis import candidate_steps, package_config
from realizer.common.utils import load_json, money, utc_iso
from realizer.estimator.economics import expected_annual_for_exact_step
from realizer.governor.policy import authority_eligible


def load_observation(path: str | Path) -> Any:
    """The normalized observation JSON at *path*, parsed (replay)."""
    return load_json(path)


DEMO_COUNTERFACTUAL_COST = Decimal("10000")
DEMO_PERIOD_DAYS = 30
DEMO_FALLBACK_PERIOD_SAVINGS = Decimal("1000")  # when the applied step has no published estimate


def _demo_period_savings(package: Mapping[str, Any], step: Optional[Mapping[str, Any]]) -> Decimal:
    """The demo's realized savings over its 30-day period: the applied step's published base savings pro rata, so a
    demo realizes about what the package expected (ratio near 1); a fixed amount when the step has no estimate."""
    expected, _ = expected_annual_for_exact_step(package, step) if step else (None, "UNAVAILABLE")
    if expected is None or expected <= 0:
        return DEMO_FALLBACK_PERIOD_SAVINGS
    return (expected * DEMO_PERIOD_DAYS / Decimal("365")).quantize(Decimal("0.01"))


def demo_observation(package: Mapping[str, Any], *, modified: bool = False) -> Dict[str, Any]:
    """A deterministic synthetic post-change observation: the first authority-eligible step of the recommended plan
    applied (``modified``: a size cut to MEDIUM applied as SMALL), eight days after the package was generated, with
    synthetic economics sized to the step's published savings. Never a measurement of a real warehouse."""
    cfg = package_config(package)
    step = next((s for s in candidate_steps(package) if authority_eligible(s)), None)
    if step:
        if modified and step.get("knob") == "warehouse_size" and str(step.get("recommended_value")) == "MEDIUM":
            cfg["warehouse_size"] = "SMALL"
        else:
            cfg[str(step.get("knob"))] = step.get("recommended_value")
    savings = _demo_period_savings(package, step)
    reestimate = money(savings * Decimal("365") / DEMO_PERIOD_DAYS) if modified else None
    generated = (package.get("provenance") or {}).get("generated_at") or "2026-10-01T00:00:00Z"
    d = dt.datetime.fromisoformat(generated.replace("Z", "+00:00")) + dt.timedelta(days=8)
    return {
        "observed_at_utc": utc_iso(d),
        "recorded_at_utc": utc_iso(d),
        "applied_at_utc": utc_iso(d - dt.timedelta(days=8)),
        "window_start_utc": utc_iso(d - dt.timedelta(days=7)),
        "window_end_utc": utc_iso(d),
        "config": cfg,
        "elapsed_days": 8,
        "query_count": 250,
        "representative_regime": True,
        "evidence_quality": "SUFFICIENT",
        "performance": {"normalized_p95_runtime_regression_pct": "2", "reliability_pass": True, "correctness_pass": True},
        "economics": {
            "currency": "USD",
            "counterfactual_old_cost": money(DEMO_COUNTERFACTUAL_COST),
            "observed_new_cost": money(DEMO_COUNTERFACTUAL_COST - savings),
            "period_days": DEMO_PERIOD_DAYS,
            "expected_annual_savings_actual_scope": reestimate,
            "counterfactual_method": "MATCHED_HISTORICAL",
            "economic_basis": "ECONOMIC_COST",
        },
        "origin_attribution": "CONFIRMED",
        "intent_state": "ACCEPTED",
        "actor": "demo-user",
        "source": "DEMO_NORMALIZED_OBSERVATION",
    }
