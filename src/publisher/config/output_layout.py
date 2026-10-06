"""Output layout: one Hive-style run folder per run under ``--output-dir``.

``<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/`` holds one run's packages/, evidence.json, manifest.json,
workbook.xlsx and publisher.log. The timestamp is the UTC wall-clock start of the run and appears only in the folder
path, never inside a file, so identical replays still write byte-identical files.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

RUN_DATE_PREFIX = "run_date="
RUN_TS_PREFIX = "run_ts="


def partitioned_run_dir(root: Path, now: dt.datetime) -> Path:
    """Create and return a new run folder under *root* for a run started at *now* (converted to UTC).

    A folder that already exists (two runs in the same second) is never reused: the next free ``-NN`` suffix is
    claimed instead, atomically, so a run never overwrites another run's output.
    """
    now = now.astimezone(dt.timezone.utc)
    parent = root / f"{RUN_DATE_PREFIX}{now:%Y-%m-%d}"
    name = f"{RUN_TS_PREFIX}{now:%Y%m%dT%H%M%SZ}"
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(0, 100):
        candidate = parent / (name if n == 0 else f"{name}-{n:02d}")
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"no free run folder under {parent} for {name}")


def latest_run_dir(path: Path) -> Path:
    """The run folder to read: *path* itself when it holds run output (a manifest or packages/), otherwise the
    newest ``run_date=*/run_ts=*`` folder under it. Raises FileNotFoundError when there is neither."""
    if (path / "manifest.json").is_file() or (path / "packages").is_dir():
        return path
    runs = sorted(
        (d for date in path.glob(f"{RUN_DATE_PREFIX}*") if date.is_dir() for d in date.glob(f"{RUN_TS_PREFIX}*") if d.is_dir()),
        key=lambda d: (d.parent.name, d.name),
    )
    if not runs:
        raise FileNotFoundError(f"no run output under {path}")
    return runs[-1]
