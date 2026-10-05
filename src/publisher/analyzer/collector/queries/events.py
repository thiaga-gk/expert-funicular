"""system.compute.warehouse_events SQL: event metrics, gaps, running idle and the daily resource series."""
from __future__ import annotations

from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_CTE
from publisher.analyzer.metrics import auto_stop_targets
from publisher.config.thresholds import Thresholds


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


RUNNING_IDLE_TARGET_FIELDS = ("removable_running_wall_minutes", "removable_running_cluster_minutes", "additional_starts",
                              "avoided_starts", "added_idle_wall_minutes")


RUNNING_IDLE_HEADER_FIELDS = ("running_idle_window_start", "running_idle_window_end", "running_idle_queries", "running_segment_count",
                              "observed_running_cluster_minutes", "kept_alive_wall_minutes", "kept_alive_gap_count", "restarts_for_queries")


def _running_idle_ctes(t: Thresholds) -> str:
    """Shared CTEs of the running-idle collector: same arithmetic as ``running_idle_metrics``.

    Same period as the event totals (events.cluster_minutes): from the later of the epoch start and the warehouse's
    first event retained in the last 365 days, to now. Milliseconds: whole-second differences would drop sub-second
    remainders on every gap.
    """
    full = t.running_idle_full_window_fraction
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    tolerance = t.kept_alive_gap_tolerance_minutes
    stopped_lag = t.kept_alive_stopped_gap_minutes
    targets = auto_stop_targets(t)
    return f"""
WITH {CONFIG_EPOCHS_CTE},
retained AS (
  SELECT workspace_id, warehouse_id, MIN(event_time) AS first_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
  GROUP BY workspace_id, warehouse_id
), cur AS (
  -- Current epoch over the period the event totals cover: running time is known only after the first retained event.
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.auto_stop_minutes,
    GREATEST(c.epoch_start, r.first_event_time) AS w_start, CURRENT_TIMESTAMP() AS w_end
  FROM config_epochs c JOIN retained r ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  WHERE c.epoch_end IS NULL
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
  SELECT r.workspace_id, r.warehouse_id, r.g0, r.g1, r.has_next,
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
  SELECT g.workspace_id, g.warehouse_id, g.g0, g.g1,
    COALESCE(SUM(TIMESTAMPDIFF(MILLISECOND, p.p0, p.p1)), 0) / 60000.0 AS running_min,
    COALESCE(SUM(CASE WHEN g.stop_at IS NOT NULL AND p.p1 > g.stop_at
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, g.stop_at), p.p1) END), 0) / 60000.0 AS kept_alive_min
  FROM g LEFT JOIN pieces p ON p.workspace_id = g.workspace_id AND p.warehouse_id = g.warehouse_id AND p.g0 = g.g0 AND p.g1 = g.g1
  GROUP BY g.workspace_id, g.warehouse_id, g.g0, g.g1
), gaps_full AS (
  -- A gap run past the current auto-stop by more than a late stop event explains (the tolerance when it ran through
  -- to the next query, the stopped-gap lag otherwise) was kept alive by something other than queries and would not
  -- stop at a lower value either: no removable time, no added restart.
  SELECT g.*, gr.running_min, gr.kept_alive_min,
    gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0 AS ran_through,
    gr.kept_alive_min > {tolerance} AND (gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0
      OR gr.kept_alive_min > {stopped_lag}) AS kept_alive
  FROM g JOIN gap_running gr ON gr.workspace_id = g.workspace_id AND gr.warehouse_id = g.warehouse_id AND gr.g0 = g.g0 AND gr.g1 = g.g1
), per_target AS (
  -- Only pairs that can contribute: a gap the warehouse ran through matters to values that do not cover it (with the
  -- margin); a gap it stopped in matters to every value (removable time, added idle, restarts saved).
  SELECT f.*, t.target_min,
    GREATEST(TIMESTAMPADD(MINUTE, t.target_min, f.g0), f.c0) AS r_start,
    CASE WHEN f.kept_alive THEN f.c0 WHEN f.stop_at IS NULL THEN f.c1 ELSE LEAST(f.c1, f.stop_at) END AS r_end
  FROM gaps_full f CROSS JOIN (SELECT EXPLODE(SEQUENCE({targets[0]}, {targets[-1]}, {t.auto_stop_step_minutes})) AS target_min) t
  WHERE NOT f.ran_through OR t.target_min < {cover} * f.gap_min
), removable AS (
  SELECT m.workspace_id, m.warehouse_id, m.g0, m.g1, m.has_next, m.gap_min, m.running_min, m.ran_through, m.target_min,
    NOT m.kept_alive AND (m.stop_at IS NULL OR m.stop_at >= m.g1) AS within_current,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) END), 0) / 60000.0 AS wall_min,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) * p.cluster_count END), 0) / 60000.0 AS cluster_min
  FROM per_target m LEFT JOIN pieces p ON p.workspace_id = m.workspace_id AND p.warehouse_id = m.warehouse_id AND p.g0 = m.g0 AND p.g1 = m.g1
  GROUP BY m.workspace_id, m.warehouse_id, m.g0, m.g1, m.has_next, m.gap_min, m.running_min, m.ran_through, m.target_min, m.stop_at, m.kept_alive
)"""


def build_running_idle_sql(t: Thresholds) -> str:
    """Running-idle collector, one row per current warehouse: window, queries, running segments, kept-alive time."""
    return _running_idle_ctes(t) + """
, gap_stats AS (
  SELECT workspace_id, warehouse_id, SUM(CASE WHEN kept_alive THEN kept_alive_min ELSE 0 END) AS kept_alive_wall_minutes, COUNT_IF(kept_alive) AS kept_alive_gap_count,
    COUNT_IF(has_next AND NOT ran_through) AS restarts_for_queries
  FROM gaps_full GROUP BY workspace_id, warehouse_id
), seg_stats AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS running_segment_count,
    SUM(TIMESTAMPDIFF(MILLISECOND, seg_start, seg_end) * cluster_count) / 60000.0 AS observed_running_cluster_minutes
  FROM running WHERE seg_end > seg_start GROUP BY workspace_id, warehouse_id
), query_stats AS (
  SELECT q.workspace_id, q.warehouse_id, COUNT(*) AS running_idle_queries
  FROM q JOIN cur c ON q.workspace_id = c.workspace_id AND q.warehouse_id = c.warehouse_id
  WHERE q.start_time >= c.w_start GROUP BY q.workspace_id, q.warehouse_id
)
SELECT c.workspace_id, c.warehouse_id, c.epoch_start,
  c.w_start AS running_idle_window_start, c.w_end AS running_idle_window_end,
  COALESCE(qs.running_idle_queries, 0) AS running_idle_queries,
  COALESCE(ss.running_segment_count, 0) AS running_segment_count,
  COALESCE(ss.observed_running_cluster_minutes, 0) AS observed_running_cluster_minutes,
  COALESCE(gs.kept_alive_wall_minutes, 0) AS kept_alive_wall_minutes,
  COALESCE(gs.kept_alive_gap_count, 0) AS kept_alive_gap_count,
  COALESCE(gs.restarts_for_queries, 0) AS restarts_for_queries
FROM cur c
LEFT JOIN gap_stats gs ON gs.workspace_id = c.workspace_id AND gs.warehouse_id = c.warehouse_id
LEFT JOIN seg_stats ss ON ss.workspace_id = c.workspace_id AND ss.warehouse_id = c.warehouse_id
LEFT JOIN query_stats qs ON qs.workspace_id = c.workspace_id AND qs.warehouse_id = c.warehouse_id
"""


def build_running_idle_targets_sql(t: Thresholds) -> str:
    """Running-idle collector, one row per current warehouse and auto-stop value with a non-zero field (absent = 0)."""
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    return _running_idle_ctes(t) + f"""
SELECT c.workspace_id, c.warehouse_id, c.epoch_start, m.target_min,
  SUM(m.wall_min) AS removable_running_wall_minutes,
  SUM(m.cluster_min) AS removable_running_cluster_minutes,
  COUNT_IF(m.has_next AND m.ran_through AND m.within_current AND m.target_min < {cover} * m.gap_min) AS additional_starts,
  COUNT_IF(m.has_next AND NOT m.ran_through AND m.target_min >= {cover} * m.gap_min) AS avoided_starts,
  SUM(CASE WHEN NOT m.ran_through THEN GREATEST(0, LEAST(m.gap_min, m.target_min) - m.running_min) ELSE 0 END) AS added_idle_wall_minutes
FROM removable m JOIN cur c ON c.workspace_id = m.workspace_id AND c.warehouse_id = m.warehouse_id
GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, m.target_min
HAVING SUM(m.wall_min) > 0 OR SUM(m.cluster_min) > 0 OR COUNT_IF(m.has_next AND m.ran_through AND m.within_current AND m.target_min < {cover} * m.gap_min) > 0
  OR COUNT_IF(m.has_next AND NOT m.ran_through AND m.target_min >= {cover} * m.gap_min) > 0
  OR SUM(CASE WHEN NOT m.ran_through THEN GREATEST(0, LEAST(m.gap_min, m.target_min) - m.running_min) ELSE 0 END) > 0
ORDER BY c.workspace_id, c.warehouse_id, m.target_min
"""


RESOURCE_DAILY_COLLECT_DAYS = 120  # covers the 84-day window plus realistic billing lag; Python clips to what was collected


# Daily resource time for the current epoch over the last 120 UTC days (Python trims to the billing basis). Warehouse-event
# segments are split at midnight so every minute lands on its own UTC day; the peak excludes cluster-recycling transients
# (cluster_count above max_clusters), which are counted separately.
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
    DATE(d.day_start) AS usage_day,
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
)
SELECT dl.*, COALESCE(st.starts, 0) AS starts, DATE_SUB(CURRENT_DATE(), 120) AS collected_from
FROM daily dl LEFT JOIN starts st
  ON st.workspace_id = dl.workspace_id AND st.warehouse_id = dl.warehouse_id AND st.usage_day = dl.usage_day
ORDER BY dl.workspace_id, dl.warehouse_id, dl.usage_day
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)
