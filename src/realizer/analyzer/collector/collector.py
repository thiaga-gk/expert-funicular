"""Collector: where an observation comes from.

Three sources. A live measurement (``collect_measurement``) reads system tables through the bridge to the Publisher's
collector (``realizer.analyzer.collector.bridge``) in two passes, the warehouse's history, then the windows the plan
asks for, and returns measurement evidence (``realizer.contracts.measurement``), which the projector turns into an
observation; the same function runs on a simulated world for the demo and the end-to-end goldens. A replayed
observation file is the normalized observation itself. The deterministic demo observation has synthetic economics
sized to the applied step's published savings and is never a measurement.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from realizer.analyzer.analysis import candidate_steps, package_config
from realizer.analyzer.collector import bridge
from realizer.analyzer.measurement import epoch_config, plan_measurement
from realizer.analyzer.projector import project_observation
from realizer.common.errors import InputError
from realizer.common.utils import money, utc_iso, write_json
from realizer.config.loader import read_json_input, validate_measurement_evidence, validate_package
from realizer.config.settings import DEFAULT_AWS_PRICE_REGISTRY
from realizer.estimator.economics import expected_annual_for_exact_step
from realizer.contracts.measurement import MEASUREMENT_EVIDENCE_FORMAT_VERSION
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.governor.policy import authority_eligible, package_currency


def package_region(package: Mapping[str, Any]) -> Optional[str]:
    """The AWS region the package's economics were modeled for (its single source region), else ``None``."""
    regions = sorted({str(r) for r in (package.get("provenance") or {}).get("source_regions") or [] if r})
    return regions[0] if len(regions) == 1 else None


def collect_measurement(client: Any, package: Mapping[str, Any], *, as_of: str, source: str, source_region: Optional[str] = None,
                        aws_registry: Optional[Mapping[str, Any]] = None, ingestion_cutoff: Optional[str] = None) -> Dict[str, Any]:
    """Measurement evidence of *package*'s warehouse at *as_of*, read through *client* (live system tables, or a
    simulated world): pass 1 reads the history, the plan picks the windows, pass 2 reads them. Validated against the
    measurement evidence schema before it is returned."""
    warehouse = package["warehouse"]
    ws, wh = str(warehouse["workspace_id"]), str(warehouse["warehouse_id"])
    as_of = utc_iso(as_of)
    region = source_region or package_region(package)
    assumptions = bridge.assumptions((package.get("cost_profile") or {}).get("applied_thresholds"))
    history = bridge.collect_history(client, ws, wh, as_of=as_of, settle_hours=assumptions["billing_settle_hours"], ingestion_cutoff=ingestion_cutoff)
    if not history["epochs"]:
        raise InputError(f"warehouse {ws}/{wh} has no configuration epoch that started before {as_of}")
    plan = plan_measurement(package, history["epochs"], history["billing"]["settled_through"])
    windows, guard = bridge.collect_windows(client, ws, wh, as_of=as_of, windows=plan.windows())
    observed = epoch_config(plan.observed_epoch)
    previous = epoch_config(plan.previous_epoch) if plan.previous_epoch is not None else None
    evidence = {
        "measurement_evidence_format_version": MEASUREMENT_EVIDENCE_FORMAT_VERSION, "source": source,
        "workspace_id": ws, "warehouse_id": wh, "as_of_utc": as_of, "source_region": region,
        "billing": history["billing"], "epochs": history["epochs"], "daily_cost": history["daily_cost"],
        "windows": windows, "guard": guard,
        "aws": {"post": bridge.aws_price(observed["warehouse_type"], observed["warehouse_size"], region, aws_registry),
                "pre": None if previous is None else bridge.aws_price(previous["warehouse_type"], previous["warehouse_size"], region, aws_registry)},
        "assumptions": assumptions,
    }
    validate_measurement_evidence(evidence)
    return evidence


def load_measurement_evidence(path: str | Path) -> Dict[str, Any]:
    """The measurement evidence file at *path*, checked against its format (``InputError`` otherwise)."""
    evidence = read_json_input(path, "measurement evidence")
    validate_measurement_evidence(evidence)
    return evidence


def measurement_path(output_dir: Path, evidence: Mapping[str, Any]) -> Path:
    """Where a live run keeps its measurement evidence: ``realization/measurements/<workspace>__<warehouse>__<as of>.json``."""
    stamp = utc_iso(evidence["as_of_utc"]).replace("-", "").replace(":", "")
    return Path(output_dir) / "realization" / "measurements" / f"{evidence['workspace_id']}__{evidence['warehouse_id']}__{stamp}.json"


def measure_live(
    package: Mapping[str, Any], output_dir: Path, *, profile: str, sql_warehouse_id: str, config_file: Optional[str] = None,
    source_region: Optional[str] = None, aws_price_registry: Optional[str] = None, as_of: Optional[str] = None,
    ingestion_cutoff: Optional[str] = None, approvals: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Measure *package*'s warehouse from system tables (read-only OAuth, on another SQL warehouse) and return its
    observation; the measurement evidence is written first, so the run can be replayed with ``observe_from_evidence``."""
    validate_package(package)
    warehouse = package["warehouse"]
    registry = bridge.load_registry(aws_price_registry or str(DEFAULT_AWS_PRICE_REGISTRY))
    auth = bridge.resolve_live_access(profile, sql_warehouse_id, [str(warehouse["warehouse_id"])], config_file)
    as_of = utc_iso(as_of or dt.datetime.now(dt.timezone.utc))
    with bridge.LiveSqlClient(auth) as client:
        evidence = collect_measurement(client, package, as_of=as_of, source="LIVE_SYSTEM_TABLES", source_region=source_region,
                                       aws_registry=registry, ingestion_cutoff=ingestion_cutoff)
    write_json(measurement_path(output_dir, evidence), evidence)
    return project_observation(package, evidence, approvals)


def observe_from_evidence(package: Mapping[str, Any], path: str | Path, approvals: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The observation a measurement evidence file (written by a live run) states for *package*."""
    validate_package(package)
    return project_observation(package, load_measurement_evidence(path), approvals)
__all__ = ["RealizerPolicy", "build_context", "demo_observation", "load_observation", "measure_live", "observe_from_evidence", "realize",
           "realize_request"]


def load_observation(path: str | Path) -> Any:
    """The normalized observation JSON at *path*, parsed (replay); an unreadable file raises ``InputError``."""
    return read_json_input(path, "observation")


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
    # The demo's old cost is at least twice its savings, so the new cost stays positive for any published saving.
    old_cost = max(DEMO_COUNTERFACTUAL_COST, savings * 2)
    reestimate = money(savings * Decimal("365") / DEMO_PERIOD_DAYS) if modified else None
    generated = (package.get("provenance") or {}).get("generated_at") or "2026-10-01T00:00:00Z"
    d = dt.datetime.fromisoformat(generated.replace("Z", "+00:00")) + dt.timedelta(days=8)
    warehouse = package["warehouse"]
    return {
        "observation_format_version": OBSERVATION_FORMAT_VERSION,
        "workspace_id": warehouse["workspace_id"],
        "warehouse_id": warehouse["warehouse_id"],
        "source": "DEMO_NORMALIZED_OBSERVATION",
        "observed_at_utc": utc_iso(d),
        "recorded_at_utc": utc_iso(d),
        "applied_at_utc": utc_iso(d - dt.timedelta(days=8)),
        "effective_time_quality": "OBSERVED_AT_ONLY",
        "window_start_utc": utc_iso(d - dt.timedelta(days=7)),
        "window_end_utc": utc_iso(d),
        "config": cfg,
        "elapsed_days": 8,
        "query_count": 250,
        "representative_regime": True,
        "evidence_quality": "SUFFICIENT",
        "workload": {},
        "performance": {"normalized_p95_runtime_regression_pct": "2", "reliability_pass": True, "correctness_pass": True, "guards": None},
        "financial_quality": {
            "attributable_spend_pct": 100, "price_coverage_pct": 100, "currency_consistency_pct": 100,
            "correction_chain_complete": True, "material_cost_component_available": True,
        },
        "economics": {
            "currency": package_currency(package) or "USD",
            "counterfactual_old_cost": money(old_cost),
            "observed_new_cost": money(old_cost - savings),
            "period_days": DEMO_PERIOD_DAYS,
            "expected_annual_savings_actual_scope": reestimate,
            "counterfactual_method": "MATCHED_HISTORICAL",
            "economic_basis": "ECONOMIC_COST",
            "dbu_realized": None,
            "aws_economic_realized": None,
            "aws_cash_realized": None,
            "aws_commitment_freed": None,
            "aws_evidence_type": None,
            "mature_ttm_realized_savings": None,
            "realized_savings_since_application": None,
        },
        "overlap_count": 0,
        "origin_attribution": "CONFIRMED",
        "intent_state": "ACCEPTED",
        "intent_reference": None,
        "actor": "demo-user",
        "applied_scenario_id": None,
        "rollback_requested": False,
        "rollback_observed": False,
        "rollback_reason": None,
        "replacement_package_id": None,
        "correction": None,
        "measurement": None,
    }
