"""Helpers that keep hand-modified evidence internally consistent, as the collector would produce it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from publisher import publisher as p  # noqa: E402


def move_current_epoch_start(e, start):
    """Start the current epoch at `start` and attribute billing rows dated on or after that day to it.

    Readiness counts the current epoch's own settled billed days, so moving the epoch start without moving its billing
    rows would read as a configuration that has not run yet.
    """
    old = e.current_epoch.epoch_start
    e.current_epoch.epoch_start = start
    day = p.parse_datetime(start).date().isoformat()
    for r in e.daily_cost_365:
        if str(r.get("usage_date"))[:10] >= day and r.get("epoch_start") in (None, old):
            r["epoch_start"] = start
    return e
