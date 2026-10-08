"""Analyzer facade: collect evidence for the requested source and analyze every supported warehouse.

Observes only: no eligibility, recommendation or economics decision is made here. Unsupported warehouse types fail
closed (never analyzed); the Governor records their exclusion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from publisher.analyzer.calibration import _prepare_workspace_calibration
from publisher.analyzer.collector.billing import collect_unseen_billed_warehouses
from publisher.analyzer.collector.collector import extract_evidence
from publisher.analyzer.collector.demo import DEMO_FLEET, resolve_demo_analysis_time, synthetic_demo_evidence
from publisher.analyzer.collector.pricing import load_aws_price_registry
from publisher.analyzer.collector.replay import evidence_saved_note, load_evidence_files, save_evidence
from publisher.analyzer.collector.sql_executor import SqlClient
from publisher.analyzer.collector.workspace_api import collect_workspace_compatibility_evidence, resolve_cli_oauth
from publisher.analyzer.findings import Analyzer
from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.cost_profiles import CostProfileSelection, parse_cost_profile_flags
from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES
from publisher.governor.cost_profiles import assign_cost_profiles, log_assignments, thresholds_for_profile
from publisher.governor.thresholds import Thresholds
from publisher.contracts.analysis import FleetAnalysis, WarehouseAnalysis
from publisher.contracts.request import MODE_DEMO, MODE_REPLAY, PublisherRequest, RunContext
from publisher.contracts.source import WarehouseEvidence


def _is_supported_warehouse(e: WarehouseEvidence) -> bool:
    """Fail-closed type gate on the current epoch's warehouse type (`SUPPORTED_WAREHOUSE_TYPES`); shared with the Governor."""
    return e.current_epoch.config.warehouse_type in SUPPORTED_WAREHOUSE_TYPES


def analyze(evidence: Sequence[WarehouseEvidence], thresholds: Thresholds,
            selection: CostProfileSelection = CostProfileSelection()) -> List[WarehouseAnalysis]:
    """Findings for every supported warehouse, in deterministic order, each under its cost profile.

    Fails closed: an unsupported or future warehouse type is never analyzed (the Governor records its exclusion).
    The cost profiles are resolved once over every collected warehouse (the governor's policy; PublisherError for a
    --cost-profile warehouse that was not collected). Workspace calibration runs over the supported warehouses only,
    with the base thresholds, before any per-knob analysis reads it.
    """
    supported = [e for e in evidence if _is_supported_warehouse(e)]
    LOGGER.info("analyzer.analyze_start warehouses=%d supported=%d unsupported=%d", len(evidence), len(supported), len(evidence) - len(supported))
    choices = assign_cost_profiles(evidence, selection, thresholds)
    log_assignments({k: v for k, v in choices.items() if k in {(e.workspace_id, e.warehouse_id) for e in supported}})
    _prepare_workspace_calibration(supported, thresholds)
    ordered = sorted(supported, key=lambda x: (x.workspace_id, x.warehouse_name.lower(), x.warehouse_id))
    results: List[WarehouseAnalysis] = []
    for e in ordered:
        choice = choices[(e.workspace_id, e.warehouse_id)]
        analyses = Analyzer(thresholds_for_profile(thresholds, choice.name)).analyze(e)
        LOGGER.debug("analyzer.warehouse_analyzed workspace_id=%s warehouse_id=%s epochs=%d knobs=%d cost_profile=%s",
                     e.workspace_id, e.warehouse_id, len(e.epochs), len(analyses), choice.name)
        results.append(WarehouseAnalysis(e, analyses, choice))
    LOGGER.info("analyzer.analyze_done analyzed=%d", len(results))
    return results


def collect(request: PublisherRequest, thresholds: Thresholds,
            on_evidence: Optional[Callable[[List[WarehouseEvidence]], None]] = None) -> Tuple[List[WarehouseEvidence], Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Evidence for the requested source: the demo simulation, replay files, or the live system tables.

    Returns the evidence, the unseen billed warehouses (live only) and the AWS price registry (live only). Raises
    PublisherError for an out-of-range --demo-count. Live runs use OAuth profiles only and read system tables read-only.
    ``on_evidence`` is called with the extracted evidence as soon as a live collection has it, before the remaining
    collector queries, so the caller can persist the expensive part.
    """
    if request.mode == MODE_DEMO:
        if not (1 <= request.demo_count <= len(DEMO_FLEET)):
            raise PublisherError(f"--demo-count must be between 1 and {len(DEMO_FLEET)}")
        LOGGER.info("mode.demo synthetic_collector_warehouses=%d", request.demo_count)
        demo = synthetic_demo_evidence(request.demo_count, resolve_demo_analysis_time(request.demo_analysis_time))
        LOGGER.info("analyzer.collect_done mode=demo warehouses=%d", len(demo))
        return demo, None, None
    if request.mode == MODE_REPLAY:
        LOGGER.info("mode.from_evidence files=%d", len(request.evidence_files))
        replayed = load_evidence_files(list(request.evidence_files))
        LOGGER.info("analyzer.collect_done mode=replay warehouses=%d", len(replayed))
        return replayed, None, None
    auth = resolve_cli_oauth(request.profile, request.execution_kind, request.execution_id, request.config_file, request.workspace_id)
    compat = collect_workspace_compatibility_evidence(auth, request.compatibility_api_mode)
    LOGGER.info("compatibility.mode=%s status=%s workspace_id=%s", compat.get("mode"), compat.get("status"), compat.get("workspace_id"))
    registry = load_aws_price_registry(request.aws_price_registry)
    def refresh():
        return resolve_cli_oauth(request.profile, request.execution_kind, request.execution_id, request.config_file, request.workspace_id)

    with SqlClient(auth, refresh=refresh) as client:
        evidence = extract_evidence(client, thresholds, request.source_region, registry, compat, ingestion_cutoff=request.billing_ingested_through)
        if on_evidence is not None:
            on_evidence(evidence)
        unseen = collect_unseen_billed_warehouses(client, ingestion_cutoff=request.billing_ingested_through)
    LOGGER.info("analyzer.collect_done mode=live warehouses=%d unseen_billed=%d", len(evidence), len(unseen or []))
    return evidence, unseen, registry


def save_collected_evidence(request: PublisherRequest, evidence: Sequence[WarehouseEvidence]) -> Path:
    """Write the freshly collected evidence to ``<output dir>/evidence.json`` (the versioned, schema-validated replay envelope).

    A live collection covers 365 days of system tables; saving it before any decision keeps it replayable
    (``--from-evidence``) when a later stage refuses or crashes. The Reporter rewrites the same bytes at the end of a
    successful run.
    """
    request.output_dir.mkdir(parents=True, exist_ok=True)
    path = request.output_dir / "evidence.json"
    save_evidence(path, evidence)
    LOGGER.info("evidence.saved_early path=%s warehouses=%d", path, len(evidence))
    return path


def run(context: RunContext) -> FleetAnalysis:
    """What is happening: collect and normalize evidence, then analyze every supported warehouse.

    The returned FleetAnalysis keeps all collected evidence (including unsupported types) beside findings for the
    supported warehouses only. A live run saves the evidence as soon as it is extracted (``FleetAnalysis.evidence_path``);
    a refusal after that point names the saved file and ``--from-evidence``.
    """
    saved: List[Path] = []

    def on_evidence(evidence: List[WarehouseEvidence]) -> None:
        saved.append(save_collected_evidence(context.request, evidence))

    try:
        evidence, unseen, registry = collect(context.request, context.thresholds, on_evidence)
        selection = parse_cost_profile_flags(context.request.cost_profiles)
        return FleetAnalysis(evidence=list(evidence), warehouses=analyze(evidence, context.thresholds, selection),
                             unseen_billed_warehouses=unseen, aws_price_registry=registry, evidence_path=saved[0] if saved else None)
    except PublisherError as exc:
        if saved:
            raise PublisherError(f"{exc} {evidence_saved_note(saved[0])}") from exc
        raise
    except Exception:
        if saved:
            LOGGER.error("%s", evidence_saved_note(saved[0]))
        raise
