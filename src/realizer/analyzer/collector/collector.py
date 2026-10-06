"""Collector: where an observation comes from.

The Realizer has no live collection. A run takes a normalized post-change observation, and this module supplies the two
ways the engine and its CLI obtain one: replay of a normalized observation file, and the deterministic demo observation.
Both are moved unchanged from the single-file Realizer. Nothing here queries Databricks.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, Mapping

from realizer.analyzer.analysis import candidate_steps, package_config
from realizer.common.utils import load_json, utc_iso
from realizer.governor.policy import authority_eligible


def load_observation(path: str | Path) -> Any:
    """The normalized observation JSON at *path*, parsed (replay)."""
    return load_json(path)


def demo_observation(package: Mapping[str, Any], *, modified: bool = False) -> Dict[str, Any]:
    cfg = package_config(package)
    step = next((s for s in candidate_steps(package) if authority_eligible(s)), None)
    if step:
        if modified and step.get("knob") == "warehouse_size" and str(step.get("recommended_value")) == "MEDIUM":
            cfg["warehouse_size"] = "SMALL"
        else:
            cfg[str(step.get("knob"))] = step.get("recommended_value")
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
            "counterfactual_old_cost": "10000",
            "observed_new_cost": "9000",
            "period_days": 30,
            "expected_annual_savings_actual_scope": "12000" if modified else None,
            "counterfactual_method": "MATCHED_HISTORICAL",
            "economic_basis": "ECONOMIC_COST",
        },
        "origin_attribution": "CONFIRMED",
        "intent_state": "ACCEPTED",
        "actor": "demo-user",
        "source": "DEMO_NORMALIZED_OBSERVATION",
    }
