"""Realization ledger: where a package's ledger lives, loading it, building and appending its entries, and the ordered
commit of one run's events.

Moved from the single-file Realizer. The path layout, loading and the latest-event lookup are the read side (the
Analyzer reads prior history through them); ``make_ledger_entry``, ``append_if_absent`` (which reloads the file for
every append, so a repeated event is suppressed, and logs ``ledger.append`` and ``ledger.duplicate_suppressed``) and
``ledger_checkpoint`` are the write side. ``commit_events`` records what the capabilities decided, in the order the
single-file Realizer recorded it: the evidence file (REVID), drift, the application events, intent, rollback,
supersession, then either the terminal regeneration or the validation episode, the counterfactual, the measurement, its
billing correction and the downstream regeneration. The capabilities hand over facts, never events; the order lives
here, and a failure a capability kept (``halt``) is raised after the events decided before it, so a failed run leaves
the ledger it always left.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.common.errors import LedgerError
from realizer.common.logging import LOGGER
from realizer.common.utils import utc_iso, write_json
from realizer.config.settings import POLICY_VERSION, PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts import realization as rc
from realizer.contracts.realization import MeasurementFacts, ReviewResult
from realizer.contracts.realization_schema import validate_ledger_entry
from realizer.contracts.reconciliation import RegenerationRequest
from realizer.contracts.request import RunContext


def ledger_path(output_dir: Path, rpkg_id: str) -> Path:
    return output_dir / "realization" / "ledger" / f"{rpkg_id}.jsonl"


def load_ledger(path: Path) -> List[rc.RealizationLedgerEntry]:
    if not path.exists():
        return []
    entries: List[rc.RealizationLedgerEntry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"ledger {path.name} cannot be read ({type(exc).__name__})") from exc
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            raw["event_type"] = rc.EventType(raw["event_type"])
            entry = rc.RealizationLedgerEntry(**raw)
            entry.validate()
        except (ValueError, TypeError, KeyError) as exc:  # json.JSONDecodeError is a ValueError
            raise LedgerError(f"ledger {path.name} line {line_no} is not a valid ledger entry ({type(exc).__name__})") from exc
        entries.append(entry)
    # recorded time governs projection order; RLE id is deterministic tie-break.
    return sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))


def latest_event(entries: Sequence[rc.RealizationLedgerEntry], *types: rc.EventType) -> Optional[rc.RealizationLedgerEntry]:
    allowed = {x.value for x in types}
    xs = [e for e in entries if e.event_type.value in allowed]
    return xs[-1] if xs else None


# ---------------------------------------------------------------------------
# Building and appending entries
# ---------------------------------------------------------------------------


def ledger_business_key(event_type: rc.EventType, payload: Mapping[str, Any]) -> Dict[str, Any]:
    keys = [
        "application_episode_id", "validation_episode_id", "measurement_id", "scenario_id", "step",
        "effective_from_utc", "window_start_utc", "window_end_utc", "counterfactual_id",
        "regeneration_request_id", "replacement_package_id", "correction_key",
    ]
    return {k: payload.get(k) for k in keys if payload.get(k) is not None}


def make_ledger_entry(
    *, package: Mapping[str, Any], rpkg_id: str, event_type: rc.EventType,
    effective_at_utc: str, recorded_at_utc: str, payload: Mapping[str, Any],
    evidence_refs: Sequence[str] = (), application_episode_id: Optional[str] = None,
    validation_episode_id: Optional[str] = None, measurement_id: Optional[str] = None,
    caused_by_entry_id: Optional[str] = None, corrects_entry_id: Optional[str] = None,
) -> rc.RealizationLedgerEntry:
    payload_dict = copy.deepcopy(dict(payload))
    business_key = ledger_business_key(event_type, {
        **payload_dict,
        "application_episode_id": application_episode_id,
        "validation_episode_id": validation_episode_id,
        "measurement_id": measurement_id,
    })
    eid = rc.content_id("RLE", {
        "kind": "REALIZATION_LEDGER_ENTRY",
        "ledger_major": rc.LEDGER_CONTRACT_VERSION.split(".")[0],
        "rpkg": rpkg_id,
        "event_type": event_type.value,
        "domain_business_key": business_key,
        "payload": payload_dict,
    })
    w = package["warehouse"]
    entry = rc.RealizationLedgerEntry(
        ledger_entry_id=eid,
        ledger_contract_version=rc.LEDGER_CONTRACT_VERSION,
        realization_package_id=rpkg_id,
        recommendation_package_id=package["recommendation_package_id"],
        event_type=event_type,
        effective_at_utc=utc_iso(effective_at_utc),
        recorded_at_utc=utc_iso(recorded_at_utc),
        workspace_id=str(w["workspace_id"]),
        warehouse_id=str(w["warehouse_id"]),
        application_episode_id=application_episode_id,
        validation_episode_id=validation_episode_id,
        measurement_id=measurement_id,
        payload=payload_dict,
        evidence_refs=list(evidence_refs),
        caused_by_entry_id=caused_by_entry_id,
        corrects_entry_id=corrects_entry_id,
        provenance={"product_version": PRODUCT_VERSION, "realizer_version": REALIZER_VERSION, "policy_version": POLICY_VERSION},
    )
    entry.validate()
    validate_ledger_entry(entry.to_dict())
    return entry


def append_if_absent(path: Path, entry: rc.RealizationLedgerEntry) -> bool:
    existing = {x.ledger_entry_id for x in load_ledger(path)}
    if entry.ledger_entry_id in existing:
        LOGGER.info("ledger.duplicate_suppressed rle=%s", entry.ledger_entry_id)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(rc.canonical_json(entry.to_dict()) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    LOGGER.info("ledger.append event=%s rle=%s", entry.event_type.value, entry.ledger_entry_id)
    return True


def ledger_checkpoint(entries: Sequence[rc.RealizationLedgerEntry]) -> Dict[str, Any]:
    ordered = sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))
    ids = [e.ledger_entry_id for e in ordered]
    digest = "SHA256:" + rc.sha256_hex({"ordered_entry_ids": ids})
    return {
        "entry_count": len(ordered),
        "first_entry_id": ids[0] if ids else None,
        "latest_entry_id": ids[-1] if ids else None,
        "latest_recorded_at_utc": ordered[-1].recorded_at_utc if ordered else None,
        "latest_effective_at_utc": max((e.effective_at_utc for e in ordered), default=None),
        "ledger_checkpoint_hash": digest,
    }

# ---------------------------------------------------------------------------
# Event payloads (assembled from the capabilities' facts, in the single-file Realizer's key order)
# ---------------------------------------------------------------------------


def replacement_payload(replacement_package_id: Any) -> Dict[str, Any]:
    """The SOURCE_PACKAGE_SUPERSEDED and REPLACEMENT_PACKAGE_LINKED payload (the two events carry the same one)."""
    return {"replacement_package_id": replacement_package_id}


def measurement_payload(measurement: MeasurementFacts, recognition_status: Optional[str]) -> Dict[str, Any]:
    """The ECONOMIC_MEASUREMENT_RECORDED payload: the Estimator's measurement and the Reviewer's recognition status."""
    return {
        "measurement_id": measurement.measurement_id,
        "window_start_utc": measurement.window_start_utc,
        "window_end_utc": measurement.window_end_utc,
        "counterfactual_id": measurement.counterfactual_id,
        "counterfactual_method": measurement.counterfactual_method,
        "recognition_status": recognition_status,
        "economics": measurement.economics,
        "attribution": measurement.attribution,
        "realization_state": measurement.realization_state,
        "mature_ttm_realized_savings": measurement.mature_ttm_realized_savings,
        "correction_status": measurement.correction_status,
    }


# ---------------------------------------------------------------------------
# Files the Packager writes next to the ledger
# ---------------------------------------------------------------------------


def evidence_path(output_dir: Path, evidence_id: str) -> Path:
    return output_dir / "realization" / "evidence" / f"{evidence_id}.json"


def regeneration_path(output_dir: Path, regeneration_request_id: str) -> Path:
    return output_dir / "realization" / "regeneration" / f"{regeneration_request_id}.json"


# ---------------------------------------------------------------------------
# Recording a run
# ---------------------------------------------------------------------------


def record_creation(context: RunContext) -> None:
    """Record the lineage (REALIZATION_CREATED) before the run reads anything else; the inputs were checked before it, and
    a corrupt ledger raises here, before anything is written."""
    package, rpkg = context.request.package, context.realization_package_id
    lpath = ledger_path(context.request.output_dir, rpkg)
    load_ledger(lpath)  # a corrupt ledger fails here, before anything is built or written

    # Stable lineage creation uses Publisher generation time so replay does not
    # depend on the first Realizer wall-clock run.
    generated = (package.get("provenance") or {}).get("generated_at") or ((package.get("warehouse") or {}).get("current_epoch") or {}).get("start_time")
    if not generated:
        generated = "1970-01-01T00:00:00Z"
    created = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.REALIZATION_CREATED,
        effective_at_utc=generated, recorded_at_utc=generated,
        payload={"recommendation_package_id": package["recommendation_package_id"]},
    )
    append_if_absent(lpath, created)


def append_regeneration(package: Mapping[str, Any], output_dir: Path, request: RegenerationRequest, recorded: str, effective: str) -> None:
    """Record a regeneration request (REGENERATION_REQUESTED on its application episode), then write its RERUN file."""
    rpkg = rc.make_rpkg_id(package["recommendation_package_id"])
    event = make_ledger_entry(
        package=package, rpkg_id=rpkg, event_type=rc.EventType.REGENERATION_REQUESTED,
        effective_at_utc=effective, recorded_at_utc=recorded, payload=request.payload,
        application_episode_id=request.application_episode_id,
    )
    append_if_absent(ledger_path(output_dir, rpkg), event)
    write_json(regeneration_path(output_dir, request.regeneration_request_id), request.payload)


def commit_events(context: RunContext, reviewed: ReviewResult) -> Tuple[Path, ...]:
    """Record everything the run decided, in the single-file Realizer's order; return the files written besides the ledger.

    Nothing for a lineage-only run (the lineage entry is recorded by ``record_creation``). Otherwise: the evidence
    file (REVID), then the lifecycle events the Reconciler decided (drift, the application events, intent, rollback
    requested and observed, supersession and the replacement link). A reconciliation failure (``halt``) is raised
    after them. Then either the terminal regeneration (and its RERUN file), which ends the run, or, when validation is
    due, the validation episode (ready, started, completed), the counterfactual, the measurement and its billing
    correction, each present only when decided, then the downstream regeneration. The review's failure is raised
    after the episode payloads present, the estimate's after the facts present, and the downstream regeneration's
    remeasurement windows are checked last. Every entry is built immediately before its own append, so a malformed
    value fails at the position it always failed.
    """
    package, output_dir = context.request.package, context.request.output_dir
    analysis, reconciliation, estimate = reviewed.analysis, reviewed.reconciliation, reviewed.estimate
    observed = analysis.observation
    if observed is None:
        return ()
    rpkg = context.realization_package_id
    lpath = ledger_path(output_dir, rpkg)
    obs_time, recorded, effective = observed.observed_at_utc, observed.recorded_at_utc, observed.effective_at_utc
    revid = observed.evidence_id
    written: List[Path] = []

    def record(event_type: rc.EventType, effective_at: str, payload: Mapping[str, Any], evidence_refs: Sequence[str], **ids: Any) -> None:
        append_if_absent(lpath, make_ledger_entry(
            package=package, rpkg_id=rpkg, event_type=event_type, effective_at_utc=effective_at, recorded_at_utc=recorded,
            payload=payload, evidence_refs=evidence_refs, **ids,
        ))

    evidence_file = evidence_path(output_dir, revid)
    write_json(evidence_file, observed.evidence_record)
    written.append(evidence_file)

    # Lifecycle: drift, the application events, intent, rollback, supersession.
    if reconciliation.drift is not None:
        record(rc.EventType.DRIFT_DETECTED, effective, reconciliation.drift, [revid], application_episode_id=reconciliation.drift_episode_id)
    for planned in reconciliation.applications:
        record(planned.event_type, effective, planned.payload, [revid], application_episode_id=planned.application_episode_id)
    if reconciliation.intent is not None:
        record(rc.EventType.INTENT_RECORDED, effective, reconciliation.intent, [], application_episode_id=reconciliation.intent_episode_id)
    if reconciliation.rollback_requested is not None:
        record(rc.EventType.ROLLBACK_REQUESTED, effective, reconciliation.rollback_requested, [revid], application_episode_id=reconciliation.rollback_episode_id)
    if reconciliation.rollback_observed is not None:
        record(rc.EventType.ROLLBACK_OBSERVED, effective, reconciliation.rollback_observed, [revid], application_episode_id=reconciliation.rollback_episode_id)
    if reconciliation.replacement_package_id:
        record(rc.EventType.SOURCE_PACKAGE_SUPERSEDED, effective, replacement_payload(reconciliation.replacement_package_id), [])
        record(rc.EventType.REPLACEMENT_PACKAGE_LINKED, effective, replacement_payload(reconciliation.replacement_package_id), [])
    if reconciliation.halt is not None:
        raise reconciliation.halt

    if reconciliation.terminal_regeneration is not None:
        append_regeneration(package, output_dir, reconciliation.terminal_regeneration, recorded, effective)
        written.append(regeneration_path(output_dir, reconciliation.terminal_regeneration.regeneration_request_id))
        return tuple(written)
    if not reconciliation.validation_due:
        return tuple(written)

    # Validation is due. The episode is recorded first (ready, started, completed), then the counterfactual, the
    # measurement and its billing correction. A stage that failed is raised after the facts it decided before the
    # failure, the episode's before the estimate's.
    active = reconciliation.active
    assert active is not None  # validation is due only for an active application
    review = reviewed  # the validation episode and the recognition status are the review's
    episode = review.validation
    app_id = active.application_episode_id
    for event_type, payload in (
        (rc.EventType.VALIDATION_READY, episode.ready_payload if episode else None),
        (rc.EventType.VALIDATION_STARTED, episode.started_payload if episode else None),
        (rc.EventType.VALIDATION_COMPLETED, episode.completed_payload if episode else None),
    ):
        if payload is None:
            break
        record(event_type, obs_time, payload, [revid], application_episode_id=app_id, validation_episode_id=episode.validation_episode_id)
    if review.halt is not None:
        raise review.halt
    assert episode is not None  # a review that did not halt has its episode
    val_id = episode.validation_episode_id

    counterfactual = estimate.counterfactual
    if counterfactual is not None and counterfactual.payload is not None:
        record(rc.EventType.COUNTERFACTUAL_RECORDED, obs_time, counterfactual.payload, [revid], application_episode_id=app_id, validation_episode_id=val_id)
    measurement = estimate.measurement
    if measurement is not None:
        record(
            rc.EventType.ECONOMIC_MEASUREMENT_RECORDED, obs_time, measurement_payload(measurement, review.recognition_status), [revid],
            application_episode_id=app_id, validation_episode_id=val_id, measurement_id=measurement.measurement_id,
        )
        correction = estimate.correction
        if correction is not None:
            record(
                rc.EventType.BILLING_CORRECTION_RECORDED, obs_time, correction.payload, [revid],
                application_episode_id=app_id, validation_episode_id=val_id, measurement_id=measurement.measurement_id,
                corrects_entry_id=correction.corrects_entry_id,
            )
    if estimate.halt is not None:
        raise estimate.halt

    # Invalidating or modified authoritative changes require fresh Publisher
    # analysis once the governed remeasurement/readiness window has passed.
    upstream = reconciliation.upstream_regeneration.get() if reconciliation.upstream_regeneration is not None else None
    if upstream is not None:
        append_regeneration(package, output_dir, upstream, recorded, effective)
        written.append(regeneration_path(output_dir, upstream.regeneration_request_id))
    return tuple(written)
