"""Packager across lineages: the source package copy, the cross-lineage events, the policy snapshots, the other
lineages' re-projected snapshots and the warehouse's state index.

* ``keep_source`` writes the copy of the source package a lineage is created from
  (``realization/sources/<workspace>__<warehouse>/<PKG>.json``). The Publisher's package is a public contract artifact
  and the Realizer never changes it; the copy is how a later run of another package of the warehouse finds this lineage
  and re-projects its snapshot. It is checked like any package when it is read back.
* ``record_events`` appends each cross-lineage event the Reconciler decided to its lineage's ledger: the run's own
  (whose ledger the run already writes, under its lock) and the others, each under that ledger's advisory lock
  (``ledger.ledger_lock``) for the append. An event
  already recorded is suppressed by its id, so a run that learns nothing new appends nothing.
* ``keep_policy`` writes the settings of the policy a validation or a PolicyDiff was recorded under
  (``realization/policies/<PSNAP>.json``), so a later PolicyDiff can name the settings that changed.
* ``state_index`` is the per-warehouse view of every lineage's snapshot (``realization/state/<workspace>__<warehouse>.json``):
  rebuilt from the ledgers on every run and never read back, so it is never a second source of truth.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from realizer.analyzer.lineages import policies_dir, source_path, warehouse_key
from realizer.common.logging import LOGGER
from realizer.common.utils import write_json
from realizer.contracts import realization as rc
from realizer.contracts.lineage import LineageEvent, LineageRecord
from realizer.governor.policy import policy_to_dict
from realizer.common.errors import LedgerError
from realizer.packager.ledger import append_if_absent, ledger_lock, ledger_path, load_ledger, make_ledger_entry
from realizer.packager.snapshot import project_snapshot

STATE_INDEX_VERSION = "1.0.0"


def keep_source(output_dir: Path, package: Mapping[str, Any]) -> Path:
    """Write the copy of *package* its lineage was created from (unchanged content); return its path."""
    path = source_path(output_dir, package)
    write_json(path, package)
    return path


def keep_policy(output_dir: Path, policy: Any) -> Path:
    """Write the settings of *policy* under its snapshot id; return the path."""
    path = policies_dir(output_dir) / f"{policy.snapshot_id}.json"
    write_json(path, {"policy_snapshot_id": policy.snapshot_id, "policy": policy_to_dict(policy)})
    return path


def record_events(output_dir: Path, own_rpkg: str, own_package: Mapping[str, Any], events: Sequence[LineageEvent],
                  others: Sequence[LineageRecord]) -> Set[str]:
    """Append each event to its lineage's ledger; return the realization package ids whose ledgers grew.

    The run's own events go to its ledger directly; another lineage's under that ledger's lock (an event whose ledger
    stays locked is left for a later run and logged ``realizer.lineage_locked``)."""
    packages = {own_rpkg: own_package, **{r.realization_package_id: r.package for r in others}}
    grown: Set[str] = set()
    for event in events:
        package = packages[event.realization_package_id]
        entry = make_ledger_entry(
            package=package, rpkg_id=event.realization_package_id, event_type=event.event_type,
            effective_at_utc=event.effective_at_utc, recorded_at_utc=event.recorded_at_utc, payload=event.payload,
            application_episode_id=event.application_episode_id,
        )
        path = ledger_path(output_dir, event.realization_package_id)
        if event.realization_package_id == own_rpkg:
            appended = append_if_absent(path, entry)
        else:
            try:
                with ledger_lock(path):
                    appended = append_if_absent(path, entry)
            except LedgerError:
                # Another run holds that ledger: every run decides the warehouse's cross-lineage events afresh, so the
                # event is left for a later run.
                LOGGER.warning("realizer.lineage_locked rpkg=%s event=%s", event.realization_package_id, event.event_type.value)
                continue
        if appended:
            grown.add(event.realization_package_id)
    return grown


def _lineage_row(snapshot: rc.RealizationPackage) -> Dict[str, Any]:
    source, app, attr = snapshot.source_recommendation, snapshot.application, snapshot.attribution
    return {
        "realization_package_id": snapshot.realization_package_id,
        "recommendation_package_id": source.get("recommendation_package_id"),
        "snapshot_id": snapshot.snapshot_id,
        "epoch_id": source.get("epoch_id"),
        "previous_epoch_id": (source.get("observed_change") or {}).get("previous_epoch_id"),
        "generated_at_utc": source.get("generated_at_utc"),
        "cost_profile": source.get("cost_profile"),
        "disposition": source.get("disposition"),
        "superseded_by_package_id": source.get("superseded_by_package_id"),
        "successor_package_id": source.get("successor_package_id"),
        "application_state": app.get("state"),
        "knob": app.get("knob"),
        "observed_applied_value": app.get("observed_applied_value"),
        "effective_from_utc": app.get("effective_from_utc"),
        "realization_state": snapshot.current_state.get("realization_state"),
        "attribution_claim_status": attr.get("attribution_claim_status"),
        "recommendation_attributed_realized_savings": attr.get("recommendation_attributed_realized_savings"),
        "claimed_by": attr.get("claimed_by") or [],
        "as_of_utc": snapshot.as_of_utc,
    }


def state_index(package: Mapping[str, Any], snapshots: Sequence[rc.RealizationPackage]) -> Dict[str, Any]:
    """The warehouse's state index: one row per lineage (oldest package first), the packages still active (not
    superseded) and, per applied knob, the lineage that claims it."""
    rows = sorted((_lineage_row(s) for s in snapshots), key=lambda r: (str(r["generated_at_utc"]), str(r["recommendation_package_id"])))
    owners = sorted(
        ({"knob": r["knob"], "observed_value": r["observed_applied_value"], "effective_from_utc": r["effective_from_utc"],
          "realization_package_id": r["realization_package_id"], "recommendation_package_id": r["recommendation_package_id"]}
         for r in rows if r["application_state"] == rc.ApplicationState.APPLIED.value and r["knob"] and not r["claimed_by"]),
        key=lambda o: (str(o["knob"]), str(o["effective_from_utc"]), str(o["recommendation_package_id"])),
    )
    warehouse = package["warehouse"]
    return {
        "state_index_version": STATE_INDEX_VERSION,
        "workspace_id": warehouse["workspace_id"],
        "warehouse_id": warehouse["warehouse_id"],
        "as_of_utc": max((r["as_of_utc"] for r in rows), default=None),
        "active_package_ids": [r["recommendation_package_id"] for r in rows if r["disposition"] == rc.SourcePackageDisposition.ACTIVE.value],
        "applied_knob_owners": owners,
        "lineages": rows,
    }


def state_index_path(output_dir: Path, package: Mapping[str, Any]) -> Path:
    return output_dir / "realization" / "state" / f"{warehouse_key(package)}.json"


def project_others(output_dir: Path, others: Sequence[LineageRecord], grown: Set[str]) -> Tuple[Tuple[rc.RealizationPackage, ...], List[rc.RealizationPackage]]:
    """The other lineages' snapshots: ``(re-projected, all)``, the re-projected ones those whose ledgers grew. A lineage
    whose ledger holds nothing yet (its run failed before it was created) is left out."""
    changed: List[rc.RealizationPackage] = []
    every: List[rc.RealizationPackage] = []
    for record in others:
        entries = load_ledger(ledger_path(output_dir, record.realization_package_id))
        if not entries:
            continue
        snapshot = project_snapshot(record.package, entries)
        every.append(snapshot)
        if record.realization_package_id in grown:
            changed.append(snapshot)
    return tuple(changed), every


def recorded_policy(events: Sequence[LineageEvent], validation_recorded: bool) -> bool:
    """Whether the run recorded anything under its policy (a validation or a PolicyDiff), so its settings are kept."""
    return validation_recorded or any(e.event_type == rc.EventType.POLICY_DIFF_RECORDED for e in events)


__all__ = ["STATE_INDEX_VERSION", "keep_policy", "keep_source", "project_others", "record_events",
           "recorded_policy", "state_index", "state_index_path"]
