"""Billing collection helpers: settle buffer, ingestion watermark, unpriced rows and the unseen-warehouse check."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.collector.queries.billing import UNSEEN_BILLED_WAREHOUSES_SQL
from publisher.analyzer.collector.sql_executor import SqlClient, with_ingestion_cutoff
from publisher.common.logging import LOGGER
from publisher.common.time import iso, parse_datetime
from publisher.common.utils import optional_float, round_or_none, safe_float


def _is_unpriced_only_row(row: Mapping[str, Any]) -> bool:
    return row.get("currency_code") is None and row.get("list_cost") is None and safe_float(row.get("unpriced_dbu_quantity")) > 0


def settle_reference_time(analysis_time: str, ingestion_cutoff: Optional[str]) -> dt.datetime:
    """The time the settle buffer is measured from: the analysis time, or for a re-collection pinned to an ingestion
    date, that date's 00:00 UTC (earlier of the two), so a later re-collection reproduces the original run's basis."""
    reference = parse_datetime(analysis_time).astimezone(dt.timezone.utc)
    if ingestion_cutoff:
        cutoff = dt.datetime.combine(dt.date.fromisoformat(str(ingestion_cutoff)), dt.time.min, tzinfo=dt.timezone.utc)
        reference = min(reference, cutoff)
    return reference


def billing_check_through(analysis_time: str, account_watermark: Optional[str], ingestion_cutoff: Optional[str], settle_hours: int) -> dt.date:
    """Last UTC day whose billing should already be present: settled, passed by the account watermark and, for a
    re-collection pinned to an ingestion date, before that date. Running after it says nothing about ingestion lag."""
    bound = (settle_reference_time(analysis_time, ingestion_cutoff) - dt.timedelta(hours=settle_hours)).date() - dt.timedelta(days=1)
    acct = parse_datetime(account_watermark) if account_watermark else None
    if acct is not None:
        bound = min(bound, acct.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1))
    if ingestion_cutoff:
        bound = min(bound, dt.date.fromisoformat(str(ingestion_cutoff)) - dt.timedelta(days=1))
    return bound


def effective_billing_watermark(workspace_watermark: Any, account_watermark: Optional[str], resource_daily: Optional[Sequence[Mapping[str, Any]]], *, check_through: dt.date) -> Tuple[Optional[str], str]:
    """The workspace's own billing watermark, unless the warehouse simply stopped running.

    A workspace whose SQL warehouses go idle stops producing usage, so its watermark stalls at the last usage even though
    billing is complete. Warehouse events come from a separate pipeline: if the warehouse did not run after the
    workspace watermark, its later days are complete $0 days and the account-wide watermark applies. If it did run, the
    missing billing is real ingestion lag and the workspace watermark stands. Without the daily series it cannot tell,
    so the workspace watermark stands (fail closed)."""
    ws = parse_datetime(workspace_watermark) if workspace_watermark is not None else None
    acct = parse_datetime(account_watermark) if account_watermark else None
    if acct is None or (ws is not None and ws >= acct):
        return (iso(workspace_watermark) if ws is not None else account_watermark), "WORKSPACE" if ws is not None else "ACCOUNT"
    if resource_daily is None:
        return (iso(workspace_watermark) if ws is not None else None), "WORKSPACE_UNVERIFIED_IDLE"
    # Running counts as lag on days after the watermark's day through the last day that should already be billed, and on
    # the watermark's own day only when it provably ran after the watermark (more running minutes than had elapsed
    # before the watermark time; the daily series cannot place them within the day).
    ws_utc = ws.astimezone(dt.timezone.utc) if ws is not None else None
    ws_day = ws_utc.date().isoformat() if ws_utc is not None else ""
    before_watermark = (ws_utc.hour * 60 + ws_utc.minute + ws_utc.second / 60.0) if ws_utc is not None else 0.0
    last = check_through.isoformat()

    def ran_after(row: Mapping[str, Any]) -> bool:
        day, uptime = str(row.get("usage_day"))[:10], safe_float(row.get("uptime_minutes"))
        if day == ws_day:
            return uptime > before_watermark
        return ws_day < day <= last and uptime > 0

    ran_since = any(ran_after(r) for r in resource_daily)
    if ran_since:
        return (iso(workspace_watermark) if ws is not None else None), "WORKSPACE_LAGGING"
    return account_watermark, "ACCOUNT_IDLE_WAREHOUSE"


def collect_unseen_billed_warehouses(client: SqlClient, *, ingestion_cutoff: Optional[str] = None) -> List[Dict[str, Any]]:
    """SQL warehouses billed in the last 30 days that have no configuration snapshot: logged as a warning and listed
    in the manifest. Their configuration is unknown, so no package is written for them."""
    rows = client.query(with_ingestion_cutoff(UNSEEN_BILLED_WAREHOUSES_SQL, ingestion_cutoff), label="unseen_billed_warehouses")
    unseen = sorted(({"workspace_id": str(r.get("workspace_id") or ""), "warehouse_id": str(r.get("warehouse_id") or ""),
                      "billed_dbu_30": round_or_none(optional_float(r.get("billed_dbu_30")), 6), "last_usage_date": None if r.get("last_usage_date") is None else str(r.get("last_usage_date"))[:10],
                      "reason_code": "NO_CONFIGURATION_SNAPSHOT"} for r in rows), key=lambda x: (x["workspace_id"], x["warehouse_id"]))
    if unseen:
        LOGGER.warning("collector.unseen_billed_warehouses %d billed SQL warehouse(s) have no configuration snapshot in "
                       "system.compute.warehouses (retention?); not analyzed: %s", len(unseen),
                       ", ".join(f"{x['workspace_id']}/{x['warehouse_id']}" for x in unseen))
    else:
        LOGGER.info("collector.unseen_billed_warehouses count=0")
    return unseen
