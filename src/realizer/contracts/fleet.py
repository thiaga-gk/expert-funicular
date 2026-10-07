"""Fleet run request: realize every warehouse of one Publisher run.

``PublisherRun`` is what the Realizer reads of a Publisher run folder (its ``manifest.json``): the packages it lists,
one per warehouse. ``FleetRequest`` is one fleet run: the Publisher run, where the observations come from (live system
tables, the simulated demo world or a folder of measurement evidence), the run folder the projections go into and the
state directory that holds the history, and the settings that apply to every warehouse. ``FleetReport`` is what the
run wrote: one ``WarehouseRun`` per warehouse (``realizer.contracts.report``), the run manifest and the workbook.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from realizer.contracts.report import RUN_STATUS_CRASHED, RUN_STATUS_REFUSED, WarehouseRun

SOURCE_LIVE = "LIVE_SYSTEM_TABLES"
SOURCE_DEMO = "DEMO_SIMULATION"
SOURCE_EVIDENCE = "MEASUREMENT_EVIDENCE"
FLEET_SOURCES = (SOURCE_LIVE, SOURCE_DEMO, SOURCE_EVIDENCE)

EXIT_OK = 0
EXIT_CRASHED = 1
EXIT_REFUSED = 2


@dataclass(frozen=True)
class PublisherRunEntry:
    """One package of a Publisher run: its warehouse, its id and its file, relative to the run folder."""
    workspace_id: str
    warehouse_id: str
    recommendation_package_id: str
    path: str


@dataclass(frozen=True)
class PublisherRun:
    """A Publisher run folder and the packages its manifest lists (``package_count`` as the manifest states it),
    the entries in (workspace, warehouse) order."""
    run_dir: Path
    package_count: int
    entries: Tuple[PublisherRunEntry, ...]


@dataclass(frozen=True)
class LiveSource:
    """The live measurement settings (``measure_live``) applied to every package of a live fleet run."""
    profile: str
    sql_warehouse_id: str
    config_file: Optional[str] = None
    source_region: Optional[str] = None
    aws_price_registry: Optional[str] = None
    ingestion_cutoff: Optional[str] = None


@dataclass(frozen=True)
class FleetRequest:
    """One fleet run.

    ``source`` is one of ``FLEET_SOURCES``: ``live`` is required for live measurement, ``evidence_dir`` for evidence
    (each package's newest measurement evidence file in it). ``run_dir`` receives the projections, the run manifest and
    the workbook; ``state_dir`` holds the history. ``as_of`` (the measurement time, ``None``: now), ``approvals`` and
    ``policy`` apply to every warehouse.
    """
    publisher_run: PublisherRun
    source: str
    run_dir: Path
    state_dir: Path
    live: Optional[LiveSource] = None
    evidence_dir: Optional[Path] = None
    as_of: Optional[str] = None
    approvals: Optional[Mapping[str, Any]] = None
    policy: Optional[Any] = None


@dataclass(frozen=True)
class FleetReport:
    """What a fleet run wrote: one ``WarehouseRun`` per warehouse in run order, the run manifest and the workbook
    (``None`` when it was not written)."""
    warehouse_runs: Tuple[WarehouseRun, ...]
    manifest_path: Path
    workbook_path: Optional[Path] = None

    @property
    def exit_status(self) -> int:
        """1 when any warehouse crashed, else 2 when any was refused, else 0."""
        statuses = {run.status for run in self.warehouse_runs}
        if RUN_STATUS_CRASHED in statuses:
            return EXIT_CRASHED
        if RUN_STATUS_REFUSED in statuses:
            return EXIT_REFUSED
        return EXIT_OK
