"""system.compute.warehouse_events SQL: event metrics, gaps, running idle and the daily resource series."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Dict, List, Mapping, Sequence

from publisher.analyzer import metrics
from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_CTE
from publisher.analyzer.metrics import auto_stop_targets
from publisher.common.errors import PublisherError
from publisher.config.defaults import QUERIES_PER_CLUSTER_PRO_CLASSIC
from publisher.governor.thresholds import Thresholds


EPOCH_EVENT_METRICS_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, min_clusters, max_clusters, epoch_start, epoch_end FROM config_epochs),
events AS (
  SELECT
    workspace_id,
    warehouse_id,
    event_type,
    COALESCE(cluster_count,0) AS cluster_count,
    event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
),
segments AS (
  SELECT
    p.workspace_id,
    p.warehouse_id,
    p.epoch_start,
    p.epoch_end,
    p.min_clusters,
    p.max_clusters,
    e.event_type,
    e.event_time,
    e.cluster_count,
    GREATEST(e.event_time,p.epoch_start) AS segment_start,
    LEAST(COALESCE(e.next_event_time,CURRENT_TIMESTAMP()),COALESCE(p.epoch_end,CURRENT_TIMESTAMP())) AS segment_end
  FROM events e
  JOIN epochs p
    ON e.workspace_id=p.workspace_id
   AND e.warehouse_id=p.warehouse_id
   AND e.event_time < COALESCE(p.epoch_end,CURRENT_TIMESTAMP())
   AND COALESCE(e.next_event_time,CURRENT_TIMESTAMP()) > p.epoch_start
),
durations AS (
  SELECT *,
    GREATEST(0.0,TIMESTAMPDIFF(SECOND,segment_start,segment_end)/60.0) AS duration_minutes
  FROM segments
  WHERE segment_end > segment_start
)
SELECT
  workspace_id,
  warehouse_id,
  epoch_start,
  SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END) AS uptime_minutes,
  SUM(CASE WHEN cluster_count>0 THEN duration_minutes*cluster_count ELSE 0 END) AS cluster_minutes,
  SUM(CASE WHEN cluster_count=min_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END) AS minutes_at_min,
  SUM(CASE WHEN cluster_count=max_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END) AS minutes_at_max,
  100.0*SUM(CASE WHEN cluster_count=min_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END)
    /NULLIF(SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END),0) AS time_at_min_pct,
  100.0*SUM(CASE WHEN cluster_count=max_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END)
    /NULLIF(SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END),0) AS time_at_max_pct,
  MAX(cluster_count) AS max_observed_clusters,
  -- Like the recent window's peak: counts above the ceiling are cluster-recycling transients, not demand.
  MAX(CASE WHEN cluster_count<=max_clusters THEN cluster_count END) AS max_observed_clusters_within_ceiling,
  MIN(segment_start) AS event_coverage_start,
  COUNT_IF(event_type='STARTING' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS starts_30,
  COUNT_IF(event_type='SCALED_UP' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_ups_30,
  COUNT_IF(event_type='SCALED_DOWN' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_downs_30
FROM durations
GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


def build_epoch_gap_metrics_sql(t: Thresholds) -> str:
    """Busy time and the inter-query gap sample per epoch (auto-stop savings come from the running-idle collector)."""
    return f"""
WITH {CONFIG_EPOCHS_CTE},
epochs AS (SELECT workspace_id, warehouse_id, auto_stop_minutes, epoch_start, epoch_end FROM config_epochs),
retained AS (
  -- warehouse_events keeps 365 days: running time is only known after a warehouse's first retained event, so the
  -- gap sample covers the same period as the event totals it is divided by.
  SELECT workspace_id, warehouse_id, MIN(event_time) AS first_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
  GROUP BY workspace_id, warehouse_id
),
ordered AS (
  SELECT x.workspace_id, x.compute.warehouse_id AS warehouse_id, p.epoch_start, p.auto_stop_minutes,
    x.start_time, x.end_time,
    MAX(x.end_time) OVER (
      PARTITION BY x.workspace_id,x.compute.warehouse_id,p.epoch_start
      ORDER BY x.start_time, x.end_time, x.statement_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS previous_busy_end_time
  FROM system.query.history x
  JOIN epochs p ON x.workspace_id=p.workspace_id
    AND x.compute.warehouse_id=p.warehouse_id
    AND x.start_time>=p.epoch_start AND (p.epoch_end IS NULL OR x.start_time<p.epoch_end)
  JOIN retained r ON r.workspace_id=x.workspace_id AND r.warehouse_id=x.compute.warehouse_id
    AND x.start_time >= r.first_event_time
  WHERE x.compute.type='WAREHOUSE' AND x.start_time>=CURRENT_TIMESTAMP()-INTERVAL 365 DAYS
), busy AS (
  -- Wall-clock minutes with at least one query running (length of the union of query intervals), over the same
  -- period as the gaps: each query adds only the part that extends past every earlier-starting query. Milliseconds:
  -- most queries last under a second, and whole-second differences would drop them.
  SELECT workspace_id, warehouse_id, epoch_start,
    SUM(GREATEST(0.0,TIMESTAMPDIFF(MILLISECOND,GREATEST(start_time,COALESCE(previous_busy_end_time,start_time)),COALESCE(end_time,start_time))/60000.0)) AS busy_minutes
  FROM ordered GROUP BY workspace_id,warehouse_id,epoch_start
), gaps AS (
  SELECT *, GREATEST(0.0,TIMESTAMPDIFF(MILLISECOND,previous_busy_end_time,start_time)/60000.0) AS gap_minutes
  FROM ordered WHERE previous_busy_end_time IS NOT NULL AND start_time>previous_busy_end_time
), gap_stats AS (
  SELECT workspace_id, warehouse_id, epoch_start,
  COUNT(*) AS gap_count,
  PERCENTILE_APPROX(gap_minutes,0.50) AS p50_gap_minutes,
  PERCENTILE_APPROX(gap_minutes,0.90) AS p90_gap_minutes
  FROM gaps GROUP BY workspace_id,warehouse_id,epoch_start
)
SELECT b.workspace_id, b.warehouse_id, b.epoch_start, b.busy_minutes,
  COALESCE(g.gap_count,0) AS gap_count, g.p50_gap_minutes, g.p90_gap_minutes
FROM busy b LEFT JOIN gap_stats g
  ON g.workspace_id=b.workspace_id AND g.warehouse_id=b.warehouse_id AND g.epoch_start=b.epoch_start
"""


RUNNING_IDLE_TARGET_FIELDS = metrics.RUNNING_IDLE_TARGET_FIELDS
RUNNING_IDLE_WINDOW_FIELDS = metrics.RUNNING_IDLE_WINDOW_FIELDS
RUNNING_IDLE_WINDOW_LABELS = ("long", "recent", "cycles", "last_cycle", "epoch", "pre", "post")
# First line of the running-idle statements: the windows as JSON, so a replaying client (the demo, test fixtures) can
# answer for the windows the collector chose; SQL ignores it. An epoch-bound statement adds a second line with its
# epochs, as-of time and auto-stop values (RUNNING_IDLE_REQUEST_MARKER).
RUNNING_IDLE_WINDOWS_MARKER = "-- running_idle_windows: "
RUNNING_IDLE_REQUEST_MARKER = "-- running_idle_request: "
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:+-]+")
# An auto-stop value that never stops (auto-stop 0) replayed as a target: longer than any gap a 366-day window holds,
# so the warehouse stays up through every gap and every gap's restart is avoided.
NEVER_STOP_TARGET_MINUTES = 366 * 1440


def _timestamp_literal(value: Any) -> str:
    """A validated SQL TIMESTAMP literal (UTC, ISO) for an epoch start or an as-of time; no collected value reaches the
    SQL unchecked."""
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise PublisherError(f"running-idle request has an invalid timestamp: {text!r}") from None
    if parsed.tzinfo is None:
        raise PublisherError(f"running-idle request timestamp must carry a UTC offset: {text!r}")
    return "TIMESTAMP'" + parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "'"


def _epoch_bound_request(windows: Sequence[Mapping[str, Any]], targets: Any, as_of: Any) -> Any:
    """The epoch-bound request of a running-idle statement, or None for the default (current epoch, every value, now).

    A window may name the configuration epoch it measures (``epoch_start``); then every window of the statement must, one
    epoch per warehouse, and ``as_of`` (default now) closes an epoch that is still open. ``targets`` are the auto-stop
    values to replay (positive integers, default every value on the grid)."""
    epochs = {(str(w["workspace_id"]), str(w["warehouse_id"])): w.get("epoch_start") for w in windows}
    bound = any(w.get("epoch_start") is not None for w in windows)
    if not bound and targets is None and as_of is None:
        return None
    if bound and any(w.get("epoch_start") is None for w in windows):
        raise PublisherError("running-idle windows must all name their epoch or none")
    for w in windows:
        if bound and str(w["epoch_start"]) != str(epochs[(str(w["workspace_id"]), str(w["warehouse_id"]))]):
            raise PublisherError("a running-idle statement measures one epoch per warehouse")
    if targets is not None:
        targets = [int(x) for x in targets]
        if not targets or any(x <= 0 or x > NEVER_STOP_TARGET_MINUTES for x in targets) or len(set(targets)) != len(targets):
            raise PublisherError(f"running-idle targets must be distinct positive minutes: {targets!r}")
    return {"epochs": {f"{ws}/{wh}": None if e is None else str(e) for (ws, wh), e in sorted(epochs.items())} if bound else None,
            "as_of": None if as_of is None else str(as_of), "targets": targets}


def _window_values(windows: Sequence[Mapping[str, Any]]) -> str:
    """VALUES rows (workspace, warehouse, label, first instant, end instant) of the running-idle windows, validated so no
    collected value can change the SQL: identifiers are plain tokens, labels known, days ISO dates (UTC midnight
    bounds, the end exclusive); an ``epoch`` window has NULL bounds (the whole evidence period)."""
    rows = []
    for w in windows:
        ws, wh, label = str(w["workspace_id"]), str(w["warehouse_id"]), str(w["label"])
        if not _IDENTIFIER.fullmatch(ws) or not _IDENTIFIER.fullmatch(wh) or label not in RUNNING_IDLE_WINDOW_LABELS:
            raise PublisherError(f"running-idle window has an invalid identifier or label: {ws!r}/{wh!r}/{label!r}")
        if w.get("epoch_start") is not None:
            _timestamp_literal(w["epoch_start"])
        if w.get("start") is None:
            d0 = d1 = "CAST(NULL AS TIMESTAMP)"
        else:
            first, last = dt.date.fromisoformat(str(w["start"])), dt.date.fromisoformat(str(w["end"]))
            d0, d1 = f"TIMESTAMP'{first.isoformat()}T00:00:00Z'", f"TIMESTAMP'{(last + dt.timedelta(days=1)).isoformat()}T00:00:00Z'"
        rows.append(f"('{ws}', '{wh}', '{label}', {d0}, {d1})")
    return ",\n    ".join(rows)


def _running_idle_ctes(t: Thresholds, windows: Sequence[Mapping[str, Any]], request: Any = None) -> str:
    """Shared CTEs of the running-idle collector: same arithmetic as ``running_idle_metrics``.

    Gaps are built and classified over the current epoch's evidence period (from the later of the epoch start and the
    warehouse's first event retained in the last 365 days, to now), then reported per window (``win``: the DBU
    forecast's days, or the whole period). Milliseconds: whole-second differences would drop sub-second remainders on
    every gap.

    An epoch-bound ``request`` (``_epoch_bound_request``) measures the named epoch instead, open or closed, over its
    evidence period up to the earlier of its end and the as-of time, and replays only the requested auto-stop values;
    without one the statement is the current-epoch statement, byte for byte.
    """
    full = t.running_idle_full_window_fraction
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    tolerance = t.kept_alive_gap_tolerance_minutes
    stopped_lag = t.kept_alive_stopped_gap_minutes
    targets = auto_stop_targets(t)
    target_values = f"SEQUENCE({targets[0]}, {targets[-1]}, {t.auto_stop_step_minutes})"
    marker = RUNNING_IDLE_WINDOWS_MARKER + json.dumps([dict(w) for w in windows], sort_keys=True, separators=(",", ":"))
    retained_until = ""
    cur = """cur AS (
  -- Current epoch over the period the event totals cover: running time is known only after the first retained event.
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.auto_stop_minutes, GREATEST(1, c.min_clusters) AS floor_clusters,
    GREATEST(c.epoch_start, r.first_event_time) AS w_start, CURRENT_TIMESTAMP() AS w_end
  FROM config_epochs c JOIN retained r ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  WHERE c.epoch_end IS NULL
)"""
    if request is not None:
        marker += "\n" + RUNNING_IDLE_REQUEST_MARKER + json.dumps(request, sort_keys=True, separators=(",", ":"))
        as_of = "CURRENT_TIMESTAMP()" if request["as_of"] is None else _timestamp_literal(request["as_of"])
        if request["targets"] is not None:
            target_values = "ARRAY(" + ", ".join(str(x) for x in request["targets"]) + ")"
        if request["as_of"] is not None:
            retained_until = f" AND event_time < {as_of}"
        if request["epochs"] is None:
            epoch_filter = "WHERE c.epoch_end IS NULL"
            w_end = as_of
        else:
            pairs = ",\n    ".join(f"('{key.split('/', 1)[0]}', '{key.split('/', 1)[1]}', {_timestamp_literal(e)})" for key, e in sorted(request["epochs"].items()))
            epoch_filter = f"""JOIN (VALUES
    {pairs}
  ) AS m(workspace_id, warehouse_id, epoch_start)
    ON m.workspace_id = c.workspace_id AND m.warehouse_id = c.warehouse_id AND m.epoch_start = c.epoch_start"""
            w_end = f"LEAST(COALESCE(c.epoch_end, {as_of}), {as_of})"
        cur = f"""cur AS (
  -- The requested epoch (open or closed) over the period the event totals cover, up to its end or the as-of time.
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.auto_stop_minutes, GREATEST(1, c.min_clusters) AS floor_clusters,
    GREATEST(c.epoch_start, r.first_event_time) AS w_start, {w_end} AS w_end
  FROM config_epochs c JOIN retained r ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  {epoch_filter}
)"""
    return f"""{marker}
WITH {CONFIG_EPOCHS_CTE},
retained AS (
  SELECT workspace_id, warehouse_id, MIN(event_time) AS first_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS{retained_until}
  GROUP BY workspace_id, warehouse_id
), {cur}, win AS (
  -- The windows the totals are reported for, inside the evidence period.
  SELECT c.workspace_id, c.warehouse_id, v.label,
    GREATEST(c.w_start, COALESCE(v.d0, c.w_start)) AS a, LEAST(c.w_end, COALESCE(v.d1, c.w_end)) AS b
  FROM (VALUES
    {_window_values(windows)}
  ) AS v(workspace_id, warehouse_id, label, d0, d1)
  JOIN cur c ON c.workspace_id = v.workspace_id AND c.warehouse_id = v.warehouse_id
), ev AS (
  SELECT workspace_id, warehouse_id, event_type, COALESCE(cluster_count, 0) AS cluster_count, event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), running AS (
  SELECT c.workspace_id, c.warehouse_id, v.cluster_count,
    GREATEST(v.event_time, c.w_start) AS seg_start,
    LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.cluster_count > 0 AND v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
), q AS (
  -- Queries from a day before the window seed the busy frontier; a query without an end time ends at start + total
  -- duration; with neither, one started in the last day is still running (busy until now), an older one ends at start.
  SELECT c.workspace_id, c.warehouse_id, x.statement_id, x.start_time,
    COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time),
      CASE WHEN x.start_time >= c.w_end - INTERVAL 1 DAY THEN c.w_end ELSE x.start_time END) AS busy_end
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id
    AND x.start_time >= GREATEST(c.epoch_start, c.w_start - INTERVAL 1 DAY) AND x.start_time < c.w_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 366 DAYS
), ordered AS (
  SELECT *, MAX(busy_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY start_time, busy_end, statement_id
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_busy_end
  FROM q
), gap_rows AS (
  SELECT workspace_id, warehouse_id, prev_busy_end AS g0, start_time AS g1, TRUE AS has_next
  FROM ordered WHERE prev_busy_end IS NOT NULL AND start_time > prev_busy_end
  UNION ALL
  -- After the last query (from the window start when there was none) the gap runs to the window end.
  SELECT c.workspace_id, c.warehouse_id, COALESCE(MAX(q.busy_end), c.w_start) AS g0, c.w_end AS g1, FALSE AS has_next
  FROM cur c LEFT JOIN q ON q.workspace_id = c.workspace_id AND q.warehouse_id = c.warehouse_id
  GROUP BY c.workspace_id, c.warehouse_id, c.w_start, c.w_end
  HAVING COALESCE(MAX(q.busy_end), c.w_start) < c.w_end
), g AS (
  -- A lower auto-stop cannot stop the warehouse later than the current setting does: running time past it was kept
  -- alive by something other than queries.
  SELECT r.workspace_id, r.warehouse_id, r.g0, r.g1, r.has_next, c.floor_clusters,
    GREATEST(r.g0, c.w_start) AS c0, LEAST(r.g1, c.w_end) AS c1,
    TIMESTAMPDIFF(MILLISECOND, r.g0, r.g1) / 60000.0 AS gap_min,
    CASE WHEN c.auto_stop_minutes > 0 THEN TIMESTAMPADD(MINUTE, c.auto_stop_minutes, r.g0) END AS stop_at
  FROM gap_rows r JOIN cur c ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  WHERE r.g1 > c.w_start
), pieces AS (
  -- RUNNING time inside each gap (inner join: a gap with no running segment has no piece, never a NULL bound).
  SELECT /*+ RANGE_JOIN(s, 3600) */ g.workspace_id, g.warehouse_id, g.g0, g.g1, s.cluster_count,
    GREATEST(g.c0, s.seg_start) AS p0, LEAST(g.c1, s.seg_end) AS p1
  FROM g JOIN running s ON s.workspace_id = g.workspace_id AND s.warehouse_id = g.warehouse_id
    AND s.seg_start < g.c1 AND s.seg_end > g.c0
), gap_running AS (
  -- The cluster count it stopped at: that of the last running piece in the gap (the floor when it did not run).
  SELECT g.workspace_id, g.warehouse_id, g.g0, g.g1,
    COALESCE(SUM(TIMESTAMPDIFF(MILLISECOND, p.p0, p.p1)), 0) / 60000.0 AS running_min,
    COALESCE(SUM(CASE WHEN g.stop_at IS NOT NULL AND p.p1 > g.stop_at
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, g.stop_at), p.p1) END), 0) / 60000.0 AS kept_alive_min,
    COALESCE(MAX_BY(p.cluster_count, p.p0), g.floor_clusters) AS stop_clusters
  FROM g LEFT JOIN pieces p ON p.workspace_id = g.workspace_id AND p.warehouse_id = g.warehouse_id AND p.g0 = g.g0 AND p.g1 = g.g1
  GROUP BY g.workspace_id, g.warehouse_id, g.g0, g.g1, g.floor_clusters
), gaps_full AS (
  -- A gap run past the current auto-stop by more than a late stop event explains (the tolerance when it ran through
  -- to the next query, the stopped-gap lag otherwise) was kept alive by something other than queries and would not
  -- stop at a lower value either: no removable time, no added restart.
  SELECT g.*, gr.running_min, gr.kept_alive_min, gr.stop_clusters,
    gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0 AS ran_through,
    gr.kept_alive_min > {tolerance} AND (gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0
      OR gr.kept_alive_min > {stopped_lag}) AS kept_alive
  FROM g JOIN gap_running gr ON gr.workspace_id = g.workspace_id AND gr.warehouse_id = g.warehouse_id AND gr.g0 = g.g0 AND gr.g1 = g.g1
), gap_win AS (
  -- Each gap in each window it touches: its time counts inside the window, its restart where its next query starts.
  SELECT f.*, w.label, w.a, w.b, f.g1 >= w.a AND f.g1 < w.b AS ends_inside
  FROM gaps_full f JOIN win w ON w.workspace_id = f.workspace_id AND w.warehouse_id = f.warehouse_id
    AND ((f.c0 < w.b AND f.c1 > w.a) OR (f.g1 >= w.a AND f.g1 < w.b))
), per_target AS (
  -- Only pairs that can contribute: a gap the warehouse ran through matters to values that do not cover it (with the
  -- margin); a gap it stopped in matters to every value (removable time, added idle, restarts saved).
  SELECT m.*, t.target_min,
    GREATEST(TIMESTAMPADD(MINUTE, t.target_min, m.g0), m.c0, m.a) AS r_start,
    LEAST(CASE WHEN m.kept_alive THEN m.c0 WHEN m.stop_at IS NULL THEN m.c1 ELSE LEAST(m.c1, m.stop_at) END, m.b) AS r_end,
    -- Raising: it stays up from where it stopped until min(gap, T) minutes into the gap.
    GREATEST(0, TIMESTAMPDIFF(MILLISECOND,
      GREATEST(TIMESTAMPADD(MILLISECOND, CAST(ROUND(m.running_min * 60000) AS BIGINT), m.g0), m.a),
      LEAST(TIMESTAMPADD(MILLISECOND, CAST(ROUND(LEAST(m.gap_min, t.target_min) * 60000) AS BIGINT), m.g0), m.b))) / 60000.0 AS extra_min
  FROM gap_win m CROSS JOIN (SELECT EXPLODE({target_values}) AS target_min) t
  WHERE NOT m.ran_through OR t.target_min < {cover} * m.gap_min
), removable AS (
  SELECT m.workspace_id, m.warehouse_id, m.label, m.g0, m.g1, m.has_next, m.gap_min, m.ran_through, m.ends_inside, m.target_min,
    m.extra_min, m.stop_clusters,
    NOT m.kept_alive AND (m.stop_at IS NULL OR m.stop_at >= m.g1) AS within_current,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) END), 0) / 60000.0 AS wall_min,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) * p.cluster_count END), 0) / 60000.0 AS cluster_min
  FROM per_target m LEFT JOIN pieces p ON p.workspace_id = m.workspace_id AND p.warehouse_id = m.warehouse_id AND p.g0 = m.g0 AND p.g1 = m.g1
  GROUP BY m.workspace_id, m.warehouse_id, m.label, m.g0, m.g1, m.has_next, m.gap_min, m.ran_through, m.ends_inside, m.target_min,
    m.extra_min, m.stop_clusters, m.stop_at, m.kept_alive
)"""


def build_running_idle_sql(t: Thresholds, windows: Sequence[Mapping[str, Any]], *, targets: Any = None, as_of: Any = None) -> str:
    """Running-idle collector, one row per current warehouse and window: bounds, queries, running time, kept-alive time
    and restarts for queries. Windows that name an ``epoch_start`` measure that epoch up to ``as_of`` (see
    ``_running_idle_ctes``)."""
    return _running_idle_ctes(t, windows, _epoch_bound_request(windows, targets, as_of)) + """
, seg_stats AS (
  SELECT w.workspace_id, w.warehouse_id, w.label, COUNT(*) AS running_segment_count,
    SUM(TIMESTAMPDIFF(MILLISECOND, GREATEST(s.seg_start, w.a), LEAST(s.seg_end, w.b)) * s.cluster_count) / 60000.0 AS observed_running_cluster_minutes,
    SUM(TIMESTAMPDIFF(MILLISECOND, GREATEST(s.seg_start, w.a), LEAST(s.seg_end, w.b))) / 60000.0 AS observed_running_wall_minutes
  FROM win w JOIN running s ON s.workspace_id = w.workspace_id AND s.warehouse_id = w.warehouse_id AND s.seg_start < w.b AND s.seg_end > w.a
  GROUP BY w.workspace_id, w.warehouse_id, w.label
), kept AS (
  -- Running time past the current auto-stop in kept-alive gaps, inside the window.
  SELECT m.workspace_id, m.warehouse_id, m.label, m.g0, m.g1,
    SUM(TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.stop_at, m.a), LEAST(p.p1, m.b))) / 60000.0 AS alive_min
  FROM gap_win m JOIN pieces p ON p.workspace_id = m.workspace_id AND p.warehouse_id = m.warehouse_id AND p.g0 = m.g0 AND p.g1 = m.g1
    AND p.p1 > GREATEST(m.stop_at, m.a) AND p.p0 < m.b
  WHERE m.kept_alive AND m.stop_at IS NOT NULL
  GROUP BY m.workspace_id, m.warehouse_id, m.label, m.g0, m.g1
), gap_stats AS (
  SELECT m.workspace_id, m.warehouse_id, m.label,
    COALESCE(SUM(k.alive_min), 0) AS kept_alive_wall_minutes, COUNT_IF(k.alive_min > 0) AS kept_alive_gap_count,
    COUNT_IF(m.has_next AND NOT m.ran_through AND m.ends_inside) AS restarts_for_queries
  FROM gap_win m LEFT JOIN kept k ON k.workspace_id = m.workspace_id AND k.warehouse_id = m.warehouse_id AND k.label = m.label AND k.g0 = m.g0 AND k.g1 = m.g1
  GROUP BY m.workspace_id, m.warehouse_id, m.label
), query_stats AS (
  SELECT w.workspace_id, w.warehouse_id, w.label, COUNT(*) AS queries
  FROM q JOIN win w ON q.workspace_id = w.workspace_id AND q.warehouse_id = w.warehouse_id AND q.start_time >= w.a AND q.start_time < w.b
  GROUP BY w.workspace_id, w.warehouse_id, w.label
)
SELECT c.workspace_id, c.warehouse_id, c.epoch_start, w.label AS window_label, w.a AS window_start, w.b AS window_end,
  COALESCE(qs.queries, 0) AS queries,
  COALESCE(ss.running_segment_count, 0) AS running_segment_count,
  COALESCE(ss.observed_running_cluster_minutes, 0) AS observed_running_cluster_minutes,
  COALESCE(ss.observed_running_wall_minutes, 0) AS observed_running_wall_minutes,
  COALESCE(gs.kept_alive_wall_minutes, 0) AS kept_alive_wall_minutes,
  COALESCE(gs.kept_alive_gap_count, 0) AS kept_alive_gap_count,
  COALESCE(gs.restarts_for_queries, 0) AS restarts_for_queries
FROM win w JOIN cur c ON c.workspace_id = w.workspace_id AND c.warehouse_id = w.warehouse_id
LEFT JOIN gap_stats gs ON gs.workspace_id = w.workspace_id AND gs.warehouse_id = w.warehouse_id AND gs.label = w.label
LEFT JOIN seg_stats ss ON ss.workspace_id = w.workspace_id AND ss.warehouse_id = w.warehouse_id AND ss.label = w.label
LEFT JOIN query_stats qs ON qs.workspace_id = w.workspace_id AND qs.warehouse_id = w.warehouse_id AND qs.label = w.label
"""


def build_running_idle_targets_sql(t: Thresholds, windows: Sequence[Mapping[str, Any]], *, targets: Any = None, as_of: Any = None) -> str:
    """Running-idle collector, one row per current warehouse, window and auto-stop value with a non-zero field (absent = 0).
    ``targets`` limits the values replayed; windows that name an ``epoch_start`` measure that epoch up to ``as_of``."""
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    return _running_idle_ctes(t, windows, _epoch_bound_request(windows, targets, as_of)) + f"""
SELECT * FROM (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, m.label AS window_label, m.target_min,
    SUM(m.wall_min) AS removable_running_wall_minutes,
    SUM(m.cluster_min) AS removable_running_cluster_minutes,
    COUNT_IF(m.has_next AND m.ran_through AND m.within_current AND m.ends_inside AND m.target_min < {cover} * m.gap_min) AS additional_starts,
    COUNT_IF(m.has_next AND NOT m.ran_through AND m.ends_inside AND m.target_min >= {cover} * m.gap_min) AS avoided_starts,
    SUM(CASE WHEN NOT m.ran_through THEN m.extra_min ELSE 0 END) AS added_idle_wall_minutes,
    SUM(CASE WHEN NOT m.ran_through THEN m.extra_min * m.stop_clusters ELSE 0 END) AS added_idle_cluster_minutes
  FROM removable m JOIN cur c ON c.workspace_id = m.workspace_id AND c.warehouse_id = m.warehouse_id
  GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, m.label, m.target_min
) x
WHERE removable_running_wall_minutes > 0 OR removable_running_cluster_minutes > 0 OR additional_starts > 0 OR avoided_starts > 0
  OR added_idle_wall_minutes > 0 OR added_idle_cluster_minutes > 0
ORDER BY workspace_id, warehouse_id, window_label, target_min
"""


CLUSTER_LEVEL_FIELDS = ("cluster_count", "running_minutes")


def build_cluster_levels_sql(t: Thresholds, windows: Sequence[Mapping[str, Any]], *, as_of: Any = None) -> str:
    """Running minutes by cluster count, one row per warehouse, window and cluster count with running time: the RUNNING
    segments of the running-idle collector (``_running_idle_ctes``) inside each window. Every window names its
    ``epoch_start`` (one epoch per warehouse), measured up to ``as_of`` (default now)."""
    if not windows or any(w.get("epoch_start") is None for w in windows):
        raise PublisherError("cluster levels windows must each name their epoch")
    return _running_idle_ctes(t, windows, _epoch_bound_request(windows, None, as_of)) + """
SELECT c.workspace_id, c.warehouse_id, c.epoch_start, w.label AS window_label, s.cluster_count,
  SUM(TIMESTAMPDIFF(MILLISECOND, GREATEST(s.seg_start, w.a), LEAST(s.seg_end, w.b))) / 60000.0 AS running_minutes
FROM win w JOIN cur c ON c.workspace_id = w.workspace_id AND c.warehouse_id = w.warehouse_id
JOIN running s ON s.workspace_id = w.workspace_id AND s.warehouse_id = w.warehouse_id AND s.seg_start < w.b AND s.seg_end > w.a
GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, w.label, s.cluster_count
ORDER BY workspace_id, warehouse_id, window_label, cluster_count
"""


def running_idle_windows_from_statement(statement: str) -> List[Dict[str, Any]]:
    """The windows a running-idle statement was built for (its marker line); [] when it has none."""
    first = statement.lstrip().split("\n", 1)[0]
    return json.loads(first[len(RUNNING_IDLE_WINDOWS_MARKER):]) if first.startswith(RUNNING_IDLE_WINDOWS_MARKER) else []


def running_idle_request_from_statement(statement: str) -> Any:
    """The epoch-bound request of a running-idle statement (its second marker line: epochs, as-of time, auto-stop
    values), or None for a current-epoch statement."""
    lines = statement.lstrip().split("\n", 2)
    second = lines[1] if len(lines) > 1 else ""
    return json.loads(second[len(RUNNING_IDLE_REQUEST_MARKER):]) if second.startswith(RUNNING_IDLE_REQUEST_MARKER) else None


RESOURCE_DAILY_COLLECT_DAYS = 120  # covers the 84-day window plus realistic billing lag; Python clips to what was collected


# Daily resource time for the current epoch over the last 120 UTC days (Python trims to the billing basis). Warehouse-event
# segments are split at midnight so every minute lands on its own UTC day; the peak excludes cluster-recycling transients
# (cluster_count above max_clusters), which are counted separately. Time at the floor is also split by the clusters each
# minute's demand needed (running + queued queries, time-weighted, per documented queries per cluster; no query = 0) for
# demand below the floor: the spare floor capacity a lower floor removes.
RESOURCE_DAILY_SQL = r"""
WITH __CONFIG_EPOCHS__,
cur AS (
  SELECT workspace_id, warehouse_id, epoch_start, min_clusters, max_clusters,
    GREATEST(epoch_start, CAST(DATE_SUB(CURRENT_DATE(), 120) AS TIMESTAMP)) AS w_start,
    CAST(CURRENT_DATE() AS TIMESTAMP) AS w_end
  FROM config_epochs
  WHERE epoch_end IS NULL AND epoch_start < CAST(CURRENT_DATE() AS TIMESTAMP)
), ev AS (
  SELECT workspace_id, warehouse_id, event_type, COALESCE(cluster_count,0) AS cluster_count, event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), seg AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.min_clusters, c.max_clusters, v.cluster_count,
    GREATEST(v.event_time, c.w_start) AS seg_start,
    LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
), days AS (
  SELECT c.workspace_id, c.warehouse_id, d AS day_start, TIMESTAMPADD(DAY, 1, d) AS day_end
  FROM cur c LATERAL VIEW explode(sequence(CAST(DATE(c.w_start) AS TIMESTAMP), TIMESTAMPADD(DAY, -1, c.w_end), INTERVAL 1 DAY)) t AS d
), piece AS (
  SELECT d.workspace_id, d.warehouse_id, s.epoch_start, s.min_clusters, s.max_clusters, s.cluster_count,
    DATE(d.day_start) AS usage_day, GREATEST(s.seg_start, d.day_start) AS p0, LEAST(s.seg_end, d.day_end) AS p1,
    GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(s.seg_start, d.day_start), LEAST(s.seg_end, d.day_end))) / 60.0 AS m
  FROM days d JOIN seg s ON s.workspace_id = d.workspace_id AND s.warehouse_id = d.warehouse_id
    AND s.seg_start < d.day_end AND s.seg_end > d.day_start AND s.seg_end > s.seg_start
), starts AS (
  SELECT v.workspace_id, v.warehouse_id, DATE(v.event_time) AS usage_day, COUNT(*) AS starts
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.event_type = 'STARTING' AND v.event_time >= c.w_start AND v.event_time < c.w_end
  GROUP BY v.workspace_id, v.warehouse_id, DATE(v.event_time)
), daily AS (
  SELECT workspace_id, warehouse_id, epoch_start, usage_day,
    SUM(CASE WHEN cluster_count > 0 THEN m ELSE 0 END) AS uptime_minutes,
    SUM(CASE WHEN cluster_count > 0 THEN m * cluster_count ELSE 0 END) AS cluster_minutes,
    SUM(CASE WHEN cluster_count > 0 AND cluster_count = min_clusters THEN m ELSE 0 END) AS minutes_at_min,
    SUM(CASE WHEN cluster_count > 0 AND cluster_count = max_clusters THEN m ELSE 0 END) AS minutes_at_max,
    SUM(CASE WHEN cluster_count > max_clusters THEN m ELSE 0 END) AS recycle_minutes,
    MAX(CASE WHEN cluster_count <= max_clusters THEN cluster_count END) AS max_cluster_count
  FROM piece
  GROUP BY workspace_id, warehouse_id, epoch_start, usage_day
), qx AS (
  SELECT c.workspace_id, c.warehouse_id, GREATEST(x.start_time, c.w_start) AS s,
    LEAST(COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time), x.start_time), c.w_end) AS t
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id
    AND x.start_time >= c.epoch_start AND x.start_time < c.w_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CAST(DATE_SUB(CURRENT_DATE(), 121) AS TIMESTAMP)
), qmin AS (
  -- Demand per minute in clusters: ceil(time-weighted running + queued queries / queries per cluster).
  SELECT workspace_id, warehouse_id, minute_start, CAST(CEIL(CEIL(SUM(ms) / 60000.0) / __QPC__) AS INT) AS need
  FROM (SELECT q.workspace_id, q.warehouse_id, m AS minute_start,
          TIMESTAMPDIFF(MILLISECOND, GREATEST(q.s, m), LEAST(q.t, TIMESTAMPADD(MINUTE, 1, m))) AS ms
        FROM qx q LATERAL VIEW EXPLODE(SEQUENCE(DATE_TRUNC('MINUTE', q.s), DATE_TRUNC('MINUTE', TIMESTAMPADD(MILLISECOND, -1, q.t)), INTERVAL 1 MINUTE)) mm AS m
        WHERE q.t > q.s) y
  GROUP BY workspace_id, warehouse_id, minute_start
  HAVING SUM(ms) > 0
), floor_busy AS (
  -- Busy time at the floor by the clusters the minute's demand needed.
  SELECT p.workspace_id, p.warehouse_id, p.usage_day, p.min_clusters, d.need,
    SUM(GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(p.p0, d.minute_start), LEAST(p.p1, TIMESTAMPADD(MINUTE, 1, d.minute_start))))) / 60.0 AS m
  FROM piece p
  JOIN qmin d ON d.workspace_id = p.workspace_id AND d.warehouse_id = p.warehouse_id
    AND d.minute_start < p.p1 AND TIMESTAMPADD(MINUTE, 1, d.minute_start) > p.p0
  WHERE p.cluster_count > 0 AND p.cluster_count = p.min_clusters
  GROUP BY p.workspace_id, p.warehouse_id, p.usage_day, p.min_clusters, d.need
), floor_rows AS (
  -- Idle time at the floor (no query running) is demand 0; busy time only for demand below the floor.
  SELECT dl.workspace_id, dl.warehouse_id, dl.usage_day, 0 AS need, dl.minutes_at_min - COALESCE(fb.busy, 0) AS m
  FROM daily dl LEFT JOIN (SELECT workspace_id, warehouse_id, usage_day, SUM(m) AS busy FROM floor_busy GROUP BY workspace_id, warehouse_id, usage_day) fb
    ON fb.workspace_id = dl.workspace_id AND fb.warehouse_id = dl.warehouse_id AND fb.usage_day = dl.usage_day
  UNION ALL
  SELECT workspace_id, warehouse_id, usage_day, need, m FROM floor_busy WHERE need < min_clusters
), floor_demand AS (
  SELECT workspace_id, warehouse_id, usage_day,
    TO_JSON(MAP_FROM_ENTRIES(COLLECT_LIST(STRUCT(CAST(need AS STRING) AS k, ROUND(m, 3) AS v)))) AS floor_minutes_by_demand_clusters
  FROM floor_rows WHERE m > 0
  GROUP BY workspace_id, warehouse_id, usage_day
)
SELECT dl.*, COALESCE(st.starts, 0) AS starts, COALESCE(fd.floor_minutes_by_demand_clusters, '{}') AS floor_minutes_by_demand_clusters,
  DATE_SUB(CURRENT_DATE(), 120) AS collected_from
FROM daily dl LEFT JOIN starts st
  ON st.workspace_id = dl.workspace_id AND st.warehouse_id = dl.warehouse_id AND st.usage_day = dl.usage_day
LEFT JOIN floor_demand fd
  ON fd.workspace_id = dl.workspace_id AND fd.warehouse_id = dl.warehouse_id AND fd.usage_day = dl.usage_day
ORDER BY dl.workspace_id, dl.warehouse_id, dl.usage_day
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE).replace("__QPC__", str(QUERIES_PER_CLUSTER_PRO_CLASSIC))
