"""Collector: runs the read-only system-table SQL and normalizes the rows into WarehouseEvidence.

This is the only place where raw rows become typed evidence. It observes and normalizes; it makes no recommendation,
eligibility or pricing decision. Identity is always ``(workspace_id, warehouse_id)``, never the warehouse ID alone.
Logging carries counts and workspace/warehouse IDs only: never SQL text, owner emails, tags or config values.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.collector.billing import (
    _is_unpriced_only_row,
    billing_check_through,
    effective_billing_watermark,
)
from publisher.analyzer.collector.pricing import classify_sql_sku, model_aws_baseline_cost
from publisher.analyzer.collector.queries.billing import (
    BILLING_FEATURE_SQL,
    BILLING_WATERMARK_SQL,
    EPOCH_BILLING_ATTRIBUTION_SQL,
    EPOCH_DAILY_COST_SQL,
    SQL_SKU_PRICE_SQL,
)
from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_SQL
from publisher.analyzer.collector.queries.events import (
    EPOCH_EVENT_METRICS_SQL,
    RESOURCE_DAILY_COLLECT_DAYS,
    RESOURCE_DAILY_SQL,
    RUNNING_IDLE_TARGET_FIELDS,
    RUNNING_IDLE_WINDOW_FIELDS,
    build_epoch_gap_metrics_sql,
    build_running_idle_sql,
    build_running_idle_targets_sql,
)
from publisher.analyzer.collector.queries.query_history import (
    CANARY_GUARD_FIELDS,
    CANARY_GUARD_SQL,
    CARRY_PERIOD_COLUMNS,
    CONCURRENCY_SCOPES,
    EPOCH_QUERY_METRICS_SQL,
    build_carried_concurrency_sql,
    build_carried_query_metrics_sql,
    build_carried_spill_source_sql,
    build_concurrency_sql,
    build_spill_source_sql,
)
from publisher.analyzer.collector.sql_executor import SqlClient, with_ingestion_cutoff
from publisher.analyzer.epochs import carried_query_view, observed_epoch_calendar_days
from publisher.analyzer.forecast import Modeler
from publisher.analyzer.metrics import FLOOR_DEMAND_FIELD, floor_demand_map, running_idle_basis
from publisher.analyzer.normalizer import (
    clean_owner,
    derive_query_source_profile,
    infer_photon_observation,
    normalize_size,
)
from publisher.common.ids import epoch_id
from publisher.common.logging import LOGGER
from publisher.common.time import iso, parse_datetime
from publisher.common.utils import as_string_list, normalize_map, optional_float, safe_float, safe_int
from publisher.governor.thresholds import Thresholds
from publisher.contracts.source import EpochEvidence, WarehouseAttribution, WarehouseConfig, WarehouseEvidence


def _warehouse_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    """``(workspace_id, warehouse_id)`` of a collector row; a missing ID becomes "" so rows still group deterministically."""
    return str(row.get("workspace_id") or ""), str(row.get("warehouse_id") or "")


def _row_key(row: Mapping[str, Any]) -> Tuple[str, str, str]:
    """``(workspace_id, warehouse_id, epoch_start)`` of a collector row, with the epoch start as an ISO string."""
    workspace_id, warehouse_id = _warehouse_key(row)
    return workspace_id, warehouse_id, iso(row.get("epoch_start")) or str(row.get("epoch_start"))


def _index_epoch_rows(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Index rows by ``_row_key``; the SQL emits one row per epoch, so a repeated key would simply keep the last row."""
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        out[_row_key(r)] = r
    return out


def _attach_running_idle(client: SqlClient, thresholds: Thresholds, requests: Sequence[Tuple[EpochEvidence, Mapping[str, Any]]]) -> int:
    """Second pass: running idle of each current epoch on the windows its DBU forecast chose (``running_idle_basis``).

    Sets ``gaps.running_idle_model`` and ``gaps.running_idle_windows`` (label -> bounds, window totals and the sparse
    ``running_idle_by_target`` map, where an auto-stop value without a row is a known zero) only when every window was
    collected; otherwise the keys stay absent (not collected) and auto-stop fails closed. Returns how many epochs got them.
    """
    windows = [{"workspace_id": ep.workspace_id, "warehouse_id": ep.warehouse_id, "label": label,
                "start": None if days is None else days[0], "end": None if days is None else days[1]}
               for ep, basis in requests for label, days in sorted(basis["windows"].items())]
    if not windows:
        return 0
    by_key = {(ep.workspace_id, ep.warehouse_id, ep.epoch_start): (ep, basis) for ep, basis in requests}
    found: Dict[Tuple[str, str, str], Dict[str, Dict[str, Any]]] = {}
    for r in client.query(build_running_idle_sql(thresholds, windows), label="running_idle"):
        key, label = _row_key(r), str(r.get("window_label") or "")
        if key in by_key and label in by_key[key][1]["windows"]:
            found.setdefault(key, {})[label] = {"window_start": iso(r.get("window_start")), "window_end": iso(r.get("window_end")),
                                                **{f: r.get(f) for f in RUNNING_IDLE_WINDOW_FIELDS}, "running_idle_by_target": {}}
    for r in client.query(build_running_idle_targets_sql(thresholds, windows), label="running_idle_targets"):
        window = found.get(_row_key(r), {}).get(str(r.get("window_label") or ""))
        if window is not None:
            window["running_idle_by_target"][str(safe_int(r.get("target_min")))] = {f: r.get(f) for f in RUNNING_IDLE_TARGET_FIELDS}
    collected = 0
    for key, (ep, basis) in by_key.items():
        if set(found.get(key, {})) == set(basis["windows"]):
            ep.gaps["running_idle_model"] = basis["model"]
            ep.gaps["running_idle_windows"] = found[key]
            collected += 1
    return collected


def extract_evidence(client: SqlClient, thresholds: Thresholds, source_region: str, aws_registry: Optional[Mapping[str, Any]] = None, compatibility_evidence: Optional[Mapping[str, Any]] = None, *, ingestion_cutoff: Optional[str] = None, analysis_time: Optional[str] = None) -> List[WarehouseEvidence]:
    """Run every collector query once and assemble one WarehouseEvidence per active warehouse.

    ``client`` only needs ``query(statement, label=...)`` (a live SqlClient, or a replay/demo stand-in). Evidence is
    built per ``(workspace_id, warehouse_id)``; each warehouse must have exactly one current epoch (``ValueError``
    otherwise) and an epoch with more than one billed currency raises rather than mixing them.
    ``analysis_time`` (ISO, default now) is the single clock for window and age arithmetic. ``ingestion_cutoff``
    (YYYY-MM-DD, validated before any query) pins billing queries to what was ingested by that date so a re-collection
    reproduces an earlier run. ``aws_registry`` prices the AWS baseline and ``compatibility_evidence`` is the REST
    result; it is attached only to warehouses of its own workspace, others get an explicit fallback marker.

    Fail-closed conventions: unpriced usage leaves an epoch's list cost null (never zero); running-time evidence that
    cannot be established is None/UNAVAILABLE rather than a guessed zero. Result order is by warehouse name, workspace
    and warehouse ID, so it does not depend on query row order.
    """
    analysis_time = analysis_time or dt.datetime.now(dt.timezone.utc).isoformat()
    with_ingestion_cutoff("", ingestion_cutoff)  # validate the cutoff before any query runs
    LOGGER.info("collector.start source_region=%s ingestion_cutoff_set=%s", source_region, ingestion_cutoff is not None)
    LOGGER.info("collector.phase_start phase=discovery")
    epoch_rows = client.query(CONFIG_EPOCHS_SQL,label="config_epochs")
    LOGGER.info("collector.discovery current_warehouses=%d epoch_rows=%d", sum(1 for r in epoch_rows if bool(r.get("is_current"))), len(epoch_rows))
    LOGGER.info("collector.phase_done phase=discovery epoch_rows=%d", len(epoch_rows))
    LOGGER.info("collector.phase_start phase=query_evidence")
    query_idx = _index_epoch_rows(client.query(EPOCH_QUERY_METRICS_SQL,label="query_metrics"))
    for r in client.query(build_spill_source_sql(thresholds),label="spill_sources"):
        if _row_key(r) in query_idx:
            query_idx[_row_key(r)].update({"spill_sources_30": safe_int(r.get("spill_sources_30")), "top_spill_source_share_pct_30": optional_float(r.get("top_spill_source_share_pct_30"))})
    # Carried evidence (demand and capacity periods), attached to the current epoch's query metrics.
    carried: Dict[Tuple[str, str, str], Dict[str, Dict[str, Any]]] = {}
    for family in CARRY_PERIOD_COLUMNS:
        for r in client.query(build_carried_query_metrics_sql(family),label=f"query_metrics_carried_{family}"):
            key = (str(r.get("workspace_id") or ""), str(r.get("warehouse_id") or ""), iso(r.get("cur_start")) or str(r.get("cur_start")))
            row = {k: v for k, v in r.items() if k not in {"workspace_id", "warehouse_id", "cur_start"}}
            row["period_start"] = iso(row.get("period_start")) or row.get("period_start")
            carried.setdefault(key, {})[family] = row
    for r in client.query(build_carried_spill_source_sql(thresholds),label="spill_sources_carried"):
        demand = carried.get(_row_key(r), {}).get("demand")
        if demand is not None:
            demand.update({"spill_sources_30": safe_int(r.get("spill_sources_30")), "top_spill_source_share_pct_30": optional_float(r.get("top_spill_source_share_pct_30"))})
    for r in client.query(build_carried_concurrency_sql(),label="concurrency_carried"):
        demand = carried.get(_row_key(r), {}).get("demand")
        scope = str(r.get("scope") or "")
        if demand is not None and scope in ("RECENT", "BUSY_START"):
            demand.setdefault("concurrency_histogram", {}).setdefault(scope, {})[str(safe_int(r.get("level")))] = safe_float(r.get("weight"))
    for key, families in carried.items():
        # A new epoch that has not run a query yet has no query-metrics row; its carried evidence still counts.
        query_idx.setdefault(key, {"workspace_id": key[0], "warehouse_id": key[1], "epoch_start": key[2]})["carried"] = families
    for r in client.query(CANARY_GUARD_SQL,label="canary_guard"):
        if _row_key(r) in query_idx and str(r.get("period")) in ("CURRENT", "PREVIOUS"):
            query_idx[_row_key(r)].setdefault("canary_guard", {})[str(r.get("period"))] = {k: r.get(k) for k in CANARY_GUARD_FIELDS}
    LOGGER.info("collector.phase_done phase=query_evidence query_epochs=%d carried_periods=%d", len(query_idx), len(carried))
    LOGGER.info("collector.phase_start phase=event_evidence")
    event_idx = _index_epoch_rows(client.query(EPOCH_EVENT_METRICS_SQL,label="warehouse_events"))
    gap_idx = _index_epoch_rows(client.query(build_epoch_gap_metrics_sql(thresholds),label="idle_gaps"))
    concurrency_idx: Dict[Tuple[str, str, str], Dict[str, Dict[str, float]]] = {}
    for r in client.query(build_concurrency_sql(),label="concurrency"):
        scope = str(r.get("scope") or "")
        if scope in CONCURRENCY_SCOPES:
            concurrency_idx.setdefault(_row_key(r), {}).setdefault(scope, {})[str(safe_int(r.get("level")))] = safe_float(r.get("weight"))
    resource_daily_idx: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    # The query reports its own collected range (CURRENT_DATE at run time); the analysis clock is only a fallback.
    resource_collected_from: Optional[str] = None
    for r in client.query(RESOURCE_DAILY_SQL,label="resource_daily"):
        day = {k: (str(v)[:10] if k == "usage_day" else v) for k, v in r.items() if k not in {"workspace_id", "warehouse_id", "epoch_start", "collected_from"}}
        if FLOOR_DEMAND_FIELD in day:
            day[FLOOR_DEMAND_FIELD] = floor_demand_map(day[FLOOR_DEMAND_FIELD])
        resource_daily_idx.setdefault(_row_key(r), []).append(day)
        if r.get("collected_from") is not None:
            resource_collected_from = str(r.get("collected_from"))[:10]
    LOGGER.info("collector.phase_done phase=event_evidence event_epochs=%d gap_epochs=%d concurrency_epochs=%d resource_daily_epochs=%d",
                len(event_idx), len(gap_idx), len(concurrency_idx), len(resource_daily_idx))
    LOGGER.info("collector.phase_start phase=billing_evidence")
    cost_rows = client.query(with_ingestion_cutoff(EPOCH_DAILY_COST_SQL, ingestion_cutoff),label="billing_daily_cost")
    # Billing completeness is judged per workspace: one workspace's lagging ingestion must not be hidden by another's.
    watermark_by_ws = {str(r.get("workspace_id") or ""): r for r in client.query(with_ingestion_cutoff(BILLING_WATERMARK_SQL, ingestion_cutoff),label="billing_watermark")}
    account_watermark = max((iso(r.get("billing_watermark")) for r in watermark_by_ws.values() if r.get("billing_watermark") is not None), default=None, key=lambda v: parse_datetime(v))
    feature_rows = {_warehouse_key(r): r for r in client.query(with_ingestion_cutoff(BILLING_FEATURE_SQL, ingestion_cutoff),label="billing_features") if r.get("warehouse_id") is not None}
    attribution_idx = _index_epoch_rows(client.query(with_ingestion_cutoff(EPOCH_BILLING_ATTRIBUTION_SQL, ingestion_cutoff),label="billing_attribution_photon_owner"))
    sku_price_rows = client.query(SQL_SKU_PRICE_SQL,label="sql_sku_prices")
    sku_prices_by_account: Dict[str, Dict[str, Any]] = {}
    nameless_sku_rows = 0
    for r in sku_price_rows:
        account_id = str(r.get("account_id") or "")
        name = str(r.get("sku_name") or "")
        if not name:
            nameless_sku_rows += 1
            continue
        sku_prices_by_account.setdefault(account_id, {})[name] = {
            "effective_list_price": safe_float(r.get("effective_list_price")),
            "currency_code": str(r.get("currency_code") or "USD"),
            "cloud": str(r.get("cloud") or "AWS"),
            "usage_unit": str(r.get("usage_unit") or "DBU"),
            "classification": classify_sql_sku(name),
        }

    if nameless_sku_rows:
        LOGGER.warning("collector.sku_price_skipped reason=MISSING_SKU_NAME rows=%d", nameless_sku_rows)

    daily_by_wh: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    daily_by_epoch: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in cost_rows:
        wk = _warehouse_key(r)
        daily_by_wh.setdefault(wk, []).append(r)
        daily_by_epoch.setdefault(_row_key(r), []).append(r)
    LOGGER.info("collector.phase_done phase=billing_evidence cost_rows=%d billed_warehouses=%d workspaces_with_watermark=%d sku_price_accounts=%d",
                len(cost_rows), len(daily_by_wh), len(watermark_by_ws), len(sku_prices_by_account))
    LOGGER.info("collector.phase_start phase=epoch_assembly")

    grouped: Dict[Tuple[str, str], List[EpochEvidence]] = {}
    for r in epoch_rows:
        workspace_id = str(r.get("workspace_id") or "")
        wh = str(r["warehouse_id"])
        wk = (workspace_id, wh)
        start = iso(r.get("epoch_start"))
        if not start:
            LOGGER.warning("collector.epoch_skipped workspace_id=%s warehouse_id=%s reason=MISSING_EPOCH_START", workspace_id, wh)
            continue
        key = (workspace_id, wh, start)
        config = WarehouseConfig(
            warehouse_type=str(r.get("warehouse_type") or "UNKNOWN").upper(),
            warehouse_channel=str(r.get("warehouse_channel") or "UNKNOWN").upper(),
            warehouse_size=normalize_size(r.get("warehouse_size")),
            min_clusters=safe_int(r.get("min_clusters"), 1),
            max_clusters=safe_int(r.get("max_clusters"), 1),
            auto_stop_minutes=safe_int(r.get("auto_stop_minutes"), 0),
        )
        epoch_daily = daily_by_epoch.get(key, [])
        currencies = sorted({str(x.get("currency_code") or "USD") for x in epoch_daily if not _is_unpriced_only_row(x)})
        if len(currencies) > 1:
            raise ValueError(f"Mixed currencies within epoch {key}: {currencies}")
        end = iso(r.get("epoch_end"))
        unpriced_dbu = round(sum(safe_float(x.get("unpriced_dbu_quantity")) for x in epoch_daily), 6)
        if unpriced_dbu > 0:
            LOGGER.warning("collector.epoch_cost_unknown workspace_id=%s warehouse_id=%s reason=UNPRICED_DBU unpriced_dbu=%.6f",
                           workspace_id, wh, unpriced_dbu)
        epoch_cost = {
            # Unpriced usage leaves the epoch's cost unknown; calibration then skips the epoch instead of underpricing it.
            "list_cost": None if unpriced_dbu > 0 else round(sum(safe_float(x.get("list_cost")) for x in epoch_daily), 2),
            "dbu_quantity": round(sum(safe_float(x.get("dbu_quantity")) for x in epoch_daily), 6),
            "unpriced_dbu_quantity": unpriced_dbu,
            "cost_days": observed_epoch_calendar_days(start, end, analysis_time),
            "active_cost_days": len({str(x.get("usage_date")) for x in epoch_daily}),
            "currency": currencies[0] if currencies else "USD",
        }
        grouped.setdefault(wk, []).append(EpochEvidence(
            epoch_id=epoch_id(workspace_id, wh, start),
            warehouse_id=wh,
            workspace_id=workspace_id,
            account_id=str(r.get("account_id") or ""),
            warehouse_name=str(r.get("warehouse_name") or wh),
            epoch_start=start,
            epoch_end=end,
            is_current=bool(r.get("is_current")),
            config=config,
            tags=normalize_map(r.get("tags")),
            # Concurrency histograms (collected for every epoch in the last 365 days; an empty histogram is a known zero).
            query={**query_idx.get(key, {}), **({"concurrency_histogram": {**{sc: {} for sc in (CONCURRENCY_SCOPES if bool(r.get("is_current")) else ("EPOCH",))},
                                                                         **concurrency_idx.get(key, {})}}
                                                if (end is None or parse_datetime(end) > parse_datetime(analysis_time) - dt.timedelta(days=365)) else {})},
            events=event_idx.get(key, {}),
            # Running-idle evidence (current epoch only) joins the gap facts after the forecast (warehouse assembly).
            gaps=dict(gap_idx.get(key, {})),
            cost=epoch_cost,
            # Current epoch only; [] means collected with no activity (a known zero), unlike None (not collected).
            resource_daily=sorted(resource_daily_idx.get(key, []), key=lambda d: d["usage_day"]) if bool(r.get("is_current")) else None,
        ))

    LOGGER.info("collector.phase_done phase=epoch_assembly warehouses=%d epochs=%d", len(grouped), sum(len(v) for v in grouped.values()))
    LOGGER.info("collector.phase_start phase=warehouse_assembly")
    modeler = Modeler(thresholds)
    result: List[WarehouseEvidence] = []
    idle_requests: List[Tuple[EpochEvidence, Dict[str, Any]]] = []
    for wk, epochs in grouped.items():
        workspace_id, wh = wk
        epochs.sort(key=lambda e: e.epoch_start)
        currents = [e for e in epochs if e.is_current]
        if len(currents) != 1:
            raise ValueError(f"Expected one current epoch for {workspace_id}/{wh}, found {len(currents)}")
        current = currents[0]
        current.events["resource_daily_from"] = resource_collected_from or (parse_datetime(analysis_time).astimezone(dt.timezone.utc).date() - dt.timedelta(days=RESOURCE_DAILY_COLLECT_DAYS)).isoformat()
        rows_days = sorted(str(r.get("usage_day"))[:10] for r in (current.resource_daily or []))
        lead_from = max(current.events["resource_daily_from"], current.epoch_start[:10])
        current.events["resource_coverage_from"] = None  # no billed day before the first retained event
        if rows_days and rows_days[0] > lead_from and any(
                lead_from <= str(x.get("usage_date"))[:10] < rows_days[0] and safe_float(x.get("dbu_quantity")) > 0
                for x in daily_by_epoch.get((workspace_id, wh, current.epoch_start), [])):
            # Billed before the warehouse's first retained event: running time is unknown until the first full day
            # the event history covers.
            current.events["resource_coverage_from"] = (dt.date.fromisoformat(rows_days[0]) + dt.timedelta(days=1)).isoformat()
            LOGGER.warning("collector.resource_coverage_degraded workspace_id=%s warehouse_id=%s reason=BILLED_BEFORE_FIRST_RETAINED_EVENT",
                           workspace_id, wh)
        if current.resource_daily == [] and any(
                str(x.get("usage_date"))[:10] >= current.events["resource_daily_from"] and safe_float(x.get("dbu_quantity")) > 0
                for x in daily_by_epoch.get((workspace_id, wh, current.epoch_start), [])):
            # Billed but no warehouse event in the retained history (running since before it): running time is
            # unknown, not idle.
            current.resource_daily = None
            current.events["resource_daily_status"] = "UNAVAILABLE_BILLED_WITHOUT_RETAINED_EVENTS"
            LOGGER.warning("collector.resource_daily_unavailable workspace_id=%s warehouse_id=%s reason=BILLED_WITHOUT_RETAINED_EVENTS",
                           workspace_id, wh)
        ws_watermark = watermark_by_ws.get(workspace_id, {})
        effective_watermark, watermark_source = effective_billing_watermark(
            ws_watermark.get("billing_watermark"), account_watermark, current.resource_daily,
            check_through=billing_check_through(analysis_time, account_watermark, ingestion_cutoff, thresholds.billing_settle_hours))
        model = modeler.forecast_from_daily_cost(daily_by_wh.get(wk, []), current.epoch_start, analysis_time, billing_watermark=effective_watermark, ingestion_cutoff=ingestion_cutoff)
        if watermark_source in ("WORKSPACE_UNVERIFIED_IDLE", "WORKSPACE_LAGGING", "ACCOUNT"):
            LOGGER.warning("collector.billing_watermark_degraded workspace_id=%s warehouse_id=%s reason=%s", workspace_id, wh, watermark_source)
        else:
            LOGGER.debug("collector.billing_watermark workspace_id=%s warehouse_id=%s source=%s", workspace_id, wh, watermark_source)
        model["billing_watermark_source"] = watermark_source
        model["billing_ingested_through"] = None if ws_watermark.get("billing_ingested_through") is None else str(ws_watermark.get("billing_ingested_through"))[:10]
        model["billing_ingestion_cutoff"] = ingestion_cutoff
        idle_requests.append((current, running_idle_basis(current.epoch_start, current.events.get("event_coverage_start"), model, thresholds)))
        account_sku_prices = sku_prices_by_account.get(current.account_id, {})
        attribution_row = attribution_idx.get((workspace_id, wh, current.epoch_start), {})
        node_types = as_string_list(attribution_row.get("observed_node_types"))
        sql_tiers = as_string_list(attribution_row.get("observed_sql_tiers"))
        aws_cost, aws_model = model_aws_baseline_cost(current, source_region, analysis_time, model, aws_registry or {}, thresholds, observed_node_types=node_types)
        if aws_cost is None:
            LOGGER.warning("collector.aws_baseline_unavailable workspace_id=%s warehouse_id=%s reason=%s", workspace_id, wh, aws_model.get("blocker") or "UNAVAILABLE")
        photon_observation = infer_photon_observation(attribution_row, analysis_time, thresholds)
        query_source = derive_query_source_profile(carried_query_view(current.query)[0])
        owners = as_string_list(attribution_row.get("observed_owners")) or []
        attribution = WarehouseAttribution(
            owner=clean_owner(attribution_row.get("latest_owner")),
            observed_owners=sorted({o for o in (clean_owner(x) for x in owners) if o is not None}),
            owner_source="system.billing.usage.identity_metadata.owned_by",
            query_source=query_source,
        )
        LOGGER.info(
            "collector.attribution workspace_id=%s warehouse_id=%s owner_known=%s photon_status=%s photon_coverage_pct=%.2f query_source=%s",
            workspace_id, wh, attribution.owner is not None, photon_observation.status,
            photon_observation.known_feature_coverage_pct, query_source.primary_category,
        )
        compatible_workspace = str((compatibility_evidence or {}).get("workspace_id") or "") == workspace_id
        if not compatible_workspace:
            # Without compatibility evidence at all (replay/demo) this is the normal state; with evidence for another workspace it is a fallback.
            (LOGGER.warning if compatibility_evidence else LOGGER.debug)(
                "collector.compatibility_fallback workspace_id=%s warehouse_id=%s reason=OTHER_WORKSPACE_API_NOT_QUERIED", workspace_id, wh)
        result.append(WarehouseEvidence(
            warehouse_id=wh,
            workspace_id=workspace_id,
            account_id=current.account_id,
            warehouse_name=current.warehouse_name,
            source_region=source_region,
            analysis_time=analysis_time,
            current_epoch_id=current.epoch_id,
            epochs=epochs,
            current_cost_365=model["current_cost_365"],
            baseline_forecast_cost_365=model["forecast_cost_365"],
            currency=model["currency"],
            cost_history_days=model["cost_history_days"],
            cost_cv_84=model["cost_cv_84"],
            forecast_metadata={k:v for k,v in model.items() if k not in {"current_cost_365","forecast_cost_365","currency"}},
            daily_cost_365=daily_by_wh.get(wk, []),
            billing_features={**feature_rows.get(wk, {}), **({} if sql_tiers is None else {"current_epoch_sql_tiers": sql_tiers}),
                              **({} if node_types is None else {"current_epoch_node_types": node_types})},
            aws_baseline_cost_365=aws_cost,
            aws_cost_model=aws_model,
            sql_sku_prices=account_sku_prices,
            compatibility_evidence=(dict(compatibility_evidence or {}) if str((compatibility_evidence or {}).get("workspace_id") or "") == workspace_id else {"mode":"A_HITL_FALLBACK","status":"OTHER_WORKSPACE_API_NOT_QUERIED","workspace_id":workspace_id}),
            photon_observation=photon_observation,
            attribution=attribution,
        ))
    LOGGER.info("collector.phase_done phase=warehouse_assembly warehouses=%d", len(result))
    LOGGER.info("collector.phase_start phase=running_idle")
    collected = _attach_running_idle(client, thresholds, idle_requests)
    LOGGER.info("collector.phase_done phase=running_idle warehouses=%d collected=%d", len(idle_requests), collected)
    LOGGER.info("collector.done warehouses=%d epochs=%d", len(result), sum(len(x.epochs) for x in result))
    return sorted(result, key=lambda e: (e.warehouse_name.lower(), e.workspace_id, e.warehouse_id))
