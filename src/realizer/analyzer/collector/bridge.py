"""Bridge to the Publisher's read-only collector: the one Realizer module that runs Publisher SQL.

A live measurement reads system tables with the Publisher's own statements, so the Realizer and the Publisher share one
definition of a configuration epoch, of billed daily cost, of running idle time and of the canary guard
(docs/architecture/publisher/lld.md, collector SQL). Two passes:

* ``collect_history``: the warehouse's configuration epochs, its billed daily cost per epoch and the last settled
  billing day (the Publisher's settle buffer and workspace billing watermark);
* ``collect_windows``: running idle and the replay of auto-stop values over named epochs and whole UTC days up to the
  as-of time, the running minutes by cluster count of those windows (a cluster-count change), the canary guard over the
  same pre- and post-change days and, for a size change, the size runtime over them (execution times on the statement
  sources of both windows and the busy wall-clock minutes of each).

A backtest reads the same rows for many warehouses and past changes: ``collect_fleet_history`` (pass 1 for the fleet),
then ``collect_epoch_windows`` and ``collect_guard_periods`` (pass 2, several warehouses per statement, one epoch and
one pair of guard periods per warehouse in each). ``demo_fleet_client`` answers them from simulated demo-fleet
warehouses whose auto-stop was changed in the past.

The results are plain rows in the measurement evidence format (``realizer.contracts.measurement``); deciding which
epochs and days to measure (``realizer.analyzer.measurement``) and what they mean (``realizer.analyzer.projector``) is
the Realizer's own, pure work. Read-only access is ``workflow_shared.databricks.access`` bound to the Realizer's identity
(``REALIZER_ACCESS``). Logs carry labels and counts, never SQL text or values. A collector refusal (an invalid value
reaching a statement builder) is raised as ``InputError``.
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.collector.billing import billing_check_through, effective_billing_watermark
from publisher.analyzer.collector.demo import DEMO_FLEET, DEMO_WORKSPACES, replay_demo_rows, simulate_demo_world
from publisher.analyzer.collector.pricing import aws_cluster_hourly_price, load_aws_price_registry
from publisher.analyzer.collector.queries.billing import BILLING_WATERMARK_SQL, EPOCH_DAILY_COST_SQL
from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_SQL
from publisher.analyzer.collector.queries.events import (
    NEVER_STOP_TARGET_MINUTES,
    build_cluster_levels_sql,
    build_running_idle_sql,
    build_running_idle_targets_sql,
)
from publisher.analyzer.collector.queries.query_history import build_canary_guard_sql, build_size_runtime_sql
from publisher.analyzer.collector.sql_executor import with_ingestion_cutoff
from publisher.analyzer.normalizer import normalize_size
from publisher.common.errors import PublisherError
from publisher.common.time import iso, parse_datetime
from publisher.common.utils import optional_float, safe_float, safe_int
from publisher.governor.thresholds import Thresholds
from realizer.common.errors import InputError
from realizer.common.logging import LOGGER
from realizer.config.settings import REALIZER_VERSION, SIZE_RUNTIME_MIN_SOURCE_QUERIES
from realizer.contracts.measurement import ASSUMPTION_FIELDS, GUARD_FIELDS, REPLAY_FIELDS, SIZE_RUNTIME_FIELDS, WINDOW_FIELDS
from workflow_shared.databricks import access
from workflow_shared.databricks.access import AccessConfig, AuthContext

REALIZER_ACCESS = AccessConfig(logger=LOGGER, user_agent=f"sql-warehouse-realizer/{REALIZER_VERSION}", error_type=InputError)

# An auto-stop that never stops (0), replayed as a value longer than any gap.
NEVER_STOP_TARGET = NEVER_STOP_TARGET_MINUTES
# Days of history the collector's billed daily cost reads before the current date (EPOCH_DAILY_COST_SQL).
HISTORY_DAYS = 365
# Assumption fields whose value comes from the package's cost profile when it states one (applied_thresholds).
PROFILED_ASSUMPTIONS = ("queue_query_pct_safe", "canary_p95_regression_pct", "cold_start_added_share_max_pct")


class LiveSqlClient(access.SqlClient):
    """The shared read-only SQL client with the Realizer's logger, user agent and ``InputError``; a context manager."""

    def __init__(self, auth: AuthContext):
        super().__init__(auth, REALIZER_ACCESS)


def resolve_live_access(profile: str, sql_warehouse_id: str, measured_warehouse_ids: Sequence[str], config_file: Optional[str] = None) -> AuthContext:
    """OAuth access through a Databricks CLI profile, executing on SQL warehouse *sql_warehouse_id*.

    Refused (``InputError``) when the execution warehouse is one of the warehouses being measured: the collector's own
    statements would keep it running and change the idle time and cost it measures. A static PAT is refused by the
    shared access layer."""
    if not sql_warehouse_id:
        raise InputError("a live measurement needs --sql-warehouse-id, the warehouse the collector statements run on")
    if str(sql_warehouse_id) in {str(x) for x in measured_warehouse_ids}:
        raise InputError(f"the collector cannot run on the warehouse it measures ({sql_warehouse_id}): its own statements would "
                         "keep it running; use another SQL warehouse for --sql-warehouse-id")
    host, token, expiry = access.resolve_cli_token(REALIZER_ACCESS, profile, config_file)
    LOGGER.info("bridge.access profile=%s execution_warehouse_id=%s", profile, sql_warehouse_id)
    return AuthContext(profile=profile, host=host, http_path=f"/sql/1.0/warehouses/{sql_warehouse_id}", access_token=token,
                       token_expiry=expiry, execution_kind="warehouse", execution_id=str(sql_warehouse_id))


def assumptions(applied_thresholds: Optional[Mapping[str, Any]] = None) -> Dict[str, float]:
    """The Publisher policy values a measurement relies on: the defaults of its ``Thresholds``, with the values the
    package's cost profile applied (``cost_profile.applied_thresholds``, grouped by technique) where it states them."""
    t = Thresholds()
    values = {f: float(getattr(t, f)) for f in ASSUMPTION_FIELDS}
    flat = {k: v for group in (applied_thresholds or {}).values() if isinstance(group, Mapping) for k, v in group.items()}
    for field in PROFILED_ASSUMPTIONS:
        if isinstance(flat.get(field), (int, float)) and not isinstance(flat.get(field), bool):
            values[field] = float(flat[field])
    return values


def _query(client: Any, statement: str, label: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in client.query(statement, label=label)]


def _of_warehouse(rows: Sequence[Mapping[str, Any]], workspace_id: str, warehouse_id: str) -> List[Mapping[str, Any]]:
    return [r for r in rows if str(r.get("workspace_id") or "") == workspace_id and str(r.get("warehouse_id") or "") == warehouse_id]


def settled_through(as_of: str, workspace_watermark: Any, account_watermark: Optional[str], ingestion_cutoff: Optional[str],
                    settle_hours: float) -> Tuple[Optional[str], str, Optional[str]]:
    """The last UTC day whose billing counts as settled at *as_of*: the Publisher's settle buffer, bounded by the
    workspace's billing watermark (the account's when the workspace's stalled because its warehouses stopped is not
    provable here, so the workspace watermark stands). ``(day or None, watermark source, watermark)``."""
    check = billing_check_through(as_of, account_watermark, ingestion_cutoff, int(settle_hours))
    watermark, source = effective_billing_watermark(workspace_watermark, account_watermark, None, check_through=check)
    mark = parse_datetime(watermark) if watermark else None
    if mark is None:
        return None, source, None
    last = min(check, mark.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1))
    return last.isoformat(), source, iso(watermark)


def collect_history(client: Any, workspace_id: str, warehouse_id: str, *, as_of: str, settle_hours: float,
                    ingestion_cutoff: Optional[str] = None) -> Dict[str, Any]:
    """Pass 1: the warehouse's configuration epochs (oldest first), its billed daily cost rows and the settled billing
    day. Refused when ``system.compute.warehouses`` has no snapshot of the warehouse."""
    try:
        epoch_rows = _of_warehouse(_query(client, CONFIG_EPOCHS_SQL, "config_epochs"), workspace_id, warehouse_id)
        watermarks = _query(client, with_ingestion_cutoff(BILLING_WATERMARK_SQL, ingestion_cutoff), "billing_watermark")
        cost_rows = _of_warehouse(_query(client, with_ingestion_cutoff(EPOCH_DAILY_COST_SQL, ingestion_cutoff), "billing_daily_cost"),
                                  workspace_id, warehouse_id)
    except PublisherError as exc:
        raise InputError(str(exc)) from None
    if not epoch_rows:
        raise InputError(f"warehouse {workspace_id}/{warehouse_id} has no configuration snapshot in system.compute.warehouses")
    epochs = _observed_epochs(epoch_rows, as_of)
    day, source, mark = _settled(watermarks, workspace_id, as_of, ingestion_cutoff, settle_hours)
    daily = _daily_cost(cost_rows)
    LOGGER.info("bridge.history workspace_id=%s warehouse_id=%s epochs=%d billed_days=%d settled_through=%s watermark_source=%s",
                workspace_id, warehouse_id, len(epochs), len(daily), day, source)
    return {"epochs": epochs, "daily_cost": daily, "billing": {"settled_through": day, "watermark_source": source, "billing_watermark": mark}}


def _observed_epochs(epoch_rows: Sequence[Mapping[str, Any]], as_of: str) -> List[Dict[str, Any]]:
    """Configuration epochs that started before *as_of*, oldest first; one still open at *as_of* has no end."""
    as_of_dt = parse_datetime(as_of)
    epochs = []
    dated = [r for r in epoch_rows if parse_datetime(r.get("epoch_start")) is not None]
    if len(dated) < len(epoch_rows):
        LOGGER.warning("bridge.epoch_skipped reason=MISSING_EPOCH_START rows=%d", len(epoch_rows) - len(dated))
    for r in sorted(dated, key=lambda x: parse_datetime(x.get("epoch_start"))):
        start = parse_datetime(r.get("epoch_start"))
        if as_of_dt is None or start >= as_of_dt:
            continue  # an epoch that starts after the as-of time is not observed yet
        end = parse_datetime(r.get("epoch_end"))
        epochs.append({
            "epoch_start": iso(r.get("epoch_start")), "epoch_end": None if end is None or end >= as_of_dt else iso(r.get("epoch_end")),
            "warehouse_type": str(r.get("warehouse_type") or "UNKNOWN").upper(), "warehouse_channel": str(r.get("warehouse_channel") or "UNKNOWN").upper(),
            "warehouse_size": normalize_size(r.get("warehouse_size")), "min_clusters": safe_int(r.get("min_clusters"), 1),
            "max_clusters": safe_int(r.get("max_clusters"), 1), "auto_stop_minutes": safe_int(r.get("auto_stop_minutes"), 0),
        })
    return epochs


def _settled(watermarks: Sequence[Mapping[str, Any]], workspace_id: str, as_of: str, ingestion_cutoff: Optional[str],
             settle_hours: float) -> Tuple[Optional[str], str, Optional[str]]:
    """``settled_through`` for one workspace from the billing watermark rows (the account's is the latest of them)."""
    ws_mark = next((r for r in watermarks if str(r.get("workspace_id") or "") == workspace_id), {})
    account_mark = max((iso(r.get("billing_watermark")) for r in watermarks if r.get("billing_watermark") is not None),
                       default=None, key=lambda v: parse_datetime(v))
    return settled_through(as_of, ws_mark.get("billing_watermark"), account_mark, ingestion_cutoff, settle_hours)


def _daily_cost(cost_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Billed daily cost rows in the evidence format, sorted."""
    return sorted(({
        "usage_date": str(r.get("usage_date"))[:10], "epoch_start": iso(r.get("epoch_start")),
        "currency_code": None if r.get("currency_code") is None else str(r.get("currency_code")),
        "dbu_quantity": safe_float(r.get("dbu_quantity")), "unpriced_dbu_quantity": safe_float(r.get("unpriced_dbu_quantity")),
        "list_cost": optional_float(r.get("list_cost")), "retracted_dbu_quantity": safe_float(r.get("retracted_dbu_quantity")),
        "restated_dbu_quantity": safe_float(r.get("restated_dbu_quantity")),
    } for r in cost_rows), key=lambda x: (x["usage_date"], x["epoch_start"], x["currency_code"] or ""))


def collect_windows(client: Any, workspace_id: str, warehouse_id: str, *, as_of: str, windows: Mapping[str, Optional[Mapping[str, Any]]],
                    size_runtime: bool = False) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Pass 2: running idle and the replay of the requested auto-stop values for each window (``pre``, ``post``: an epoch,
    whole UTC days ``first_day``..``last_day``, ``targets`` and ``levels``, whether to read the running minutes by
    cluster count), then the canary guard over both windows' days and, with *size_runtime*, the size runtime over them
    (statement sources with at least ``SIZE_RUNTIME_MIN_SOURCE_QUERIES`` executed, finished queries in each window).
    ``(windows in the evidence format, guard rows by period or None, size runtime or None)``."""
    out: Dict[str, Any] = {}
    runtime = None
    try:
        for label in ("pre", "post"):
            spec = windows.get(label)
            if not spec:
                out[label] = None
                continue
            asked = [{"workspace_id": workspace_id, "warehouse_id": warehouse_id, "label": label, "start": spec["first_day"],
                      "end": spec["last_day"], "epoch_start": spec["epoch_start"]}]
            targets = sorted({int(x) for x in spec.get("targets") or []}) or None
            totals = _query(client, build_running_idle_sql(Thresholds(), asked, targets=targets, as_of=as_of), "running_idle")
            replay = _query(client, build_running_idle_targets_sql(Thresholds(), asked, targets=targets, as_of=as_of), "running_idle_targets") if targets else []
            row = next((r for r in totals if str(r.get("window_label")) == label), None)
            levels = None
            if spec.get("levels"):
                rows = _query(client, build_cluster_levels_sql(Thresholds(), asked, as_of=as_of), "cluster_levels")
                levels = sorted(({"cluster_count": safe_int(r.get("cluster_count")), "running_minutes": safe_float(r.get("running_minutes"))}
                                 for r in rows if str(r.get("window_label")) == label and safe_int(r.get("cluster_count")) > 0),
                                key=lambda x: x["cluster_count"])
            out[label] = {
                "label": label, "epoch_start": spec["epoch_start"], "first_day": spec["first_day"], "last_day": spec["last_day"],
                "window_start": None if row is None else iso(row.get("window_start")), "window_end": None if row is None else iso(row.get("window_end")),
                "running": None if row is None else {f: safe_float(row.get(f)) for f in WINDOW_FIELDS},
                "replay": sorted(({"target_min": safe_int(r.get("target_min")), **{f: safe_float(r.get(f)) for f in REPLAY_FIELDS}}
                                  for r in replay if str(r.get("window_label")) == label), key=lambda x: x["target_min"]),
                "levels": levels,
            }
        guard = None
        if out.get("pre") and out.get("post"):
            period = {"workspace_id": workspace_id, "warehouse_id": warehouse_id, "epoch_start": out["post"]["epoch_start"],
                      "previous": [out["pre"]["first_day"], out["pre"]["last_day"]], "current": [out["post"]["first_day"], out["post"]["last_day"]]}
            rows = _query(client, build_canary_guard_sql([period]), "canary_guard")
            by_period = {str(r.get("period")): r for r in rows}
            guard = {p: (None if p not in by_period else {f: optional_float(by_period[p].get(f)) for f in GUARD_FIELDS}) for p in ("CURRENT", "PREVIOUS")}
            if size_runtime:
                found = _query(client, build_size_runtime_sql([period], min_source_queries=SIZE_RUNTIME_MIN_SOURCE_QUERIES), "size_runtime")
                row = next(iter(found), None)
                runtime = None if row is None else {"min_source_queries": SIZE_RUNTIME_MIN_SOURCE_QUERIES, **{
                    f: (optional_float(row.get(f)) if f in ("post_ms_at_post_median", "post_ms_at_pre_median")
                        else safe_float(row.get(f)) if f.endswith("_minutes") else safe_int(row.get(f))) for f in SIZE_RUNTIME_FIELDS}}
    except PublisherError as exc:
        raise InputError(str(exc)) from None
    LOGGER.info("bridge.windows workspace_id=%s warehouse_id=%s pre=%s post=%s guard=%s size_runtime=%s", workspace_id, warehouse_id,
                out.get("pre") is not None and out["pre"]["running"] is not None, out.get("post") is not None and out["post"]["running"] is not None,
                guard is not None, runtime is not None)
    return out, guard, runtime


def history_first_day(today: dt.date) -> str:
    """The first UTC day the collector's history holds when it runs on *today*: billed daily cost is read for the
    ``HISTORY_DAYS`` days before the current date (``EPOCH_DAILY_COST_SQL``), and system tables keep about a year."""
    return (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()


def collect_fleet_history(client: Any, *, as_of: str, settle_hours: float, ingestion_cutoff: Optional[str] = None,
                          selectors: Sequence[Tuple[Optional[str], str]] = ()) -> List[Dict[str, Any]]:
    """Backtest pass 1: per warehouse (all of them, or those *selectors* name as ``(workspace_id or None, warehouse_id)``)
    its configuration epochs, billed daily cost and settled billing day, read with one statement each for the fleet.
    Ordered by workspace and warehouse. Refused when a selector names no warehouse with a configuration snapshot."""
    try:
        epoch_rows = _query(client, CONFIG_EPOCHS_SQL, "config_epochs")
        watermarks = _query(client, with_ingestion_cutoff(BILLING_WATERMARK_SQL, ingestion_cutoff), "billing_watermark")
        cost_rows = _query(client, with_ingestion_cutoff(EPOCH_DAILY_COST_SQL, ingestion_cutoff), "billing_daily_cost")
    except PublisherError as exc:
        raise InputError(str(exc)) from None
    keys = sorted({(str(r.get("workspace_id") or ""), str(r.get("warehouse_id") or "")) for r in epoch_rows})
    chosen = [k for k in keys if not selectors or any(wh == k[1] and ws in (None, k[0]) for ws, wh in selectors)]
    for ws, wh in selectors:
        if not any(wh == k[1] and ws in (None, k[0]) for k in keys):
            raise InputError("--warehouse names a warehouse without a configuration snapshot in system.compute.warehouses")
    out = []
    for ws, wh in chosen:
        epochs = _observed_epochs(_of_warehouse(epoch_rows, ws, wh), as_of)
        if not epochs:
            continue
        day, source, mark = _settled(watermarks, ws, as_of, ingestion_cutoff, settle_hours)
        out.append({"workspace_id": ws, "warehouse_id": wh, "epochs": epochs, "daily_cost": _daily_cost(_of_warehouse(cost_rows, ws, wh)),
                    "billing": {"settled_through": day, "watermark_source": source, "billing_watermark": mark}})
    LOGGER.info("bridge.fleet_history warehouses=%d epochs=%d billed_days=%d", len(out), sum(len(w["epochs"]) for w in out),
                sum(len(w["daily_cost"]) for w in out))
    return out


def collect_epoch_windows(client: Any, windows: Sequence[Mapping[str, Any]], *, as_of: str) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Backtest pass 2: running idle and the replay of auto-stop values over named epochs, several warehouses in one
    statement. Each window is ``{workspace_id, warehouse_id, label, epoch_start, first_day, last_day, targets}``; the
    statement builder refuses two epochs of one warehouse. The statement replays every window's values; each window
    keeps the replay rows of its own. Keyed by ``(workspace_id, warehouse_id, label)`` in the evidence window format."""
    if not windows:
        return {}
    asked = [{"workspace_id": str(w["workspace_id"]), "warehouse_id": str(w["warehouse_id"]), "label": w["label"], "start": w["first_day"],
              "end": w["last_day"], "epoch_start": w["epoch_start"]} for w in windows]
    targets = sorted({int(x) for w in windows for x in w.get("targets") or []}) or None
    try:
        totals = _query(client, build_running_idle_sql(Thresholds(), asked, targets=targets, as_of=as_of), "running_idle")
        replay = _query(client, build_running_idle_targets_sql(Thresholds(), asked, targets=targets, as_of=as_of), "running_idle_targets") if targets else []
    except PublisherError as exc:
        raise InputError(str(exc)) from None
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for w in windows:
        ws, wh, label = str(w["workspace_id"]), str(w["warehouse_id"]), str(w["label"])
        mine = lambda r: (str(r.get("workspace_id") or ""), str(r.get("warehouse_id") or ""), str(r.get("window_label"))) == (ws, wh, label)  # noqa: E731
        own = {int(x) for x in w.get("targets") or []}
        row = next((r for r in totals if mine(r)), None)
        out[(ws, wh, label)] = {
            "label": label, "epoch_start": w["epoch_start"], "first_day": w["first_day"], "last_day": w["last_day"],
            "window_start": None if row is None else iso(row.get("window_start")), "window_end": None if row is None else iso(row.get("window_end")),
            "running": None if row is None else {f: safe_float(row.get(f)) for f in WINDOW_FIELDS},
            "replay": sorted(({"target_min": safe_int(r.get("target_min")), **{f: safe_float(r.get(f)) for f in REPLAY_FIELDS}}
                              for r in replay if mine(r) and safe_int(r.get("target_min")) in own), key=lambda x: x["target_min"]),
        }
    LOGGER.info("bridge.epoch_windows windows=%d with_running=%d", len(out), sum(1 for v in out.values() if v["running"] is not None))
    return out


def collect_guard_periods(client: Any, periods: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Optional[Dict[str, Optional[float]]]]]:
    """Backtest pass 2: the canary guard over each warehouse's pair of periods (``{workspace_id, warehouse_id,
    epoch_start, previous: [first, last], current: [first, last]}``, one pair per warehouse), in one statement. Keyed by
    ``(workspace_id, warehouse_id)``: the ``CURRENT`` and ``PREVIOUS`` rows (None for a period without one)."""
    if not periods:
        return {}
    try:
        rows = _query(client, build_canary_guard_sql(periods), "canary_guard")
    except PublisherError as exc:
        raise InputError(str(exc)) from None
    out = {}
    for x in periods:
        key = (str(x["workspace_id"]), str(x["warehouse_id"]))
        by_period = {str(r.get("period")): r for r in rows if (str(r.get("workspace_id") or ""), str(r.get("warehouse_id") or "")) == key}
        out[key] = {p: (None if p not in by_period else {f: optional_float(by_period[p].get(f)) for f in GUARD_FIELDS}) for p in ("CURRENT", "PREVIOUS")}
    LOGGER.info("bridge.guard_periods warehouses=%d", len(out))
    return out


def load_registry(path: Optional[str]) -> Dict[str, Any]:
    """The regional EC2 price registry at *path* (the Publisher's loader); a missing or malformed file is refused."""
    try:
        return load_aws_price_registry(path)
    except (PublisherError, ValueError) as exc:
        raise InputError(f"AWS price registry is not readable: {path} ({type(exc).__name__})") from None


def aws_price(warehouse_type: str, warehouse_size: str, region: Optional[str], registry: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The modeled customer AWS (EC2) price of one cluster-hour (the Publisher's topology and regional price registry).
    Serverless runs on Databricks-managed infrastructure: price 0, status NOT_CUSTOMER_BILLED. Unknown is null with a
    blocker, never zero."""
    if str(warehouse_type).upper() == "SERVERLESS":
        return {"status": "NOT_CUSTOMER_BILLED", "cluster_hourly_price": 0.0, "currency": "USD", "region": region, "blocker": None}
    if not region:
        return {"status": "UNAVAILABLE", "cluster_hourly_price": None, "currency": None, "region": None, "blocker": "SOURCE_REGION_NOT_GIVEN"}
    hourly, model = aws_cluster_hourly_price(normalize_size(warehouse_size), region, registry or {})
    if hourly is None:
        return {"status": "UNAVAILABLE", "cluster_hourly_price": None, "currency": None, "region": region, "blocker": model.get("blocker")}
    return {"status": "MODELED", "cluster_hourly_price": float(hourly), "currency": str(model.get("currency") or "USD"), "region": region, "blocker": None}


class DemoWorldClient:
    """Answers the collector statements from a simulated world (``publisher.analyzer.collector.demo.simulate_demo_world``):
    the demo and the end-to-end goldens run the live path on it. The billing watermark is the analysis day 06:00 UTC,
    as in the demo fleet. Never a measurement of a real warehouse."""

    def __init__(self, rows: Mapping[str, Any], analysis_time: str):
        self.rows = rows
        self.analysis_time = analysis_time
        self._cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
        if label == "billing_watermark":
            day = self.analysis_time[:10]
            return [{"workspace_id": ws, "billing_watermark": f"{day}T06:00:00+00:00", "billing_ingested_through": day}
                    for ws in sorted({r["workspace_id"] for r in self.rows.get("config_epochs", [])})]
        return replay_demo_rows(self.rows, statement, label, self._cache)


def demo_fleet_client(changes: Sequence[Mapping[str, Any]], analysis_time: str) -> DemoWorldClient:
    """A client over simulated demo-fleet warehouses whose configuration changed in the past (the backtest demo). Each
    change is ``{scenario, after, change_day, change_minute}``: the fleet warehouse of golden *scenario* keeps its own
    configuration until *change_day* + *change_minute* (minutes after 00:00 UTC) and runs with the spec keys of *after*
    (auto_stop, size, min, max, type) from then on. The simulated rows of all of them are merged, as one workspace's
    system tables. Never a measurement of a real warehouse."""
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for change in changes:
        index, fleet = next((i, s) for i, s in enumerate(DEMO_FLEET, start=1) if s["scenario"] == change["scenario"])
        spec = copy.deepcopy(fleet)
        before = {k: spec[k] for k in change["after"]}
        spec.update(change["after"])
        spec.update({"prior": [{"start": spec.pop("created", None) or "2025-07-01", **before}], "epoch_start": change["change_day"],
                     "epoch_start_minute": int(change.get("change_minute", 0))})
        for label, rows in simulate_demo_world(spec, index, analysis_time).items():
            merged.setdefault(label, []).extend(rows)
    return DemoWorldClient(merged, analysis_time)
