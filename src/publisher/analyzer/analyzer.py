"""Analyzer facade: collect evidence for the requested source and analyze every supported warehouse."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from publisher.analyzer.calibration import _prepare_workspace_calibration
from publisher.analyzer.collector.billing import collect_unseen_billed_warehouses
from publisher.analyzer.collector.collector import extract_evidence
from publisher.analyzer.collector.demo import DEMO_FLEET, synthetic_demo_evidence
from publisher.analyzer.collector.pricing import load_aws_price_registry
from publisher.analyzer.collector.replay import load_evidence_files
from publisher.analyzer.collector.sql_executor import SqlClient
from publisher.analyzer.collector.workspace_api import collect_workspace_compatibility_evidence, resolve_cli_oauth
from publisher.analyzer.findings import Analyzer
from publisher.common.logging import LOGGER
from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES
from publisher.governor.thresholds import Thresholds
from publisher.contracts.analysis import FleetAnalysis, WarehouseAnalysis
from publisher.contracts.request import MODE_DEMO, MODE_REPLAY, PublisherRequest, RunContext
from publisher.contracts.source import WarehouseEvidence


def _is_supported_warehouse(e: WarehouseEvidence) -> bool:
    return e.current_epoch.config.warehouse_type in SUPPORTED_WAREHOUSE_TYPES


def analyze(evidence: Sequence[WarehouseEvidence], thresholds: Thresholds) -> List[WarehouseAnalysis]:
    """Findings for every supported warehouse, in deterministic order.

    Fails closed: an unsupported or future warehouse type is never analyzed (the Governor records its exclusion).
    Workspace calibration runs over the supported warehouses only, before any per-knob analysis reads it.
    """
    supported = [e for e in evidence if _is_supported_warehouse(e)]
    _prepare_workspace_calibration(supported, thresholds)
    ordered = sorted(supported, key=lambda x: (x.workspace_id, x.warehouse_name.lower(), x.warehouse_id))
    return [WarehouseAnalysis(e, Analyzer(thresholds).analyze(e)) for e in ordered]


def collect(request: PublisherRequest, thresholds: Thresholds) -> Tuple[List[WarehouseEvidence], Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Evidence for the requested source: the demo simulation, replay files, or the live system tables.

    Returns the evidence, the unseen billed warehouses (live only) and the AWS price registry (live only).
    """
    if request.mode == MODE_DEMO:
        if not (1 <= request.demo_count <= len(DEMO_FLEET)):
            raise SystemExit(f"--demo-count must be between 1 and {len(DEMO_FLEET)}")
        LOGGER.info("mode.demo synthetic_collector_warehouses=%d", request.demo_count)
        return synthetic_demo_evidence(request.demo_count), None, None
    if request.mode == MODE_REPLAY:
        LOGGER.info("mode.from_evidence files=%d", len(request.evidence_files))
        return load_evidence_files(list(request.evidence_files)), None, None
    auth = resolve_cli_oauth(request.profile, request.execution_kind, request.execution_id, request.config_file, request.workspace_id)
    compat = collect_workspace_compatibility_evidence(auth, request.compatibility_api_mode)
    LOGGER.info("compatibility.mode=%s status=%s workspace_id=%s", compat.get("mode"), compat.get("status"), compat.get("workspace_id"))
    registry = load_aws_price_registry(request.aws_price_registry)
    with SqlClient(auth) as client:
        evidence = extract_evidence(client, thresholds, request.source_region, registry, compat, ingestion_cutoff=request.billing_ingested_through)
        unseen = collect_unseen_billed_warehouses(client, ingestion_cutoff=request.billing_ingested_through)
    return evidence, unseen, registry


def run(context: RunContext) -> FleetAnalysis:
    """What is happening: collect and normalize evidence, then analyze every supported warehouse."""
    evidence, unseen, registry = collect(context.request, context.thresholds)
    return FleetAnalysis(evidence=list(evidence), warehouses=analyze(evidence, context.thresholds),
                         unseen_billed_warehouses=unseen, aws_price_registry=registry)
