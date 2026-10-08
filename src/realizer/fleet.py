"""Fleet run: realize every warehouse of one Publisher run, one warehouse at a time (the facade's ``realize_fleet``).

This module only sequences: per warehouse of the Publisher run (in workspace, warehouse order) it takes the warehouse's
lock in the state (``packager.warehouse_lock``; a held lock refuses that warehouse), reads the run's package, asks the
Packager which lineages kept in the state are open (``packager.open_lineages``: not superseded, or the owner of an
applied knob; oldest package first), observes each of them and the run's package from the chosen source (live system
tables, the demo's simulated world, or each package's newest measurement evidence in a folder) and realizes each with
``realize_request`` (the run's package once, also when it is one of the open lineages). Every decision is the
capabilities'. A ``RealizerError`` refuses that warehouse (status REFUSED, its message the reason) and any other
exception crashes it (CRASHED, the exception type only, logged by type only); the other warehouses go on. In a live
run the warehouse the collector statements run on is not measured (SKIPPED, with a reason, logged at info; it changes
no exit status). Then the run
manifest and the realization workbook are written into the run folder.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from realizer.common.errors import InputError, RealizerError
from realizer.common.logging import LOGGER
from realizer.common.utils import same_warehouse_id, utc_iso
from realizer.config.loader import load_package, validate_package
from realizer.contracts.approvals import warn_unmatched
from realizer.config.output_layout import RUN_MANIFEST_NAME, WORKBOOK_NAME
from realizer.contracts.fleet import SOURCE_DEMO, SOURCE_EVIDENCE, SOURCE_LIVE, FleetReport, FleetRequest, PublisherRunEntry
from realizer.contracts.report import RUN_STATUS_CRASHED, RUN_STATUS_OK, RUN_STATUS_REFUSED, RUN_STATUS_SKIPPED, WarehouseRun
from realizer.contracts.request import RealizationRequest
from realizer.packager import packager
from realizer.realizer import measure_demo, measure_live, observe_from_evidence, realize_request
from realizer.analyzer.analysis import next_plan_step
from realizer.analyzer.collector.collector import LiveSession, newest_measurement
from realizer.reporter.run_manifest import build_run_manifest, write_run_manifest
from realizer.reporter.workbook import write_realization_workbook


def observe(request: FleetRequest, package: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The observation of *package* from the run's source; a live or demo measurement is written to the state first."""
    if request.source == SOURCE_LIVE:
        live = request.live
        return measure_live(package, request.state_dir, profile=live.profile, sql_warehouse_id=live.sql_warehouse_id,
                            config_file=live.config_file, source_region=live.source_region, aws_price_registry=live.aws_price_registry,
                            as_of=request.as_of, ingestion_cutoff=live.ingestion_cutoff, approvals=request.approvals, session=live.session)
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


COLLECTOR_SKIP_REASON = "the collector warehouse cannot measure itself"


def is_collector_warehouse(request: FleetRequest, entry: PublisherRunEntry) -> bool:
    """Whether *entry* is the warehouse a live run's collector statements run on: its own statements would keep it running
    and change the idle time and cost they measure, so it is not measured (a single-package run refuses instead)."""
    return request.source == SOURCE_LIVE and request.live is not None and same_warehouse_id(entry.warehouse_id, request.live.sql_warehouse_id)


PROJECTION_DIRS = ("realization/packages/", "realization/snapshots/")


def _in_run(run_dir: Path, paths: List[Path]) -> List[str]:
    """The projections this run wrote (realization packages and snapshots), relative to the run folder, once each. In
    the single-folder layout the state shares the run folder; its history files are never listed."""
    relative = set()
    for path in paths:
        try:
            name = Path(path).relative_to(run_dir).as_posix()
        except ValueError:
            continue
        if name.startswith(PROJECTION_DIRS):
            relative.add(name)
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


def realize_all(request: FleetRequest) -> Tuple[List[WarehouseRun], Dict[Tuple[str, str], List[str]]]:
    """Every warehouse of the request's Publisher run, one at a time, each failure isolated to its warehouse: the
    ``WarehouseRun`` of each (in run order) and the projections each wrote under the run folder."""
    runs: List[WarehouseRun] = []
    files: Dict[Tuple[str, str], List[str]] = {}
    for entry in request.publisher_run.entries:
        started = time.monotonic()
        written: List[Path] = []
        status, reason = RUN_STATUS_OK, None
        try:
            if is_collector_warehouse(request, entry):
                status, reason = RUN_STATUS_SKIPPED, COLLECTOR_SKIP_REASON
                LOGGER.info("fleet.warehouse_skipped workspace=%s warehouse=%s reason=%s", entry.workspace_id, entry.warehouse_id, reason)
            else:
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
        LOGGER.info("fleet.warehouse workspace=%s warehouse=%s status=%s realizations=%d elapsed_ms=%d", entry.workspace_id, entry.warehouse_id,
                    status, len(run.realizations), int((time.monotonic() - started) * 1000))
    return runs, files


def run_fleet(request: FleetRequest) -> FleetReport:
    """Realize every warehouse of the request's Publisher run, then write the run manifest and the realization workbook
    into the run folder; return what was written. A failure is isolated to its warehouse. A live run keeps one connection
    for all of its warehouses and reads the account-wide statements once (``LiveSession``)."""
    LOGGER.info("fleet.start source=%s warehouses=%d manifest_packages=%d", request.source, len(request.publisher_run.entries),
                request.publisher_run.package_count)
    fleet_started = time.monotonic()
    if request.source == SOURCE_LIVE and request.as_of is None:
        request = dataclasses.replace(request, as_of=utc_iso(dt.datetime.now(dt.timezone.utc).replace(microsecond=0)))  # one measurement time for the run
    with contextlib.ExitStack() as stack:
        if request.source == SOURCE_LIVE and request.live is not None:
            live = request.live
            session = stack.enter_context(LiveSession(profile=live.profile, sql_warehouse_id=live.sql_warehouse_id, config_file=live.config_file))
            request = dataclasses.replace(request, live=dataclasses.replace(live, session=session))
        runs, files = realize_all(request)
    warn_unmatched(request.approvals)
    workbook = write_realization_workbook(request.run_dir / WORKBOOK_NAME, runs)
    LOGGER.info("workbook.written path=%s", workbook)
    manifest = write_run_manifest(request.run_dir / RUN_MANIFEST_NAME, build_run_manifest(request, runs, files, WORKBOOK_NAME))
    LOGGER.info("fleet.done ok=%d refused=%d crashed=%d skipped=%d elapsed_ms=%d manifest=%s", *(sum(1 for r in runs if r.status == s)
                for s in (RUN_STATUS_OK, RUN_STATUS_REFUSED, RUN_STATUS_CRASHED, RUN_STATUS_SKIPPED)), int((time.monotonic() - fleet_started) * 1000), manifest)
    return FleetReport(warehouse_runs=tuple(runs), manifest_path=manifest, workbook_path=workbook)
