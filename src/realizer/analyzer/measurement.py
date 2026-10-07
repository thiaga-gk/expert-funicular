"""Measurement planning: which configuration epochs and which whole UTC days a live measurement reads.

Pure: it reads the package and the warehouse's configuration epochs and settled billing day (the bridge's first pass)
and decides, for the step the observed configuration applies, the post-change window, the matched pre-change window and
the auto-stop values to replay. The bridge's second pass collects exactly these windows, and the projector reads them
back through the same plan. A step whose knob has no replay (``REPLAY_KNOBS``) gets the same windows without replay
values: its guards and billed cost before and after the change are observed, its saving is not measured.

* The step is the first authority-eligible CANDIDATE step of the recommended plan whose knob differs, at the as-of
  time, from the package's value; the change epoch is the first epoch of the latest run of epochs that hold the
  observed value of that knob, and the pre-change epoch the one before it.
* The post-change window is the most recent whole weeks of the epoch in effect at the as-of time, from its first whole
  UTC day to the last settled billing day (or the day before it ended), at most ``MAX_WINDOW_WEEKS``.
* The pre-change window is as many whole weeks as the post window holds (fewer when the pre-change epoch is shorter),
  ending the day before the change day; the change day itself is in neither window.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.analysis import normalize_value, plan_steps
from realizer.contracts.measurement import EPOCH_KNOBS
from realizer.governor.policy import authority_eligible

WEEK_DAYS = 7
MAX_WINDOW_WEEKS = 12  # the Publisher's long forecast window (84 days)
# Knobs whose counterfactual is an auto-stop replay.
REPLAY_KNOBS = ("auto_stop_minutes",)
# Knobs a counterfactual exists for (a min-cluster change: the floor cut's levels, ``cluster_counterfactual``); a change
# to another knob is observed, not measured.
COUNTERFACTUAL_KNOBS = REPLAY_KNOBS + ("min_clusters",)
# Knobs whose windows also read the running minutes by cluster count, and the knob whose measurement reads the size
# runtime (execution times on matched statement sources and busy minutes before and after the change).
LEVEL_KNOBS = ("min_clusters", "max_clusters")
RUNTIME_KNOBS = ("warehouse_size",)
# An auto-stop that never stops (0): replayed as a value longer than any gap (the bridge's NEVER_STOP_TARGET).
NEVER_STOP_TARGET_MINUTES = 366 * 1440


@dataclass(frozen=True)
class MeasurementPlan:
    """What one measurement reads. ``reason`` is set when there is nothing to measure: ``NOT_APPLIED`` (no step's knob
    changed; ``post`` is None), ``NOT_SETTLED`` (no whole settled week after the change; ``post`` is None),
    ``KNOB_NOT_REPLAYED`` (the changed knob has no replay counterfactual yet: the windows are planned as for a replayed
    knob, without replay targets, so the change is observed but not measured; ``post`` is None until a whole week has
    settled). ``pre`` is None when no pre-change epoch holds a whole week. ``other_knob_changes`` are decision knobs
    other than the step's that differ between the pre-change and the observed epoch."""
    observed_epoch: Mapping[str, Any]
    step: Optional[Mapping[str, Any]] = None
    change_epoch: Optional[Mapping[str, Any]] = None
    previous_epoch: Optional[Mapping[str, Any]] = None
    post: Optional[Mapping[str, Any]] = None
    pre: Optional[Mapping[str, Any]] = None
    settled_days_since_change: int = 0
    other_knob_changes: Tuple[str, ...] = field(default_factory=tuple)
    reason: Optional[str] = None

    @property
    def replayed_value(self) -> Optional[int]:
        """The step's source value, the auto-stop the counterfactual replays (None when not a replayed knob)."""
        if self.step is None or self.step.get("knob") not in REPLAY_KNOBS:
            return None
        return int(self.step["current_value"])

    @property
    def reads_levels(self) -> bool:
        """Whether the windows also read the running minutes by cluster count (a cluster-count change)."""
        return self.step is not None and self.step.get("knob") in LEVEL_KNOBS

    @property
    def reads_size_runtime(self) -> bool:
        """Whether the measurement reads the size runtime over the pre- and post-change days (a size change)."""
        return self.step is not None and self.step.get("knob") in RUNTIME_KNOBS

    def windows(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """The bridge's second-pass request: per window its epoch, days, the auto-stop values to replay (the source
        value after the change; the applied value before it, the Publisher's own prediction on pre-change days) and
        whether it reads the running minutes by cluster count."""
        out: Dict[str, Optional[Dict[str, Any]]] = {"pre": None, "post": None}
        if self.post is not None:
            out["post"] = {**self.post, "targets": [replay_target(self.replayed_value)] if self.replayed_value is not None else [],
                           "levels": self.reads_levels}
        if self.pre is not None:
            applied = self.observed_epoch.get(str(self.step.get("knob"))) if self.step else None
            out["pre"] = {**self.pre, "targets": [replay_target(int(applied))] if self.replayed_value is not None and applied is not None else [],
                          "levels": self.reads_levels}
        return out


def replay_target(value: int) -> int:
    """The running-idle target for auto-stop *value*: the value itself, or the never-stop target for 0."""
    return NEVER_STOP_TARGET_MINUTES if int(value) == 0 else int(value)


def _day(ts: str) -> dt.date:
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.timezone.utc).date()


def first_full_day(ts: str) -> dt.date:
    """The first whole UTC day at or after *ts* (its own day when it is midnight)."""
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    return t.date() if t.time() == dt.time(0) else t.date() + dt.timedelta(days=1)


def epoch_config(epoch: Mapping[str, Any]) -> Dict[str, Any]:
    """An epoch's decision knobs in the Publisher's spelling."""
    return {k: normalize_value(k, epoch.get(k)) for k in EPOCH_KNOBS}


def plan_measurement(package: Mapping[str, Any], epochs: Sequence[Mapping[str, Any]], settled_through: Optional[str]) -> MeasurementPlan:
    """The measurement plan for *package* over the warehouse's *epochs* (oldest first, every one started before the as-of
    time; the last is in effect at it) and the last settled billing day."""
    if not epochs:
        raise ValueError("a measurement needs at least one configuration epoch")
    ordered = sorted(epochs, key=lambda e: dt.datetime.fromisoformat(str(e["epoch_start"]).replace("Z", "+00:00")))
    observed = ordered[-1]
    observed_cfg = epoch_config(observed)
    # The recommended plan's steps first, then every other scenario's (an owner may apply an alternative such as a migration).
    step = next((s for s in plan_steps(package) if authority_eligible(s)
                 and observed_cfg.get(str(s.get("knob"))) is not None
                 and observed_cfg.get(str(s.get("knob"))) != normalize_value(str(s.get("knob")), s.get("current_value"))), None)
    if step is None:
        return MeasurementPlan(observed_epoch=observed, reason="NOT_APPLIED")
    knob = str(step["knob"])
    value = observed_cfg[knob]
    i = len(ordered) - 1
    while i > 0 and epoch_config(ordered[i - 1])[knob] == value:
        i -= 1
    change, previous = ordered[i], (ordered[i - 1] if i > 0 else None)
    others = tuple(sorted(k for k in EPOCH_KNOBS if k != knob and previous is not None and epoch_config(previous)[k] != observed_cfg[k]))
    base = dict(observed_epoch=observed, step=step, change_epoch=change, previous_epoch=previous, other_knob_changes=others)
    first = first_full_day(observed["epoch_start"])
    settled = None if settled_through is None else dt.date.fromisoformat(settled_through)
    last = settled
    if observed.get("epoch_end") is not None and last is not None:
        last = min(last, _day(observed["epoch_end"]) - dt.timedelta(days=1))
    since_change = 0 if settled is None else max(0, (settled - first_full_day(change["epoch_start"])).days + 1)
    # A knob without a replay counterfactual is observed over the same windows (guards, billed cost before and after),
    # never measured: its reason stays KNOB_NOT_REPLAYED whether or not a whole week has settled.
    unreplayed = None if knob in COUNTERFACTUAL_KNOBS else "KNOB_NOT_REPLAYED"
    days = 0 if last is None else (last - first).days + 1
    weeks = min(MAX_WINDOW_WEEKS, days // WEEK_DAYS)
    if weeks < 1:
        return MeasurementPlan(**base, settled_days_since_change=since_change, reason=unreplayed or "NOT_SETTLED")
    assert last is not None
    post = {"label": "post", "epoch_start": observed["epoch_start"], "first_day": (last - dt.timedelta(days=weeks * WEEK_DAYS - 1)).isoformat(),
            "last_day": last.isoformat()}
    pre = None
    if previous is not None:
        pre_last = _day(change["epoch_start"]) - dt.timedelta(days=1)
        pre_days = (pre_last - first_full_day(previous["epoch_start"])).days + 1
        pre_weeks = min(weeks, max(0, pre_days) // WEEK_DAYS)
        if pre_weeks >= 1:
            pre = {"label": "pre", "epoch_start": previous["epoch_start"], "first_day": (pre_last - dt.timedelta(days=pre_weeks * WEEK_DAYS - 1)).isoformat(),
                   "last_day": pre_last.isoformat()}
    return MeasurementPlan(**base, post=post, pre=pre, settled_days_since_change=since_change, reason=unreplayed)


def window_bounds(window: Mapping[str, Any]) -> Tuple[str, str]:
    """``(start, end)`` UTC instants of a window of whole days (end exclusive: midnight after its last day)."""
    a = dt.datetime.combine(dt.date.fromisoformat(window["first_day"]), dt.time(), dt.timezone.utc)
    b = dt.datetime.combine(dt.date.fromisoformat(window["last_day"]) + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
    return a.isoformat().replace("+00:00", "Z"), b.isoformat().replace("+00:00", "Z")


def window_days(window: Optional[Mapping[str, Any]]) -> int:
    """Whole days a window holds (0 for none)."""
    if not window:
        return 0
    return (dt.date.fromisoformat(window["last_day"]) - dt.date.fromisoformat(window["first_day"])).days + 1


def daily_in(daily: Sequence[Mapping[str, Any]], window: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The billed daily cost rows inside a window's days."""
    if not window:
        return []
    return [r for r in daily if window["first_day"] <= r["usage_date"] <= window["last_day"]]
