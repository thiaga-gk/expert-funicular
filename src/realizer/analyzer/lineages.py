"""Analyzer read side of a warehouse's lineages: the other realization lineages of the run's warehouse in the output
directory, and the policy snapshots recorded there.

Each lineage is found through the copy of its source package the Realizer keeps when the lineage is created
(``realization/sources/<workspace>__<warehouse>/<PKG>.json``): the copy names the package, the package names its
realization package (RPKG-*) and so its ledger. A copy is checked like any package the Realizer reads
(``validate_package``: an edited copy is refused, never read), and its ledger is loaded as the run's own is (a corrupt
ledger fails the run). Policy snapshots (``realization/policies/<PSNAP>.json``) are the settings of each policy a
validation or a PolicyDiff was recorded under. Nothing here writes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from realizer.common.errors import InputError
from realizer.common.utils import load_json
from realizer.config.loader import validate_package
from realizer.contracts.lineage import LineageRecord, WarehouseHistory
from realizer.contracts.realization import make_rpkg_id
from realizer.packager.ledger import ledger_path, load_ledger


def safe_name(value: Any) -> str:
    """A filesystem-safe fragment of an id (the Publisher's package file rule): anything outside [A-Za-z0-9_.-] becomes
    ``_``, an empty value ``unknown``."""
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    return text or "unknown"


def warehouse_key(package: Mapping[str, Any]) -> str:
    """``<workspace>__<warehouse>``: the name of a warehouse's source folder and state index file."""
    warehouse = package["warehouse"]
    return f"{safe_name(warehouse['workspace_id'])}__{safe_name(warehouse['warehouse_id'])}"


def sources_dir(output_dir: Path, package: Mapping[str, Any]) -> Path:
    return output_dir / "realization" / "sources" / warehouse_key(package)


def source_path(output_dir: Path, package: Mapping[str, Any]) -> Path:
    """Where the copy of *package* is kept: one file per package, in its warehouse's folder."""
    return sources_dir(output_dir, package) / f"{safe_name(package['recommendation_package_id'])}.json"


def policies_dir(output_dir: Path) -> Path:
    return output_dir / "realization" / "policies"


def read_lineages(output_dir: Path, package: Mapping[str, Any]) -> Tuple[LineageRecord, ...]:
    """Every other lineage of *package*'s warehouse kept in *output_dir*, in package id order, with its ledger.

    A copy that is not the package it is named after (edited, or of another warehouse) raises ``InputError``; a corrupt
    ledger raises ``LedgerError``, as the run's own would."""
    folder = sources_dir(output_dir, package)
    if not folder.is_dir():
        return ()
    own = package["recommendation_package_id"]
    warehouse = (package["warehouse"]["workspace_id"], package["warehouse"]["warehouse_id"])
    records = []
    for path in sorted(folder.glob("PKG-*.json")):
        if path.stem == own:
            continue
        try:
            other = load_json(path)
            validate_package(other)
        except (OSError, ValueError) as exc:  # an unreadable copy, invalid JSON or a package the Realizer refuses
            raise InputError(f"source package copy {path.name} cannot be read as a package ({type(exc).__name__})") from exc
        pkg_id = other["recommendation_package_id"]
        if path.stem != pkg_id or (other["warehouse"]["workspace_id"], other["warehouse"]["warehouse_id"]) != warehouse:
            raise InputError(f"source package copy {path.name} is not the package of its name and warehouse")
        rpkg = make_rpkg_id(pkg_id)
        records.append(LineageRecord(realization_package_id=rpkg, recommendation_package_id=pkg_id, package=other,
                                     entries=tuple(load_ledger(ledger_path(output_dir, rpkg)))))
    return tuple(records)


def read_policy_snapshots(output_dir: Path) -> Dict[str, Mapping[str, Any]]:
    """The recorded policy snapshots: PSNAP id -> policy settings (empty when none was recorded)."""
    folder = policies_dir(output_dir)
    if not folder.is_dir():
        return {}
    snapshots = {}
    for path in sorted(folder.glob("PSNAP-*.json")):
        try:
            document = load_json(path)
        except (OSError, ValueError) as exc:
            raise InputError(f"policy snapshot {path.name} cannot be read ({type(exc).__name__})") from exc
        if not isinstance(document, Mapping) or document.get("policy_snapshot_id") != path.stem or not isinstance(document.get("policy"), Mapping):
            raise InputError(f"policy snapshot {path.name} is not a policy snapshot of its name")
        snapshots[path.stem] = document["policy"]
    return snapshots


def read_warehouse_history(output_dir: Path, package: Mapping[str, Any]) -> WarehouseHistory:
    """The other lineages of the package's warehouse and the recorded policy snapshots."""
    return WarehouseHistory(lineages=read_lineages(output_dir, package), policy_snapshots=read_policy_snapshots(output_dir))
