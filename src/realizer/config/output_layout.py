"""Output layout of a Realizer CLI run: the long-lived state and one Hive-style run folder per run.

The history of every warehouse (ledgers, evidence, regeneration requests, measurement evidence, source package copies,
policies and state index, all under ``realization/``) lives in one state directory that outlives the runs, by default
``<output-dir>/state``. A run's projections (``realization/packages/`` and ``realization/snapshots/``), the run manifest
(``manifest.json``), the realization workbook (``workbook.xlsx``) and the log (``realizer.log``) go into a new run
folder ``<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/``, the Publisher's folder rule
(``publisher.config.output_layout.partitioned_run_dir``: the UTC start of the run, a folder never reused). With
``--flat-output`` the state and the projections both go under ``--output-dir`` itself (the single-folder layout, the
facade's default). The paths under each root are the same in both layouts, so the single-folder layout is the run-folder
layout with the state directory and the run folder being one directory.

A Publisher run is read from its run folder or, given a root, the newest ``run_date=*/run_ts=*`` folder under it
(``latest_run_dir``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from publisher.config.output_layout import RUN_DATE_PREFIX, RUN_TS_PREFIX, latest_run_dir, partitioned_run_dir

DEFAULT_STATE_DIRNAME = "state"
RUN_MANIFEST_NAME = "manifest.json"
RUN_LOG_NAME = "realizer.log"
WORKBOOK_NAME = "workbook.xlsx"
PUBLISHER_MANIFEST_NAME = "manifest.json"

__all__ = ["DEFAULT_STATE_DIRNAME", "PUBLISHER_MANIFEST_NAME", "RUN_DATE_PREFIX", "RUN_LOG_NAME", "RUN_MANIFEST_NAME", "RUN_TS_PREFIX",
           "RunLayout", "WORKBOOK_NAME", "latest_run_dir", "partitioned_run_dir", "resolve_layout", "resolve_state_dir",
           "warehouse_lock_path"]


@dataclass(frozen=True)
class RunLayout:
    """Where one CLI run writes: ``run_dir`` (the projections, the run manifest, the workbook and the log) and
    ``state_dir`` (the history). ``flat`` is the single-folder layout, which writes no log file."""
    run_dir: Path
    state_dir: Path
    flat: bool

    @property
    def log_path(self) -> Optional[Path]:
        """The run log file, ``None`` in the single-folder layout (stderr only, as the facade's runs)."""
        return None if self.flat else self.run_dir / RUN_LOG_NAME

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / RUN_MANIFEST_NAME

    @property
    def workbook_path(self) -> Path:
        return self.run_dir / WORKBOOK_NAME


def resolve_state_dir(output_dir: Path, state_dir: Optional[Path] = None, *, flat: bool = False) -> Path:
    """The state directory of a run under *output_dir*: *state_dir* when given, else ``<output-dir>/state``, or
    *output_dir* itself in the single-folder layout (*flat*). Nothing is created."""
    if state_dir is not None:
        return Path(state_dir)
    return Path(output_dir) if flat else Path(output_dir) / DEFAULT_STATE_DIRNAME


def resolve_layout(output_dir: Path, state_dir: Optional[Path] = None, *, flat: bool = False, now: Optional[dt.datetime] = None) -> RunLayout:
    """The layout of a run under *output_dir*: a new run folder (created now, named after the UTC start *now*) and the
    state directory (``resolve_state_dir``); with *flat*, *output_dir* itself is the run folder. Only the run folder
    is created here."""
    output_dir = Path(output_dir)
    state = resolve_state_dir(output_dir, state_dir, flat=flat)
    if flat:
        return RunLayout(run_dir=output_dir, state_dir=state, flat=True)
    return RunLayout(run_dir=partitioned_run_dir(output_dir, now or dt.datetime.now(dt.timezone.utc)), state_dir=state, flat=False)


def warehouse_lock_path(state_dir: Path, warehouse_key: str) -> Path:
    """The advisory lock file a fleet run holds while it realizes one warehouse: ``realization/state/<key>.lock``, next
    to the warehouse's state index, present only while a run holds it."""
    return Path(state_dir) / "realization" / "state" / f"{warehouse_key}.lock"
