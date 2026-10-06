"""Realizer Workbook Extension Contract v1.0.0.

This is a separately versioned presentation projection over immutable Publisher
PKG + canonical Realizer RPKG/RLE. It does not modify Workbook Contract v1.1.0
and MUST NOT recompute recommendation or realization economics.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

from . import realizer_contract as rrc

REALIZER_WORKBOOK_EXTENSION_VERSION = "1.0.0"
SHEET_REALIZATION_SUMMARY = "Realization Summary"
SHEET_REALIZATION_TIMELINE = "Realization Timeline"
SHEET_NAMES = (SHEET_REALIZATION_SUMMARY, SHEET_REALIZATION_TIMELINE)


@dataclass(frozen=True)
class RealizerWorkbookProjection:
    realization_summary: List[Dict[str, Any]]
    realization_timeline: List[Dict[str, Any]]

    def validate(self, realizations: Sequence[rrc.RealizationPackage]) -> None:
        expected = {x.realization_package_id for x in realizations}
        actual = {r["realization_package_id"] for r in self.realization_summary}
        if expected != actual or len(self.realization_summary) != len(realizations):
            raise ValueError("one Realization Summary row required per RPKG")


def _json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _money_float(v: Any) -> float | None:
    if v is None:
        return None
    return round(float(v), 2)


def project_summary(r: rrc.RealizationPackage) -> Dict[str, Any]:
    e, a, attr = r.economics, r.application, r.attribution
    return {
        "recommendation_package_id": r.source_recommendation.get("recommendation_package_id"),
        "realization_package_id": r.realization_package_id,
        "snapshot_id": r.snapshot_id,
        "workspace_id": r.warehouse.get("workspace_id"),
        "warehouse_id": r.warehouse.get("warehouse_id"),
        "warehouse_name": r.warehouse.get("warehouse_name"),
        "source_package_disposition": r.source_recommendation.get("disposition"),
        "implementation_conformance": a.get("implementation_conformance"),
        "application_scope": a.get("application_scope"),
        "intent_state": a.get("intent_state"),
        "recommended_value": a.get("recommended_value"),
        "observed_applied_value": a.get("observed_applied_value"),
        "application_state": a.get("state"),
        "plan_progress": (a.get("plan_progress") or {}).get("state"),
        "validation_state": r.validation.get("state"),
        "realization_state": r.current_state.get("realization_state"),
        "maturity_state": r.maturity.get("state"),
        "origin_attribution": attr.get("origin_attribution"),
        "attribution_claim_status": attr.get("attribution_claim_status"),
        "observed_realized_savings": _money_float(attr.get("validated_observed_realization")),
        "recommendation_attributed_savings": _money_float(attr.get("recommendation_attributed_realized_savings")),
        "annualized_realized_savings": _money_float(e.get("annualized_realized_savings")),
        "mature_ttm_realized_savings": _money_float(e.get("mature_ttm_realized_savings")),
        "expected_same_scope_annual_savings": _money_float(e.get("expected_same_scope_annual_savings")),
        "realization_ratio": None if e.get("realization_ratio") is None else float(e.get("realization_ratio")),
        "economic_basis": e.get("economic_basis"),
        "denominator_source": e.get("denominator_source"),
        "regeneration_status": r.regeneration.get("status"),
        "replacement_package_id": r.regeneration.get("replacement_package_id"),
        "as_of_utc": r.as_of_utc,
        "application_json": _json(a),
        "validation_json": _json(r.validation),
        "economics_json": _json(e),
        "attribution_json": _json(attr),
    }


def project_timeline(entries: Sequence[rrc.RealizationLedgerEntry]) -> List[Dict[str, Any]]:
    return [
        {
            "ledger_entry_id": e.ledger_entry_id,
            "realization_package_id": e.realization_package_id,
            "recommendation_package_id": e.recommendation_package_id,
            "event_type": e.event_type.value,
            "effective_at_utc": e.effective_at_utc,
            "recorded_at_utc": e.recorded_at_utc,
            "workspace_id": e.workspace_id,
            "warehouse_id": e.warehouse_id,
            "application_episode_id": e.application_episode_id,
            "validation_episode_id": e.validation_episode_id,
            "measurement_id": e.measurement_id,
            "corrects_entry_id": e.corrects_entry_id,
            "evidence_refs": _json(e.evidence_refs),
            "payload_json": _json(e.payload),
        }
        for e in sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))
    ]


def project_realizer_workbook(
    realizations: Sequence[rrc.RealizationPackage],
    ledgers: Mapping[str, Sequence[rrc.RealizationLedgerEntry]],
) -> RealizerWorkbookProjection:
    projection = RealizerWorkbookProjection(
        realization_summary=[project_summary(r) for r in realizations],
        realization_timeline=[row for r in realizations for row in project_timeline(ledgers.get(r.realization_package_id, ()))],
    )
    projection.validate(realizations)
    return projection
