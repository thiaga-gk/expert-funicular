"""Evidence registry: public evidence references for findings, scenario compatibility checks and shared cost inputs."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from publisher.analyzer.epochs import ready_billed_days
from publisher.common.utils import round_or_none
from publisher.config.versions import MODEL_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.scenario import Scenario
from publisher.contracts.source import WarehouseEvidence


def _add_evidence(
    registry: Dict[str, rc.EvidenceRecord],
    *,
    e: WarehouseEvidence,
    contract_epoch_id: str,
    metric: str,
    value: Any,
    unit: str,
    source: str,
    source_field: str,
    observation_window: str,
    threshold: Any,
    comparator: str,
    interpretation: str,
    epoch_scope: str = "CURRENT",
) -> str:
    """Register one evidence record under its content-derived ``EVID-`` ID and return the ID.

    The ID hashes the metric, value, unit, source, window, threshold and comparator, so identical evidence
    de-duplicates; the same ID with a different record is a collision and raises.
    """
    evidence_id = rc.make_evidence_id(
        workspace_id=e.workspace_id,
        warehouse_id=e.warehouse_id,
        epoch_id=contract_epoch_id,
        metric=metric,
        value=value,
        unit=unit,
        source=source,
        source_field=source_field,
        observation_window=observation_window,
        threshold=threshold,
        comparator=comparator,
    )
    record = rc.EvidenceRecord(
        evidence_id=evidence_id,
        metric=metric,
        value=value,
        unit=unit,
        source=source,
        source_field=source_field,
        epoch_scope=epoch_scope,
        observation_window=observation_window,
        threshold=threshold,
        comparator=comparator,
        interpretation=interpretation,
    )
    if evidence_id in registry and registry[evidence_id] != record:
        raise ValueError(f"Evidence ID collision for {evidence_id}")
    registry[evidence_id] = record
    return evidence_id


def _build_evidence_registry(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    scenarios: Sequence[Scenario],
    contract_epoch_id: str,
) -> Tuple[Dict[str, rc.EvidenceRecord], Dict[str, str], Dict[Tuple[str, str], str], List[str]]:
    """Build one deduplicated evidence registry and return old-finding/check mappings plus shared cost refs.

    Returns (registry, internal finding ID -> EVID, (scenario name, check) -> EVID, shared cost EVIDs). Shared cost
    refs are attached to every candidate (economics are decision-relevant and part of the evidence fingerprint);
    the billing-correction and Preview-channel records are package evidence only: no candidate references them, so
    no decision or ID depends on them. Registry insertion order is deterministic (findings, costs, checks).
    """
    registry: Dict[str, rc.EvidenceRecord] = {}
    finding_map: Dict[str, str] = {}
    compatibility_map: Dict[Tuple[str, str], str] = {}

    for c in candidates:
        for f in c.findings:
            eid = _add_evidence(
                registry, e=e, contract_epoch_id=contract_epoch_id,
                metric=f.metric, value=f.value, unit=f.unit or "value",
                source=f.source_table, source_field=f.source_field,
                observation_window=f.window or "current_epoch", epoch_scope=f.epoch_scope,
                threshold=f.threshold, comparator=f.comparator or "",
                interpretation=f.interpretation,
            )
            finding_map[f.finding_id] = eid

    # Economics are decision-relevant and therefore part of the evidence fingerprint.
    shared_cost_refs = [
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="actual_dbu_cost_365d", value=round_or_none(e.current_cost_365, 2), unit=e.currency,
            source="system.billing.usage", source_field="usage_quantity × historical effective list price",
            observation_window="trailing_365d", threshold=None, comparator="",
            interpretation="Observed trailing-365-day Databricks SQL list cost attributed to the warehouse.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="forward_dbu_cost_365d", value=round_or_none(e.dbu_365d_baseline_cost, 2), unit=e.currency,
            source="derived", source_field=MODEL_VERSION,
            observation_window="forward_365d", threshold=None, comparator="",
            interpretation="No-change forward DBU cost forecast anchored to the current configuration epoch.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="forward_aws_cost_365d", value=None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365, 2), unit=e.currency,
            source="derived", source_field="aws_current_epoch_cost_model",
            observation_window="forward_365d", threshold=None, comparator="",
            interpretation="Forward customer AWS infrastructure cost model for the current configuration epoch; null when unavailable.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="cost_history_days", value=e.cost_history_days, unit="days",
            source="system.billing.usage", source_field="usage_date",
            observation_window="current_epoch", threshold=None, comparator="",
            interpretation="Calendar-day coverage available to the forward cost model.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="billed_cost_days", value=ready_billed_days(e), unit="days",
            source="system.billing.usage", source_field="usage_date/usage_quantity",
            observation_window="current_epoch", threshold=rc.REEVALUATION_READY_BILLED_DAYS, comparator=">= readiness",
            interpretation="Settled days of the current epoch with billed usage: the current configuration is ready once it reaches the readiness count.",
        ),
    ]
    # Cost-basis quality signal: package evidence only. It is not a shared cost ref, so it never enters candidate
    # evidence refs (and their IDs); no decision, confidence or economics reads it.
    _add_evidence(
        registry, e=e, contract_epoch_id=contract_epoch_id,
        metric="billing_correction_dbu_share_365d", value=round_or_none((e.forecast_metadata or {}).get("billing_correction_dbu_share_365"), 6), unit="ratio",
        source="system.billing.usage", source_field="record_type",
        observation_window="trailing_365d", threshold=None, comparator="",
        interpretation="Retracted DBU (RETRACTION records) per originally billed DBU in the settled trailing window; can exceed 1 when a day is corrected more than once. A cost-basis quality signal, not used in decisions; null when not collected.",
    )

    # The channel is observed, never recommended: a Preview warehouse gets an informational record only (package
    # evidence; no candidate references it, so no decision or ID depends on it).
    if e.current_epoch.config.warehouse_channel == "PREVIEW":
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="warehouse_channel", value="PREVIEW", unit="state",
            source="system.compute.warehouses", source_field="warehouse_channel",
            observation_window="current_epoch", threshold=None, comparator="",
            interpretation="Informational: the warehouse runs on the Preview channel. No recommendation is made; confirm no workload depends on Preview-only behavior before any move to Current.",
        )

    for s in scenarios:
        for check in (s.compatibility or {}).get("checks", []) or []:
            name = str(check.get("check") or "compatibility_check")
            status = str(check.get("status") or "UNKNOWN")
            source = str(check.get("source") or (s.compatibility or {}).get("evidence_mode") or "compatibility")
            details = check.get("evidence")
            eid = _add_evidence(
                registry, e=e, contract_epoch_id=contract_epoch_id,
                metric=f"compatibility.{name}", value={"status": status, "evidence": details}, unit="state",
                source=source, source_field=name, observation_window="point_in_time",
                threshold="PASS", comparator="compatibility_check",
                interpretation=f"Compatibility check {name} returned {status}.",
            )
            compatibility_map[(s.name, name)] = eid

    return registry, finding_map, compatibility_map, shared_cost_refs
