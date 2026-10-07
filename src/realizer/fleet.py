"""Fleet run: realize every warehouse of one Publisher run, one warehouse at a time (the facade's ``realize_fleet``).

This module only sequences: per warehouse of the Publisher run (in workspace, warehouse order) it takes the warehouse's
lock in the state (``packager.warehouse_lock``; a held lock refuses that warehouse), reads the run's package, asks the
Packager which lineages kept in the state are open (``packager.open_lineages``: not superseded, or the owner of an
applied knob; oldest package first), observes each of them and the run's package from the chosen source (live system
tables, the demo's simulated world, or each package's newest measurement evidence in a folder) and realizes each with
``realize_request`` (the run's package once, also when it is one of the open lineages). Every decision is the
capabilities'. A ``RealizerError`` refuses that warehouse (status REFUSED, its message the reason) and any other
exception crashes it (CRASHED, the exception type only, logged by type only); the other warehouses go on. Then the run
manifest and the realization workbook are written into the run folder.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from realizer.common.errors import InputError, RealizerError
from realizer.common.logging import LOGGER
from realizer.config.loader import load_package, validate_package
from realizer.config.output_layout import RUN_MANIFEST_NAME, WORKBOOK_NAME
from realizer.contracts.fleet import SOURCE_DEMO, SOURCE_EVIDENCE, SOURCE_LIVE, FleetReport, FleetRequest, PublisherRunEntry
from realizer.contracts.report import RUN_STATUS_CRASHED, RUN_STATUS_OK, RUN_STATUS_REFUSED, WarehouseRun
from realizer.contracts.request import RealizationRequest
from realizer.packager import packager
from realizer.realizer import measure_demo, measure_live, observe_from_evidence, realize_request
from realizer.analyzer.analysis import next_plan_step
from realizer.analyzer.collector.collector import newest_measurement
from realizer.reporter.run_manifest import build_run_manifest, write_run_manifest
from realizer.reporter.workbook import write_realization_workbook


def observe(request: FleetRequest, package: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The observation of *package* from the run's source; a live or demo measurement is written to the state first."""
    if request.source == SOURCE_LIVE:
        live = request.live
        return measure_live(package, request.state_dir, profile=live.profile, sql_warehouse_id=live.sql_warehouse_id,
                            config_file=live.config_file, source_region=live.source_region, aws_price_registry=live.aws_price_registry,
                            as_of=request.as_of, ingestion_cutoff=live.ingestion_cutoff, approvals=request.approvals)
    if request.source == SOURCE_DEMO:
        return measure_demo(package, request.state_dir, as_of=request.as_of, approvals=request.approvals)
    if request.source == SOURCE_EVIDENCE:
        return observe_from_evidence(package, newest_measurement(request.evidence_dir, package), request.approvals)
    raise InputError(f"unknown fleet source {request.source}")


def run_package(request: FleetRequest, entry: PublisherRunEntry) -> Mapping[str, Any]:
    """The run's package of *entry*, checked like any package and against its manifest entry."""
    package = load_package(request.publisher_run.run_dir / entry.path)
    validate_package(package)
    warehouse = package["warehouse"]
    if (package["recommendation_package_id"], warehouse["workspace_id"], warehouse["warehouse_id"]) != (
            entry.recommendation_package_id, entry.workspace_id, entry.warehouse_id):
        raise InputError(f"package {entry.path} is not the package the Publisher run manifest names for {entry.workspace_id}/{entry.warehouse_id}")
    return package


def realize_warehouse(request: FleetRequest, entry: PublisherRunEntry) -> List[Path]:
    """Realize every open lineage of the warehouse kept in the state (oldest first), then the run's package, under the
    warehouse's lock; return the files written. Each lineage's observation is read before any lineage is realized."""
    with packager.warehouse_lock(request.state_dir, entry.workspace_id, entry.warehouse_id):
        package = run_package(request, entry)
        packages = [record.package for record in packager.open_lineages(request.state_dir, entry.workspace_id, entry.warehouse_id)]
        if entry.recommendation_package_id not in {p["recommendation_package_id"] for p in packages}:
            packages.append(package)
        observed = [(p, observe(request, p)) for p in packages]
        written: List[Path] = []
        for p, observation in observed:
            report = realize_request(RealizationRequest(package=p, observation=observation, output_dir=request.run_dir,
                                                        state_dir=request.state_dir, policy=request.policy))
            written.extend(report.files)
        return written


def _in_run(run_dir: Path, paths: List[Path]) -> List[str]:
    """The paths under the run folder, relative to it, once each."""
    relative = set()
    for path in paths:
        try:
            relative.add(Path(path).relative_to(run_dir).as_posix())
        except ValueError:
            continue
    return sorted(relative)


def _warehouse_run(request: FleetRequest, entry: PublisherRunEntry, status: str, reason: Optional[str], files: List[str]) -> WarehouseRun:
    rpkgs = [Path(f).stem for f in files if f.startswith("realization/packages/")]
    try:
        realizations, ledgers, index, packages = packager.warehouse_view(request.state_dir, entry.workspace_id, entry.warehouse_id, rpkgs)
    except Exception as exc:  # noqa: BLE001 - a state the view cannot read: the warehouse's outcome stands, without its rows
        LOGGER.warning("fleet.view_unreadable workspace=%s warehouse=%s error_type=%s", entry.workspace_id, entry.warehouse_id, type(exc).__name__)
        realizations, ledgers, index, packages = (), {}, None, {}
    steps = {pkg_id: step for pkg_id, step in ((pkg_id, next_plan_step(p)) for pkg_id, p in sorted(packages.items())) if step is not None}
    return WarehouseRun(workspace_id=entry.workspace_id, warehouse_id=entry.warehouse_id, status=status, reason=reason,
                        recommendation_package_id=entry.recommendation_package_id, realizations=realizations, ledgers=ledgers,
                        state_index=index, next_steps=steps)


def run_fleet(request: FleetRequest) -> FleetReport:
    """Realize every warehouse of the request's Publisher run, then write the run manifest and the realization workbook
    into the run folder; return what was written. A failure is isolated to its warehouse."""
    LOGGER.info("fleet.start source=%s warehouses=%d manifest_packages=%d", request.source, len(request.publisher_run.entries),
                request.publisher_run.package_count)
    runs: List[WarehouseRun] = []
    files: Dict[Tuple[str, str], List[str]] = {}
    for entry in request.publisher_run.entries:
        written: List[Path] = []
        status, reason = RUN_STATUS_OK, None
        try:
            written = realize_warehouse(request, entry)
        except RealizerError as exc:
            status, reason = RUN_STATUS_REFUSED, str(exc)
            LOGGER.warning("fleet.warehouse_refused workspace=%s warehouse=%s error_type=%s", entry.workspace_id, entry.warehouse_id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - isolated to the warehouse, recorded by type only (its message may carry data)
            status, reason = RUN_STATUS_CRASHED, type(exc).__name__
            LOGGER.error("fleet.warehouse_crashed workspace=%s warehouse=%s error_type=%s", entry.workspace_id, entry.warehouse_id, type(exc).__name__)
        in_run = _in_run(request.run_dir, written)
        files[(entry.workspace_id, entry.warehouse_id)] = in_run
        run = _warehouse_run(request, entry, status, reason, in_run)
        runs.append(run)
        LOGGER.info("fleet.warehouse workspace=%s warehouse=%s status=%s realizations=%d", entry.workspace_id, entry.warehouse_id,
                    status, len(run.realizations))
    workbook = write_realization_workbook(request.run_dir / WORKBOOK_NAME, runs)
    LOGGER.info("workbook.written path=%s", workbook)
    manifest = write_run_manifest(request.run_dir / RUN_MANIFEST_NAME, build_run_manifest(request, runs, files, WORKBOOK_NAME))
    LOGGER.info("fleet.done ok=%d refused=%d crashed=%d manifest=%s", *(sum(1 for r in runs if r.status == s)
                for s in (RUN_STATUS_OK, RUN_STATUS_REFUSED, RUN_STATUS_CRASHED)), manifest)
    return FleetReport(warehouse_runs=tuple(runs), manifest_path=manifest, workbook_path=workbook)
