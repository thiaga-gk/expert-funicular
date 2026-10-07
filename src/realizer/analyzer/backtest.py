"""Backtest: past auto-stop changes, what the Publisher would have predicted for each and what it realized.

Pure and Realizer-owned. No package, no classification and no ledger: a backtest reads the configuration epochs of
``system.compute.warehouses`` (through the bridge) and measures each past auto-stop change with the same replay
arithmetic a live measurement uses, so the prediction model can be checked on real changes before a recommended one
matures.

* A past change is two consecutive epochs of one warehouse whose auto-stop differs while every other decision knob is
  equal. A pair where another knob changed too cannot isolate auto-stop (``OTHER_KNOB_CHANGED``); an unsupported
  warehouse type is never measured (``UNSUPPORTED_WAREHOUSE_TYPE``); the warehouse the collector statements run on is
  not measured (``EXECUTION_WAREHOUSE``).
* Post-change window: the most recent whole UTC weeks of the changed epoch, from its first whole day to the earlier of
  the last settled billing day and the day before the epoch ended, at most ``MAX_WINDOW_WEEKS``; none is
  ``NOT_SETTLED``, and whole weeks that fall before the first day the history holds (``history_first_day``: system
  tables keep a year) are ``BEFORE_HISTORY``. Pre-change window: as many whole weeks (fewer when the epoch before is
  shorter) ending the day before the change day; a change without one is still measured (no prediction, cross-check
  ``NO_PRE_WINDOW``). The change day is in neither window.
* Prediction: the saving the Publisher's replay would have predicted for moving old -> new, on the pre-change days run
  at the old value (the new value replayed). Realization: the saving the change made, on the post-change days run at
  the new value (the old value replayed). Each is priced at its own window's billed DBU cost per running cluster-minute
  and the modeled AWS price of a cluster-hour, and annualized (x 365 / window days). The billed cross-check sits next to
  the realization.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.crosscheck import billed_cross_check, weekly_costs
from realizer.analyzer.measurement import MAX_WINDOW_WEEKS, WEEK_DAYS, daily_in, epoch_config, first_full_day, replay_target, window_days
from realizer.analyzer.projector import billed, replay_net_cluster_minutes, restart_cost_cluster_minutes, volume_ratio, window_summary
from realizer.common.errors import InputError
from realizer.common.utils import money, parse_utc, ratio_string, utc_iso
from realizer.contracts.backtest import BACKTEST_REPORT_FORMAT_VERSION
from realizer.contracts.measurement import EPOCH_KNOBS

ANNUAL_DAYS = Decimal("365")
KNOB = "auto_stop_minutes"
GUARD_REPORTED = ("failure_cancel_pct", "queued_query_pct", "p95_execution_ms_shared_sources")
REPLAY_ROW_FIELDS = ("added_idle_cluster_minutes", "avoided_starts", "removable_running_cluster_minutes", "additional_starts")


@dataclass(frozen=True)
class PastChange:
    """One auto-stop change: the epoch before it and the epoch it started. ``reason`` is set when it is not measured
    (``post`` is then None); ``pre`` is None when the epoch before holds no whole week inside the history."""
    workspace_id: str
    warehouse_id: str
    previous_epoch: Mapping[str, Any]
    change_epoch: Mapping[str, Any]
    pre: Optional[Mapping[str, Any]] = None
    post: Optional[Mapping[str, Any]] = None
    reason: Optional[str] = None

    @property
    def old(self) -> int:
        return int(self.previous_epoch[KNOB])

    @property
    def new(self) -> int:
        return int(self.change_epoch[KNOB])

    @property
    def change_start(self) -> str:
        return str(self.change_epoch["epoch_start"])

    @property
    def raised(self) -> bool:
        """The change keeps the warehouse up longer: a higher value, or never stopping (0)."""
        return self.new == 0 or (self.old != 0 and self.new > self.old)

    def windows(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """The bridge's second-pass request: the post-change days replay the old value (the realization), the pre-change
        days the new one (the prediction)."""
        return {"pre": None if self.pre is None else {**self.pre, "targets": [replay_target(self.new)]},
                "post": None if self.post is None else {**self.post, "targets": [replay_target(self.old)]}}


def _time(ts: Any) -> dt.datetime:
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _window(label: str, epoch_start: str, last: dt.date, weeks: int) -> Dict[str, Any]:
    return {"label": label, "epoch_start": epoch_start, "first_day": (last - dt.timedelta(days=weeks * WEEK_DAYS - 1)).isoformat(),
            "last_day": last.isoformat()}


def find_changes(workspace_id: str, warehouse_id: str, epochs: Sequence[Mapping[str, Any]], settled_through: Optional[str],
                 history_first_day: str, *, supported_types: Collection[str], execution_warehouse_id: Optional[str] = None) -> List[PastChange]:
    """The past auto-stop changes of one warehouse (*epochs* oldest first, all started before the as-of time), with the
    windows each is measured on or the reason it is not."""
    ordered = sorted(epochs, key=lambda e: _time(e["epoch_start"]))
    history = dt.date.fromisoformat(history_first_day)
    settled = None if settled_through is None else dt.date.fromisoformat(settled_through)
    out: List[PastChange] = []
    for previous, change in zip(ordered, ordered[1:]):
        before, after = epoch_config(previous), epoch_config(change)
        if before[KNOB] == after[KNOB]:
            continue
        base = dict(workspace_id=workspace_id, warehouse_id=warehouse_id, previous_epoch=previous, change_epoch=change)
        if before["warehouse_type"] not in supported_types or after["warehouse_type"] not in supported_types:
            out.append(PastChange(**base, reason="UNSUPPORTED_WAREHOUSE_TYPE"))
            continue
        if any(before[k] != after[k] for k in EPOCH_KNOBS if k != KNOB):
            out.append(PastChange(**base, reason="OTHER_KNOB_CHANGED"))
            continue
        if execution_warehouse_id is not None and warehouse_id == execution_warehouse_id:
            out.append(PastChange(**base, reason="EXECUTION_WAREHOUSE"))
            continue
        first = first_full_day(change["epoch_start"])
        last = settled
        if change.get("epoch_end") is not None and last is not None:
            last = min(last, _time(change["epoch_end"]).date() - dt.timedelta(days=1))
        whole = 0 if last is None else max(0, (last - first).days + 1) // WEEK_DAYS
        weeks = 0 if last is None else min(MAX_WINDOW_WEEKS, max(0, (last - max(first, history)).days + 1) // WEEK_DAYS)
        if weeks < 1:
            out.append(PastChange(**base, reason="NOT_SETTLED" if whole < 1 else "BEFORE_HISTORY"))
            continue
        assert last is not None
        post = _window("post", change["epoch_start"], last, weeks)
        pre_last = _time(change["epoch_start"]).date() - dt.timedelta(days=1)
        pre_days = (pre_last - max(first_full_day(previous["epoch_start"]), history)).days + 1
        pre_weeks = min(weeks, max(0, pre_days) // WEEK_DAYS)
        pre = _window("pre", previous["epoch_start"], pre_last, pre_weeks) if pre_weeks >= 1 else None
        out.append(PastChange(**base, pre=pre, post=post))
    return out


def plan_backtest(evidence: Mapping[str, Any], *, supported_types: Collection[str]) -> List[PastChange]:
    """Every past change of the warehouses *evidence* holds (``warehouses``: epochs and settled billing day each),
    ordered by warehouse and change time."""
    out: List[PastChange] = []
    for w in evidence["warehouses"]:
        out.extend(find_changes(str(w["workspace_id"]), str(w["warehouse_id"]), w["epochs"], w["billing"]["settled_through"],
                                evidence["history_first_day"], supported_types=supported_types,
                                execution_warehouse_id=evidence.get("execution_warehouse_id")))
    return sorted(out, key=lambda c: (c.workspace_id, c.warehouse_id, _time(c.change_start)))


def collection_rounds(changes: Sequence[PastChange]) -> List[List[PastChange]]:
    """Measured changes grouped so one collector statement reads at most one epoch per warehouse: round *r* holds the
    *r*-th measured change of every warehouse (its pre-change windows in one statement, its post-change windows in
    another, its guard periods in a third)."""
    by_warehouse: Dict[Tuple[str, str], List[PastChange]] = {}
    for c in changes:
        if c.post is not None:
            by_warehouse.setdefault((c.workspace_id, c.warehouse_id), []).append(c)
    depth = max((len(v) for v in by_warehouse.values()), default=0)
    return [[v[r] for _, v in sorted(by_warehouse.items()) if r < len(v)] for r in range(depth)]


def _q(value: Any, places: str = "0.001") -> Optional[str]:
    """A measured quantity as a canonical decimal string (``None`` stays ``None``)."""
    return None if value is None else ratio_string(Decimal(str(value)).quantize(Decimal(places)))


def _replay_row(window: Mapping[str, Any], target: int) -> Dict[str, Decimal]:
    """The replay row of *target*; a value without a row is a known zero (the collector omits zero rows)."""
    row = next((r for r in window.get("replay") or [] if int(r["target_min"]) == target), None)
    return {f: Decimal(str((row or {}).get(f, 0))) for f in REPLAY_ROW_FIELDS}


def _priced(window: Optional[Mapping[str, Any]], plan_window: Optional[Mapping[str, Any]], daily: Sequence[Mapping[str, Any]],
            net: Optional[Decimal], aws_hourly: Optional[Decimal]) -> Optional[Dict[str, Any]]:
    """*net* cluster-minutes saved on one window, priced at that window's billed DBU cost per running cluster-minute
    and the modeled AWS price; None without running time or billing priced in one currency."""
    if window is None or plan_window is None or window.get("running") is None or net is None:
        return None
    bill = billed(daily_in(daily, plan_window))
    if bill["cost"] is None or len(bill["currencies"]) > 1:
        return None
    running = Decimal(str(window["running"]["observed_running_cluster_minutes"]))
    if running <= 0 and bill["cost"] > 0:
        return None  # billed without running time: cost per cluster-minute unknown
    rate = bill["cost"] / running if running > 0 else Decimal("0")
    dbu = net * rate
    aws = None if aws_hourly is None else net / 60 * aws_hourly
    total = dbu + (aws or Decimal("0"))
    days = window_days(plan_window)
    return {"currency": bill["currencies"][0] if bill["currencies"] else None, "days": days, "net": net, "rate": rate,
            "dbu": dbu, "aws": aws, "total": total, "annual": total * ANNUAL_DAYS / days}


def _economics(priced: Optional[Mapping[str, Any]], target: int) -> Optional[Dict[str, Any]]:
    if priced is None:
        return None
    return {"replayed_target_minutes": target, "net_cluster_minutes": _q(priced["net"]), "dbu_cost_per_cluster_minute": _q(priced["rate"], "0.000001"),
            "dbu_saving": money(priced["dbu"]), "aws_saving": money(priced["aws"]), "window_saving": money(priced["total"]),
            "annual_saving": money(priced["annual"])}


def _cold_start_pct(window: Optional[Mapping[str, Any]]) -> Optional[str]:
    if window is None or window.get("running") is None or not window["running"]["queries"]:
        return None
    return _q(Decimal(str(window["running"]["restarts_for_queries"])) * 100 / Decimal(str(window["running"]["queries"])), "0.01")


def _guard(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Optional[str]]]:
    return None if row is None else {f: _q(row.get(f), "0.01") for f in GUARD_REPORTED}


def _measured(change: PastChange, held: Mapping[str, Any], daily: Sequence[Mapping[str, Any]], assumptions: Mapping[str, Any]) -> Dict[str, Any]:
    """The report entry of one measured change from the windows the evidence holds for it."""
    windows = held["windows"]
    for label, asked in change.windows().items():
        got = windows.get(label)
        if (asked is None) != (got is None) or (asked is not None and (got["epoch_start"], got["first_day"], got["last_day"])
                                                 != (asked["epoch_start"], asked["first_day"], asked["last_day"])):
            raise InputError(f"backtest evidence does not hold the planned {label} window of a change")
    config = epoch_config(change.change_epoch)
    restart = restart_cost_cluster_minutes(config, assumptions)
    aws = held.get("aws") or {}
    aws_hourly = None if aws.get("cluster_hourly_price") is None else Decimal(str(aws["cluster_hourly_price"]))
    post_window, pre_window = windows.get("post"), windows.get("pre")
    realized_net = None
    if post_window is not None and post_window.get("running") is not None:
        realized_net = replay_net_cluster_minutes(_replay_row(post_window, replay_target(change.old)), change.old, change.new, restart)
    predicted_net = None
    if pre_window is not None and pre_window.get("running") is not None:
        predicted_net = -replay_net_cluster_minutes(_replay_row(pre_window, replay_target(change.new)), change.new, change.old, restart)
    realized = _priced(post_window, change.post, daily, realized_net, aws_hourly)
    predicted = _priced(pre_window, change.pre, daily, predicted_net, aws_hourly)
    post, pre = window_summary(post_window, daily), window_summary(pre_window, daily)
    ratio = volume_ratio(pre, post)
    check = None
    if realized is not None:
        pre_rows = daily_in(daily, change.pre)
        check = billed_cross_check(
            replay_dbu_saving=realized["dbu"],
            pre=None if pre is None else {"days": pre["days"], "billed_cost": billed(pre_rows)["cost"]},
            post=None if post is None else {"days": post["days"], "billed_cost": billed(daily_in(daily, change.post))["cost"]},
            pre_weekly=None if change.pre is None else weekly_costs(pre_rows, change.pre["first_day"], window_days(change.pre)),
            volume_ratio=ratio, other_knob_changes=())
    to_predicted = None
    if realized is not None and predicted is not None and predicted["annual"] > 0:
        to_predicted = _q(realized["annual"] / predicted["annual"], "0.0001")
    guard = held.get("guard") or {}
    return {
        **_identity(change),
        "warehouse_type": config["warehouse_type"], "warehouse_size": config["warehouse_size"], "min_clusters": config["min_clusters"],
        "direction": "RAISED" if change.raised else "LOWERED", "restart_cost_cluster_minutes": _q(restart),
        "currency": (realized or predicted or {}).get("currency"),
        "cost_basis": "DBU_AND_MODELED_AWS" if aws_hourly is not None else "DBU_ONLY",
        "aws_status": aws.get("status") or "UNAVAILABLE", "aws_cluster_hourly_price": _q(aws_hourly, "0.0001"),
        "pre_window": pre, "post_window": post, "volume_ratio": _q(ratio, "0.0001"),
        "prediction": _economics(predicted, replay_target(change.new)), "realization": _economics(realized, replay_target(change.old)),
        "realized_to_predicted_ratio": to_predicted,
        "cross_check": None if check is None else {
            "outcome": check["outcome"], "reason": check["reason"], "replay_dbu_saving": money(check["replay_dbu_saving"]),
            "billed_dbu_reduction": money(check["billed_dbu_reduction"]), "difference_pct": _q(check["difference_pct"], "0.01"),
            "noise": money(check["noise"]), "pre_weekly_costs": [money(x) for x in check["pre_weekly_costs"]],
        },
        "cold_start_share_pct": {"pre": _cold_start_pct(pre_window), "post": _cold_start_pct(post_window)},
        "guards": {"pre": _guard(guard.get("PREVIOUS")), "post": _guard(guard.get("CURRENT"))},
    }


def _identity(change: PastChange) -> Dict[str, Any]:
    return {"workspace_id": change.workspace_id, "warehouse_id": change.warehouse_id, "change_at_utc": utc_iso(change.change_start),
            "previous_epoch_start": utc_iso(change.previous_epoch["epoch_start"]), "old_auto_stop_minutes": change.old,
            "new_auto_stop_minutes": change.new}


def backtest_report(evidence: Mapping[str, Any], *, supported_types: Collection[str]) -> Dict[str, Any]:
    """The backtest report (format 1.0.0) *evidence* states: every measured change, every skipped one with its reason,
    and the assumptions used. Identical evidence gives an identical report."""
    changes = plan_backtest(evidence, supported_types=supported_types)
    by_warehouse = {(str(w["workspace_id"]), str(w["warehouse_id"])): w for w in evidence["warehouses"]}
    measured, skipped = [], []
    for c in changes:
        if c.post is None:
            skipped.append({**_identity(c), "reason": c.reason})
            continue
        w = by_warehouse[(c.workspace_id, c.warehouse_id)]
        held = next((h for h in w["changes"] if _time(h["change_epoch_start"]) == _time(c.change_start)), None)
        if held is None:
            raise InputError("backtest evidence does not hold a planned change")
        measured.append(_measured(c, held, w["daily_cost"], evidence["assumptions"]))
    return {
        "report_format_version": BACKTEST_REPORT_FORMAT_VERSION, "source": evidence["source"], "as_of_utc": utc_iso(evidence["as_of_utc"]),
        "history_first_day": evidence["history_first_day"], "source_region": evidence["source_region"],
        "warehouses_read": evidence["warehouses_read"],
        "assumptions": {**{k: _q(v, "0.0001") for k, v in sorted(evidence["assumptions"].items())}, "max_window_weeks": MAX_WINDOW_WEEKS,
                        "annualization_days": int(ANNUAL_DAYS)},
        "changes": measured, "skipped": skipped,
    }


def summary_lines(report: Mapping[str, Any]) -> List[str]:
    """One line per change for a person: warehouse, change time, old -> new, post-change days, predicted and realized
    annual saving and the cross-check outcome (a skipped change: its reason)."""
    lines = []
    entries = [(c, None) for c in report["changes"]] + [(s, s["reason"]) for s in report["skipped"]]
    for c, reason in sorted(entries, key=lambda x: (x[0]["workspace_id"], x[0]["warehouse_id"], parse_utc(x[0]["change_at_utc"]))):
        head = f"{c['workspace_id']}/{c['warehouse_id']} {c['change_at_utc']} auto-stop {c['old_auto_stop_minutes']}->{c['new_auto_stop_minutes']}"
        if reason is not None:
            lines.append(f"{head}: skipped {reason}")
            continue
        annual = lambda block: "n/a" if not block else block["annual_saving"]  # noqa: E731
        check = c["cross_check"] or {}
        outcome = check.get("outcome") or "n/a"
        if check.get("reason"):
            outcome += f" {check['reason']}"
        days = (c["post_window"] or {}).get("days", 0)
        lines.append(f"{head}: post {days} days, predicted {annual(c['prediction'])}/yr, realized {annual(c['realization'])}/yr "
                     f"{c['currency'] or ''} ({c['cost_basis']}), cross-check {outcome}".replace("  ", " "))
    return lines
