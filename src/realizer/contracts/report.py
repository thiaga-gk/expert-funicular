"""Realizer report contract: the Realization Workbook Contract v2.0.0 and the lineage projection.

A presentation projection over the canonical Realizer RPKG, its ledger (RLE) and the warehouse's state index. It never
recomputes recommendation or realization economics and never decides: every value comes from those inputs as given.

* The realization workbook (``project_realization_workbook``; rendered by ``realizer.reporter.workbook``): three sheets,
  Warehouses (one row per warehouse of a fleet run), Lineages (one row per realization package) and Timeline (the ledger
  events). This module is the only home of their column order and groups and of the next-action label table; the renderer
  keeps no second list.
* The lineage projection (``project_summary``, ``project_timeline``, ``project_realizer_workbook``): the summary and
  timeline rows of a set of realization packages, which the stored scenario baseline pins (``report.json``) and the
  Lineages and Timeline sheets reuse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.common.utils import parse_utc
from realizer.contracts import realization as rrc

# 2.0.0: the realization workbook (Warehouses, Lineages, Timeline) replaces the two-sheet Realizer Workbook Extension
# 1.0.0 (Realization Summary, Realization Timeline).
REALIZATION_WORKBOOK_CONTRACT_VERSION = "2.0.0"
SHEET_WAREHOUSES = "Warehouses"
SHEET_LINEAGES = "Lineages"
SHEET_TIMELINE = "Timeline"
SHEET_NAMES = (SHEET_WAREHOUSES, SHEET_LINEAGES, SHEET_TIMELINE)


@dataclass(frozen=True)
class RealizationReport:
    """Reporter output: the realization package a run projected and the files the Reporter wrote for it.

    ``realization`` is the RealizationPackage (RPKG-*) body the package and the snapshot file both hold. ``files`` are
    the paths written, in write order: ``realization/packages/<RPKG>.json``, then ``realization/snapshots/<RSNAP>.json``,
    then the same two for each other lineage of the warehouse the run re-projected, then the warehouse's state index
    ``realization/state/<workspace>__<warehouse>.json`` (the evidence, regeneration and policy files are the Packager's,
    in ``PackagingResult.written``). An internal hand-off, not part of the public contract surface.
    """
    realization: rrc.RealizationPackage
    files: Tuple[Path, ...] = ()


# Run status of one warehouse in a fleet run (the run manifest and the realization workbook).
RUN_STATUS_OK = "OK"
RUN_STATUS_REFUSED = "REFUSED"  # a RealizerError: an input the Realizer does not accept, an unreadable file, a locked ledger
RUN_STATUS_CRASHED = "CRASHED"  # any other exception: a defect, recorded by its type only
RUN_STATUSES = (RUN_STATUS_OK, RUN_STATUS_REFUSED, RUN_STATUS_CRASHED)


@dataclass(frozen=True)
class WarehouseRun:
    """What one fleet run learned about one warehouse: the hand-off from the run to the realization workbook.

    ``status`` is one of ``RUN_STATUSES``; ``reason`` is the refusal message (a ``RealizerError``'s, which never carries
    data values) or the exception type of a crash, ``None`` when OK. ``realizations`` are the realization packages the
    run projected for the warehouse (every lineage it realized or re-projected, in package id order; empty when the
    warehouse failed before any was written), ``ledgers`` their ledger entries by realization package id, and
    ``state_index`` the warehouse's state index as written (``None`` when none was). ``next_steps`` maps a package id to
    the first step of its plan a person applies (``realizer.analyzer.analysis.next_plan_step``: ``step``, ``knob``,
    ``from_value``, ``to_value``), which names the next action of a lineage not yet applied.
    """
    workspace_id: str
    warehouse_id: str
    status: str
    reason: Optional[str] = None
    recommendation_package_id: Optional[str] = None
    realizations: Tuple[rrc.RealizationPackage, ...] = ()
    ledgers: Mapping[str, Sequence[rrc.RealizationLedgerEntry]] = field(default_factory=dict)
    state_index: Optional[Mapping[str, Any]] = None
    next_steps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


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
        for e in sorted(entries, key=lambda x: parse_utc(x.recorded_at_utc))  # ledger order: time, then append order
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


# ---------------------------------------------------------------------------
# The realization workbook (Realization Workbook Contract REALIZATION_WORKBOOK_CONTRACT_VERSION)
# ---------------------------------------------------------------------------

# What a column holds, which decides how it is written: an id as text, money as a signed number in the row's currency, a
# ratio as a number, a count or a number of days as a number, a time or a day as text, an audit JSON text, other text.
KIND_ID = "ID"
KIND_TEXT = "TEXT"
KIND_TIME = "TIME"
KIND_MONEY = "MONEY"
KIND_RATIO = "RATIO"
KIND_NUMBER = "NUMBER"
KIND_COUNT = "COUNT"
KIND_JSON = "JSON"
KIND_VALUE = "VALUE"  # a configuration value: a number (auto-stop minutes, cluster counts) or a text (size, type)


@dataclass(frozen=True)
class ColumnSpec:
    """One workbook column: data key, header text, what it holds (``KIND_*``) and width. The order of a sheet's column
    tuple is the order on the sheet."""
    key: str
    header: str
    kind: str = KIND_TEXT
    width: float = 16.0


@dataclass(frozen=True)
class ColumnGroup:
    """A banner over a contiguous run of columns on one sheet, from start_key to end_key inclusive."""
    title: str
    start_key: str
    end_key: str


WAREHOUSE_COLUMNS = (
    ColumnSpec("workspace_id", "Workspace ID", KIND_ID, 18),
    ColumnSpec("warehouse_id", "Warehouse ID", KIND_ID, 16),
    ColumnSpec("warehouse_name", "Warehouse", KIND_TEXT, 22),
    ColumnSpec("recommendation_package_id", "Package ID", KIND_ID, 30),
    ColumnSpec("realization_package_id", "Realization package ID", KIND_ID, 31),
    ColumnSpec("lineage_basis", "Lineage shown", KIND_TEXT, 24),
    ColumnSpec("run_status", "Run status", KIND_TEXT, 11),
    ColumnSpec("run_reason", "Run reason", KIND_TEXT, 30),
    ColumnSpec("applied_knob", "Applied knob", KIND_TEXT, 18),
    ColumnSpec("from_value", "From", KIND_VALUE, 10),
    ColumnSpec("applied_value", "Applied", KIND_VALUE, 10),
    ColumnSpec("recommended_value", "Recommended", KIND_VALUE, 12),
    ColumnSpec("applied_at_utc", "Applied at (UTC)", KIND_TIME, 21),
    ColumnSpec("applied_until_utc", "Applied until (UTC)", KIND_TIME, 21),
    ColumnSpec("implementation_conformance", "Conformance", KIND_TEXT, 13),
    ColumnSpec("validation_state", "Validation", KIND_TEXT, 12),
    ColumnSpec("realization_state", "Realization", KIND_TEXT, 17),
    ColumnSpec("maturity_state", "Maturity", KIND_TEXT, 15),
    ColumnSpec("currency", "Currency", KIND_TEXT, 9),
    ColumnSpec("realized_to_date", "Realized to date", KIND_MONEY, 15),
    ColumnSpec("days_covered", "Days covered", KIND_NUMBER, 10),
    ColumnSpec("first_day", "First day", KIND_TIME, 11),
    ColumnSpec("last_day", "Last day", KIND_TIME, 11),
    ColumnSpec("annualized_realized_savings", "Annualized realized", KIND_MONEY, 15),
    ColumnSpec("expected_same_scope_annual_savings", "Expected annual (same scope)", KIND_MONEY, 16),
    ColumnSpec("realization_ratio", "Realization ratio", KIND_RATIO, 11),
    ColumnSpec("attribution_claim_status", "Claim", KIND_TEXT, 16),
    ColumnSpec("claimed_by_package_id", "Claimed by package", KIND_ID, 30),
    ColumnSpec("other_lineages", "Other lineages", KIND_COUNT, 9),
    ColumnSpec("next_action", "Next action", KIND_TEXT, 36),
    ColumnSpec("as_of_utc", "Last observed (UTC)", KIND_TIME, 21),
    ColumnSpec("application_json", "Application JSON", KIND_JSON, 24),
    ColumnSpec("economics_json", "Economics JSON", KIND_JSON, 24),
    ColumnSpec("attribution_json", "Attribution JSON", KIND_JSON, 24),
    ColumnSpec("applied_knob_owners_json", "Applied knob owners JSON", KIND_JSON, 24),
)
WAREHOUSE_GROUPS = (
    ColumnGroup("Identity", "workspace_id", "lineage_basis"),
    ColumnGroup("Run", "run_status", "run_reason"),
    ColumnGroup("Applied change", "applied_knob", "applied_until_utc"),
    ColumnGroup("State", "implementation_conformance", "maturity_state"),
    ColumnGroup("Realized economics", "currency", "realization_ratio"),
    ColumnGroup("Attribution", "attribution_claim_status", "other_lineages"),
    ColumnGroup("Action", "next_action", "as_of_utc"),
    ColumnGroup("Audit", "application_json", "applied_knob_owners_json"),
)

LINEAGE_COLUMNS = (
    ColumnSpec("recommendation_package_id", "Package ID", KIND_ID, 30),
    ColumnSpec("realization_package_id", "Realization package ID", KIND_ID, 31),
    ColumnSpec("snapshot_id", "Snapshot ID", KIND_ID, 31),
    ColumnSpec("workspace_id", "Workspace ID", KIND_ID, 18),
    ColumnSpec("warehouse_id", "Warehouse ID", KIND_ID, 16),
    ColumnSpec("warehouse_name", "Warehouse", KIND_TEXT, 22),
    ColumnSpec("source_package_disposition", "Package disposition", KIND_TEXT, 13),
    ColumnSpec("implementation_conformance", "Conformance", KIND_TEXT, 13),
    ColumnSpec("application_scope", "Application scope", KIND_TEXT, 12),
    ColumnSpec("intent_state", "Intent", KIND_TEXT, 11),
    ColumnSpec("recommended_value", "Recommended", KIND_VALUE, 12),
    ColumnSpec("observed_applied_value", "Applied", KIND_VALUE, 10),
    ColumnSpec("application_state", "Application", KIND_TEXT, 13),
    ColumnSpec("plan_progress", "Plan progress", KIND_TEXT, 20),
    ColumnSpec("validation_state", "Validation", KIND_TEXT, 12),
    ColumnSpec("realization_state", "Realization", KIND_TEXT, 17),
    ColumnSpec("maturity_state", "Maturity", KIND_TEXT, 15),
    ColumnSpec("origin_attribution", "Origin", KIND_TEXT, 12),
    ColumnSpec("attribution_claim_status", "Claim", KIND_TEXT, 16),
    ColumnSpec("claimed_by_package_id", "Claimed by package", KIND_ID, 30),
    ColumnSpec("currency", "Currency", KIND_TEXT, 9),
    ColumnSpec("observed_realized_savings", "Observed realization", KIND_MONEY, 15),
    ColumnSpec("recommendation_attributed_savings", "Attributed realization", KIND_MONEY, 15),
    ColumnSpec("realized_to_date", "Realized to date", KIND_MONEY, 15),
    ColumnSpec("realized_to_date_days_covered", "Days covered", KIND_NUMBER, 10),
    ColumnSpec("annualized_realized_savings", "Annualized realized", KIND_MONEY, 15),
    ColumnSpec("mature_ttm_realized_savings", "Mature TTM realized", KIND_MONEY, 15),
    ColumnSpec("expected_same_scope_annual_savings", "Expected annual (same scope)", KIND_MONEY, 16),
    ColumnSpec("realization_ratio", "Realization ratio", KIND_RATIO, 11),
    ColumnSpec("economic_basis", "Economic basis", KIND_TEXT, 16),
    ColumnSpec("denominator_source", "Denominator source", KIND_TEXT, 22),
    ColumnSpec("regeneration_status", "Regeneration", KIND_TEXT, 13),
    ColumnSpec("replacement_package_id", "Replacement package", KIND_ID, 30),
    ColumnSpec("next_action", "Next action", KIND_TEXT, 36),
    ColumnSpec("as_of_utc", "As of (UTC)", KIND_TIME, 21),
    ColumnSpec("application_json", "Application JSON", KIND_JSON, 24),
    ColumnSpec("validation_json", "Validation JSON", KIND_JSON, 24),
    ColumnSpec("economics_json", "Economics JSON", KIND_JSON, 24),
    ColumnSpec("attribution_json", "Attribution JSON", KIND_JSON, 24),
)
LINEAGE_GROUPS = (
    ColumnGroup("Identity", "recommendation_package_id", "source_package_disposition"),
    ColumnGroup("Application", "implementation_conformance", "plan_progress"),
    ColumnGroup("State", "validation_state", "maturity_state"),
    ColumnGroup("Attribution", "origin_attribution", "claimed_by_package_id"),
    ColumnGroup("Realized economics", "currency", "denominator_source"),
    ColumnGroup("Action", "regeneration_status", "as_of_utc"),
    ColumnGroup("Audit", "application_json", "attribution_json"),
)

TIMELINE_COLUMNS = (
    ColumnSpec("ledger_entry_id", "Ledger entry ID", KIND_ID, 30),
    ColumnSpec("realization_package_id", "Realization package ID", KIND_ID, 31),
    ColumnSpec("recommendation_package_id", "Package ID", KIND_ID, 30),
    ColumnSpec("event_type", "Event", KIND_TEXT, 34),
    ColumnSpec("effective_at_utc", "Effective at (UTC)", KIND_TIME, 21),
    ColumnSpec("recorded_at_utc", "Recorded at (UTC)", KIND_TIME, 21),
    ColumnSpec("workspace_id", "Workspace ID", KIND_ID, 18),
    ColumnSpec("warehouse_id", "Warehouse ID", KIND_ID, 16),
    ColumnSpec("application_episode_id", "Application episode ID", KIND_ID, 30),
    ColumnSpec("validation_episode_id", "Validation episode ID", KIND_ID, 30),
    ColumnSpec("measurement_id", "Measurement ID", KIND_ID, 31),
    ColumnSpec("corrects_entry_id", "Corrects entry ID", KIND_ID, 30),
    ColumnSpec("evidence_refs", "Evidence refs JSON", KIND_JSON, 24),
    ColumnSpec("payload_json", "Payload JSON", KIND_JSON, 24),
)
TIMELINE_GROUPS = (
    ColumnGroup("Identity", "ledger_entry_id", "recommendation_package_id"),
    ColumnGroup("Event", "event_type", "recorded_at_utc"),
    ColumnGroup("Warehouse", "workspace_id", "warehouse_id"),
    ColumnGroup("Episodes", "application_episode_id", "corrects_entry_id"),
    ColumnGroup("Audit", "evidence_refs", "payload_json"),
)

SHEET_COLUMNS: Mapping[str, Tuple[Tuple[ColumnSpec, ...], Tuple[ColumnGroup, ...]]] = {
    SHEET_WAREHOUSES: (WAREHOUSE_COLUMNS, WAREHOUSE_GROUPS),
    SHEET_LINEAGES: (LINEAGE_COLUMNS, LINEAGE_GROUPS),
    SHEET_TIMELINE: (TIMELINE_COLUMNS, TIMELINE_GROUPS),
}

# Why the Warehouses row shows the lineage it shows (``select_lineage``).
LINEAGE_BASIS_CLAIMS_LATEST_CHANGE = "CLAIMS_LATEST_CHANGE"
LINEAGE_BASIS_NEWEST_ACTIVE_PACKAGE = "NEWEST_ACTIVE_PACKAGE"

# The next action: one fixed label per (state, condition). The state is a lineage's realization state, a run status
# (REFUSED, CRASHED: the run itself failed) or NO_LINEAGE (a run that projected no lineage to show). The condition refines
# a state where the regeneration status or the plan progress changes what a person does; None is the state's label
# otherwise.
NO_LINEAGE = "NO_LINEAGE"
WHEN_NOTHING_TO_APPLY = "NO_ACTIONABLE_STEP"          # plan progress: the package has no authority-eligible step
WHEN_REGENERATION_REQUESTED = "REGENERATION_REQUESTED"  # regeneration status REQUESTED
WHEN_REPLACED = "REPLACEMENT_LINKED"                    # regeneration status REPLACED: a replacement package is linked

_RUN_THE_PUBLISHER = "Run the Publisher: regeneration requested"
_KEEP_MONITORING = "Keep monitoring"
_WAIT = "Wait for validation"
_REVIEW_BLOCKER = "Review the blocker"
_REPLACED = "None: replacement package linked"
NEXT_ACTIONS: Mapping[Tuple[str, Optional[str]], str] = {
    (RUN_STATUS_REFUSED, None): "Fix the run: REFUSED",
    (RUN_STATUS_CRASHED, None): "Fix the run: CRASHED",
    (NO_LINEAGE, None): "None: no realization to show",
    ("NOT_APPLIED", None): "Apply the recommended change",
    ("NOT_APPLIED", WHEN_NOTHING_TO_APPLY): "None: no step to apply",
    ("APPLIED", None): _WAIT,
    ("VALIDATING", None): _WAIT,
    ("REALIZED", None): _KEEP_MONITORING,
    ("MONITORING", None): _KEEP_MONITORING,
    ("PERFORMANCE_VALIDATED", None): _KEEP_MONITORING,
    ("CAPACITY_VALIDATED", None): _KEEP_MONITORING,
    ("SAFETY_ACTION_APPLIED", None): "Keep monitoring: safety rollback in force",
    ("UNDER_REALIZED", None): "Review: re-optimization recommended",
    ("VALIDATION_FAILED", None): "Roll back: a guard failed",
    ("ROLLED_BACK", None): "None: rolled back",
    ("ROLLED_BACK", WHEN_REGENERATION_REQUESTED): _RUN_THE_PUBLISHER,
    ("ROLLED_BACK", WHEN_REPLACED): _REPLACED,
    ("DRIFTED", None): _RUN_THE_PUBLISHER,
    ("DRIFTED", WHEN_REPLACED): _REPLACED,
    ("REGENERATING", None): _RUN_THE_PUBLISHER,
    ("REGENERATING", WHEN_REPLACED): _REPLACED,
    ("BLOCKED", None): _REVIEW_BLOCKER,
    ("INVALIDATED", None): _REVIEW_BLOCKER,
}


def _condition(r: rrc.RealizationPackage, state: str) -> Optional[str]:
    """The condition of *r* that refines its state's label, if the table has a row for it."""
    regeneration = (r.regeneration or {}).get("status")
    progress = (r.application or {}).get("plan_progress") or {}
    candidates = []
    if regeneration == rrc.RegenerationStatus.REPLACED.value:
        candidates.append(WHEN_REPLACED)
    if regeneration == rrc.RegenerationStatus.REQUESTED.value:
        candidates.append(WHEN_REGENERATION_REQUESTED)
    if progress.get("authoritative_actionable_steps") == 0:
        candidates.append(WHEN_NOTHING_TO_APPLY)
    return next((c for c in candidates if (state, c) in NEXT_ACTIONS), None)


APPLY_STEP = "Apply step {step}: {knob} {from_value} \u2192 {to_value}"  # NOT_APPLIED with the package's first step known


def next_action(r: rrc.RealizationPackage, next_step: Optional[Mapping[str, Any]] = None) -> str:
    """The next action label of a lineage, from ``NEXT_ACTIONS`` by its realization state and, where the table
    distinguishes it, its regeneration status or plan progress. A lineage not yet applied names *next_step* (its
    package's first plan step, ``WarehouseRun.next_steps``) with ``APPLY_STEP`` when it is given. A state the table does
    not name fails closed."""
    state = str((r.current_state or {}).get("realization_state"))
    if (state, None) not in NEXT_ACTIONS or state in RUN_STATUSES or state == NO_LINEAGE:
        raise ValueError(f"no next action for realization state {state!r}")
    condition = _condition(r, state)
    if state == "NOT_APPLIED" and condition is None and next_step:
        return APPLY_STEP.format(**{k: "\u2014" if next_step.get(k) is None else next_step.get(k)
                                    for k in ("step", "knob", "from_value", "to_value")})
    return NEXT_ACTIONS[(state, condition)]


def _run_next_action(run: WarehouseRun, lineage: Optional[rrc.RealizationPackage]) -> str:
    if run.status != RUN_STATUS_OK:
        return NEXT_ACTIONS[(run.status, None)]
    if lineage is None:
        return NEXT_ACTIONS[(NO_LINEAGE, None)]
    return next_action(lineage, run.next_steps.get(str(_pkg(lineage))))


def _pkg(r: rrc.RealizationPackage) -> Optional[str]:
    return (r.source_recommendation or {}).get("recommendation_package_id")


def select_lineage(run: WarehouseRun) -> Tuple[Optional[rrc.RealizationPackage], Optional[str]]:
    """The lineage the Warehouses row shows, and why.

    The lineage that claims the warehouse's latest applied change: from the state index's ``applied_knob_owners`` when
    given (the owner whose change took effect last), else, from the realizations, the CLAIMABLE lineage whose application
    took effect last. Otherwise the newest package that is not superseded (latest generated). Otherwise none. Ties go to the
    highest package id, so the choice never depends on input order. Nothing is decided here: the owners, claim statuses and
    dispositions are the Realizer's.
    """
    by_rpkg = {r.realization_package_id: r for r in run.realizations}
    owners = [o for o in ((run.state_index or {}).get("applied_knob_owners") or []) if o.get("realization_package_id") in by_rpkg]
    if owners:
        latest = max(owners, key=lambda o: (parse_utc(o["effective_from_utc"]), str(o.get("recommendation_package_id"))))
        return by_rpkg[latest["realization_package_id"]], LINEAGE_BASIS_CLAIMS_LATEST_CHANGE
    claiming = [r for r in run.realizations
                if (r.attribution or {}).get("attribution_claim_status") == rrc.AttributionClaimStatus.CLAIMABLE.value
                and (r.application or {}).get("effective_from_utc")]
    if claiming:
        return max(claiming, key=lambda r: (parse_utc(r.application["effective_from_utc"]), str(_pkg(r)))), LINEAGE_BASIS_CLAIMS_LATEST_CHANGE
    active = [r for r in run.realizations
              if (r.source_recommendation or {}).get("disposition") != rrc.SourcePackageDisposition.SUPERSEDED.value]
    if active:
        return max(active, key=lambda r: (parse_utc(r.source_recommendation["generated_at_utc"]), str(_pkg(r)))), LINEAGE_BASIS_NEWEST_ACTIVE_PACKAGE
    return None, None


def _number(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _claimed_by(r: rrc.RealizationPackage) -> Optional[str]:
    """The owner's package id(s) when another lineage claims this lineage's change (``attribution.claimed_by``)."""
    owners = sorted({str(c.get("recommendation_package_id")) for c in (r.attribution or {}).get("claimed_by") or []})
    return ", ".join(owners) or None


def _to_date(r: rrc.RealizationPackage) -> Mapping[str, Any]:
    return (r.economics or {}).get("realized_to_date") or {}


def project_lineage(r: rrc.RealizationPackage, next_step: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A Lineages row: ``project_summary`` plus the owner that claims the change, the currency, realized to date with
    the days it covers, and the next action, in ``LINEAGE_COLUMNS`` order."""
    row = {
        **project_summary(r),
        "claimed_by_package_id": _claimed_by(r),
        "currency": (r.economics or {}).get("currency"),
        "realized_to_date": _money_float((r.economics or {}).get("realized_savings_since_application")),
        "realized_to_date_days_covered": _number(_to_date(r).get("days_covered")),
        "next_action": next_action(r, next_step),
    }
    return {c.key: row[c.key] for c in LINEAGE_COLUMNS}


def _lineage_order(r: rrc.RealizationPackage) -> Tuple[Any, ...]:
    generated = (r.source_recommendation or {}).get("generated_at_utc")
    return (parse_utc(generated) if generated else parse_utc("1970-01-01T00:00:00Z"), str(_pkg(r)), r.realization_package_id)


def project_warehouse(run: WarehouseRun) -> Dict[str, Any]:
    """A Warehouses row: the run's status and the lineage ``select_lineage`` shows (empty cells when there is none)."""
    lineage, basis = select_lineage(run)
    index_lineages = (run.state_index or {}).get("lineages")
    known = len(index_lineages) if run.state_index is not None and index_lineages is not None else len(run.realizations)
    owners = (run.state_index or {}).get("applied_knob_owners") if run.state_index is not None else None
    row: Dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "warehouse_id": run.warehouse_id,
        "warehouse_name": None,
        "recommendation_package_id": run.recommendation_package_id,
        "realization_package_id": None,
        "lineage_basis": basis,
        "run_status": run.status,
        "run_reason": run.reason,
        "other_lineages": max(known - (1 if lineage is not None else 0), 0),
        "next_action": _run_next_action(run, lineage),
        "as_of_utc": (run.state_index or {}).get("as_of_utc"),
        "applied_knob_owners_json": None if owners is None else _json(owners),
    }
    if lineage is not None:
        a, e, attr = lineage.application or {}, lineage.economics or {}, lineage.attribution or {}
        to_date = _to_date(lineage)
        row.update({
            "warehouse_name": (lineage.warehouse or {}).get("warehouse_name"),
            "recommendation_package_id": _pkg(lineage),
            "realization_package_id": lineage.realization_package_id,
            "applied_knob": a.get("knob"),
            "from_value": a.get("source_value"),
            "applied_value": a.get("observed_applied_value"),
            "recommended_value": a.get("recommended_value"),
            "applied_at_utc": a.get("effective_from_utc"),
            "applied_until_utc": a.get("effective_to_utc"),
            "implementation_conformance": a.get("implementation_conformance"),
            "validation_state": (lineage.validation or {}).get("state"),
            "realization_state": (lineage.current_state or {}).get("realization_state"),
            "maturity_state": (lineage.maturity or {}).get("state"),
            "currency": e.get("currency"),
            "realized_to_date": _money_float(e.get("realized_savings_since_application")),
            "days_covered": _number(to_date.get("days_covered")),
            "first_day": to_date.get("first_day"),
            "last_day": to_date.get("last_day"),
            "annualized_realized_savings": _money_float(e.get("annualized_realized_savings")),
            "expected_same_scope_annual_savings": _money_float(e.get("expected_same_scope_annual_savings")),
            "realization_ratio": _number(e.get("realization_ratio")),
            "attribution_claim_status": attr.get("attribution_claim_status"),
            "claimed_by_package_id": _claimed_by(lineage),
            "as_of_utc": lineage.as_of_utc,
            "application_json": _json(a),
            "economics_json": _json(e),
            "attribution_json": _json(attr),
        })
    return {c.key: row.get(c.key) for c in WAREHOUSE_COLUMNS}


@dataclass(frozen=True)
class RealizationWorkbookProjection:
    """The rows of the three sheets, each in its sheet's column order, and the latest time the inputs were observed
    (``as_of_utc``, None when nothing was): the renderer stamps the file with it, so identical inputs give identical files'
    content."""
    warehouses: List[Dict[str, Any]]
    lineages: List[Dict[str, Any]]
    timeline: List[Dict[str, Any]]
    as_of_utc: Optional[str] = None

    def validate(self, warehouse_runs: Sequence[WarehouseRun]) -> None:
        if len(self.warehouses) != len(warehouse_runs):
            raise ValueError("one Warehouses row required per warehouse run")
        if len(self.lineages) != sum(len(run.realizations) for run in warehouse_runs):
            raise ValueError("one Lineages row required per realization package")
        for sheet, rows in ((SHEET_WAREHOUSES, self.warehouses), (SHEET_LINEAGES, self.lineages), (SHEET_TIMELINE, self.timeline)):
            columns = [c.key for c in SHEET_COLUMNS[sheet][0]]
            if any(list(row) != columns for row in rows):
                raise ValueError(f"{sheet} rows must hold exactly the contract columns, in order")


def project_realization_workbook(warehouse_runs: Sequence[WarehouseRun]) -> RealizationWorkbookProjection:
    """The realization workbook's rows: warehouses sorted by workspace and warehouse; within each, its lineages oldest
    package first (as the state index orders them) and every ledger event of each lineage in ledger order."""
    runs = sorted(warehouse_runs, key=lambda run: (str(run.workspace_id), str(run.warehouse_id)))
    identities = [(run.workspace_id, run.warehouse_id) for run in runs]
    if len(set(identities)) != len(identities):
        raise ValueError("one warehouse run per (workspace_id, warehouse_id)")
    lineages: List[Dict[str, Any]] = []
    timeline: List[Dict[str, Any]] = []
    times: List[str] = []
    for run in runs:
        for r in sorted(run.realizations, key=_lineage_order):
            lineages.append(project_lineage(r, run.next_steps.get(str(_pkg(r)))))
            timeline.extend(project_timeline(run.ledgers.get(r.realization_package_id, ())))
            times.append(r.as_of_utc)
        if run.state_index and run.state_index.get("as_of_utc"):
            times.append(run.state_index["as_of_utc"])
    projection = RealizationWorkbookProjection(
        warehouses=[project_warehouse(run) for run in runs],
        lineages=lineages,
        timeline=timeline,
        as_of_utc=max(times, key=parse_utc) if times else None,
    )
    projection.validate(runs)
    return projection
