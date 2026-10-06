"""system.query.history SQL: per-epoch query metrics, spill, canary guards, carried periods and concurrency."""
from __future__ import annotations

from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_CTE
from publisher.config.defaults import SCALE_UP_LAG_MINUTES_PRO_CLASSIC, SCALE_UP_LAG_MINUTES_SERVERLESS
from publisher.governor.thresholds import Thresholds


EPOCH_QUERY_METRICS_SQL = r"""
WITH __CONFIG_EPOCHS__,
h AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
q AS (
  SELECT
    x.workspace_id,
    x.compute.warehouse_id AS warehouse_id,
    h.epoch_start,
    h.epoch_end,
    x.start_time,
    x.total_duration_ms,
    x.total_task_duration_ms,
    x.execution_duration_ms,
    x.waiting_for_compute_duration_ms,
    x.waiting_at_capacity_duration_ms,
    x.spilled_local_bytes,
    x.read_bytes,
    x.read_io_cache_percent,
    x.execution_status,
    x.from_result_cache,
    x.compilation_duration_ms,
    x.result_fetch_duration_ms,
    -- Decision population for latency and spill: queries not served from the result cache (cache hits take about
    -- 0 ms and would loosen the latency gates). Failed and cancelled queries stay in: a statement timeout is latency.
    COALESCE(x.from_result_cache, FALSE) = FALSE AS in_decision_population,
    x.query_tags,
    x.query_source,
    x.client_application,
    x.statement_text
  FROM system.query.history x
  JOIN h ON x.workspace_id = h.workspace_id
        AND x.compute.warehouse_id = h.warehouse_id
        AND x.start_time >= h.epoch_start
        AND (h.epoch_end IS NULL OR x.start_time < h.epoch_end)
  WHERE x.compute.type = 'WAREHOUSE'
    AND x.compute.warehouse_id IS NOT NULL
    AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
)
SELECT
  workspace_id,
  warehouse_id,
  epoch_start,
  COUNT(*) AS epoch_queries,
  COUNT(DISTINCT DATE(start_time)) AS epoch_active_days,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS) AS queries_30,
  COUNT(DISTINCT CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN DATE(start_time) END) AS active_days_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN total_duration_ms END, 0.95) AS p95_total_ms_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_for_compute_duration_ms,0) > 0) AS compute_wait_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND waiting_for_compute_duration_ms > 0 THEN waiting_for_compute_duration_ms END, 0.95) AS p95_wait_compute_ms_when_waited_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_at_capacity_duration_ms,0) > 0) AS queued_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND waiting_at_capacity_duration_ms > 0 THEN waiting_at_capacity_duration_ms END, 0.95) AS p95_wait_capacity_ms_when_queued_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population) AS decision_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END, 0.95) AS p95_execution_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END, 0.99) AS p99_execution_ms_30,
  MAX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END) AS max_execution_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN compilation_duration_ms END, 0.95) AS p95_compilation_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN result_fetch_duration_ms END, 0.95) AS p95_result_fetch_ms_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_at_capacity_duration_ms,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS queued_query_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND COALESCE(spilled_local_bytes,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population),0) AS spill_query_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND COALESCE(spilled_local_bytes,0) > 0) AS spill_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND spilled_local_bytes > 0 THEN spilled_local_bytes END,0.95) AS p95_spill_bytes_when_spilled_30,
  SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(spilled_local_bytes,0) ELSE 0 END)/POW(1024,3) AS total_spill_gib_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(read_bytes,0) END,0.95) AS p95_read_bytes_30,
  -- I/O cache share of bytes read (weighted by read_bytes), not a per-query average that small reads would dominate.
  SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND read_io_cache_percent IS NOT NULL THEN read_io_cache_percent * COALESCE(read_bytes,0) END)
    / NULLIF(SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND read_io_cache_percent IS NOT NULL THEN COALESCE(read_bytes,0) END),0) AS avg_read_io_cache_percent_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND execution_status IN ('FAILED','CANCELED'))
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS failure_cancel_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND from_result_cache = TRUE)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS result_cache_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND SIZE(query_tags) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS tagged_query_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND (query_source.dashboard_id IS NOT NULL OR query_source.legacy_dashboard_id IS NOT NULL)) AS dashboard_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.job_info.job_id IS NOT NULL) AS job_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.notebook_id IS NOT NULL) AS notebook_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.genie_space_id IS NOT NULL) AS genie_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.alert_id IS NOT NULL) AS alert_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.sql_query_id IS NOT NULL) AS sql_query_source_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%power bi%') AS power_bi_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%tableau%') AS tableau_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%looker%') AS looker_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%databricks sql editor%') AS databricks_sql_editor_client_queries_30,
  ARRAY_SORT(COLLECT_SET(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND client_application IS NOT NULL THEN client_application END)) AS client_applications_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND statement_text IS NOT NULL AND TRIM(statement_text) <> '' AND statement_text <> '<REDACTED>')
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS statement_text_visible_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(statement_text,'')) LIKE '%hive_metastore.%') AS hive_metastore_reference_queries_30,
  PERCENTILE_APPROX(execution_duration_ms,0.95) AS epoch_p95_execution_ms,
  100.0 * COUNT_IF(COALESCE(waiting_at_capacity_duration_ms,0) > 0)/NULLIF(COUNT(*),0) AS epoch_queued_query_pct,
  100.0 * COUNT_IF(COALESCE(spilled_local_bytes,0) > 0)/NULLIF(COUNT(*),0) AS epoch_spill_query_pct,
  SUM(total_task_duration_ms) AS epoch_task_duration_ms,
  -- Queries per complete UTC week (week 1 = the 7 days before today), null for a week not wholly inside the epoch:
  -- the growth guard compares the last 4 weeks with the 4 before.
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 0 DAYS) END AS queries_week_1,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS) END AS queries_week_2,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS) END AS queries_week_3,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS) END AS queries_week_4,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS) END AS queries_week_5,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS) END AS queries_week_6,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS) END AS queries_week_7,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 56 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 56 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS) END AS queries_week_8
FROM q
GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


# Spill concentration (current epoch, last 30 days): spilled bytes per statement source (saved query, dashboard, job,
# notebook, else a hash of the statement text; a redacted statement counts on its own, never merged with others) and the
# share carried by the top three sources. The statement text itself is never exported.
def build_spill_source_sql(t: Thresholds) -> str:
    """Spill concentration collector, with the number of top sources rendered from Thresholds."""
    return SPILL_SOURCE_SQL_TEMPLATE.replace("__TOP_SOURCES__", str(int(t.spill_concentration_top_sources)))


SPILL_SOURCE_SQL_TEMPLATE = r"""
WITH __CONFIG_EPOCHS__,
cur AS (SELECT workspace_id, warehouse_id, epoch_start FROM config_epochs WHERE epoch_end IS NULL),
src AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start,
    COALESCE(x.query_source.sql_query_id, x.query_source.dashboard_id, x.query_source.legacy_dashboard_id,
      CAST(x.query_source.job_info.job_id AS STRING), x.query_source.notebook_id,
      CASE WHEN x.statement_text IS NULL OR x.statement_text = '<REDACTED>' THEN x.statement_id ELSE SHA2(x.statement_text, 256) END) AS source_key,
    SUM(CAST(x.spilled_local_bytes AS DOUBLE)) AS spilled_bytes
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id AND x.start_time >= c.epoch_start
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
    AND COALESCE(x.from_result_cache, FALSE) = FALSE AND x.spilled_local_bytes > 0
  GROUP BY 1, 2, 3, 4
), ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, epoch_start ORDER BY spilled_bytes DESC, source_key) AS rn,
    SUM(spilled_bytes) OVER (PARTITION BY workspace_id, warehouse_id, epoch_start) AS total_bytes
  FROM src
)
SELECT workspace_id, warehouse_id, epoch_start, COUNT(*) AS spill_sources_30,
  100.0 * SUM(CASE WHEN rn <= __TOP_SOURCES__ THEN spilled_bytes ELSE 0 END) / MAX(total_bytes) AS top_spill_source_share_pct_30
FROM ranked GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


# Fail-fast canary guard, for a current epoch at most 30 days old (the canary window): its queries against the
# immediately preceding epoch's last 30 days, on the
# same statement sources (saved query, dashboard, job, notebook, else a hash of the statement text; a redacted statement
# counts on its own). The statement text itself is never exported.
CANARY_GUARD_FIELDS = ("queries", "failure_cancel_pct", "queued_queries", "queued_query_pct", "decision_queries", "spill_queries",
                       "spill_query_pct", "shared_source_queries", "p95_execution_ms_shared_sources")


CANARY_GUARD_SQL = r"""
WITH __CONFIG_EPOCHS__,
cur AS (SELECT workspace_id, warehouse_id, epoch_start FROM config_epochs c WHERE epoch_end IS NULL AND c.epoch_start >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),
prv AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, h.epoch_start AS prev_start, h.epoch_end AS prev_end
  FROM cur c JOIN config_epochs h ON h.workspace_id = c.workspace_id AND h.warehouse_id = c.warehouse_id AND h.epoch_end = c.epoch_start
), q AS (
  SELECT p.workspace_id, p.warehouse_id, p.epoch_start,
    CASE WHEN x.start_time >= p.epoch_start THEN 'CURRENT' ELSE 'PREVIOUS' END AS period,
    COALESCE(x.query_source.sql_query_id, x.query_source.dashboard_id, x.query_source.legacy_dashboard_id,
      CAST(x.query_source.job_info.job_id AS STRING), x.query_source.notebook_id,
      CASE WHEN x.statement_text IS NULL OR x.statement_text = '<REDACTED>' THEN x.statement_id ELSE SHA2(x.statement_text, 256) END) AS source_key,
    x.execution_duration_ms, x.execution_status,
    COALESCE(x.waiting_at_capacity_duration_ms, 0) > 0 AS queued,
    COALESCE(x.from_result_cache, FALSE) = FALSE AS executed,
    COALESCE(x.spilled_local_bytes, 0) > 0 AS spilled
  FROM system.query.history x
  JOIN prv p ON x.workspace_id = p.workspace_id AND x.compute.warehouse_id = p.warehouse_id
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 60 DAYS
    AND ((x.start_time >= p.epoch_start AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS)
      OR (x.start_time >= p.prev_start AND x.start_time < p.prev_end AND x.start_time >= p.prev_end - INTERVAL 30 DAYS))
), shared AS (
  SELECT workspace_id, warehouse_id, source_key FROM q WHERE executed GROUP BY 1, 2, 3 HAVING COUNT(DISTINCT period) = 2
)
SELECT q.workspace_id, q.warehouse_id, q.epoch_start, q.period,
  COUNT(*) AS queries,
  100.0 * COUNT_IF(q.execution_status IN ('FAILED','CANCELED')) / NULLIF(COUNT(*), 0) AS failure_cancel_pct,
  COUNT_IF(q.queued) AS queued_queries,
  100.0 * COUNT_IF(q.queued) / NULLIF(COUNT(*), 0) AS queued_query_pct,
  COUNT_IF(q.executed) AS decision_queries,
  COUNT_IF(q.executed AND q.spilled) AS spill_queries,
  100.0 * COUNT_IF(q.executed AND q.spilled) / NULLIF(COUNT_IF(q.executed), 0) AS spill_query_pct,
  COUNT_IF(q.executed AND s.source_key IS NOT NULL) AS shared_source_queries,
  PERCENTILE_APPROX(CASE WHEN q.executed AND s.source_key IS NOT NULL THEN q.execution_duration_ms END, 0.95) AS p95_execution_ms_shared_sources
FROM q LEFT JOIN shared s ON q.workspace_id = s.workspace_id AND q.warehouse_id = s.warehouse_id AND q.source_key = s.source_key
GROUP BY q.workspace_id, q.warehouse_id, q.epoch_start, q.period
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


# Carry periods: evidence a configuration change cannot affect is read across that change, inside the last 30 days. The
# demand period starts at the last size, type or channel change (query demand and per-query evidence); the capacity
# period at the last size, type, channel, min- or max-cluster change (capacity queueing). Only a current epoch younger
# than 30 days carries (otherwise its own 30 days are the whole window). Running time, idle time and billing never carry.
PERIODS_CTE = r"""
periods AS (
  SELECT *,
    MAX(CASE WHEN demand_break THEN epoch_start END) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY epoch_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS demand_period_start,
    MAX(CASE WHEN capacity_break THEN epoch_start END) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY epoch_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS capacity_period_start
  FROM (
    SELECT *,
      NOT (COALESCE(LAG(warehouse_type) OVER w <=> warehouse_type, FALSE) AND COALESCE(LAG(warehouse_size) OVER w <=> warehouse_size, FALSE)
           AND COALESCE(LAG(warehouse_channel) OVER w <=> warehouse_channel, FALSE) AND LAG(epoch_start) OVER w IS NOT NULL) AS demand_break,
      NOT (COALESCE(LAG(warehouse_type) OVER w <=> warehouse_type, FALSE) AND COALESCE(LAG(warehouse_size) OVER w <=> warehouse_size, FALSE)
           AND COALESCE(LAG(warehouse_channel) OVER w <=> warehouse_channel, FALSE)
           AND COALESCE(LAG(min_clusters) OVER w <=> min_clusters, FALSE) AND COALESCE(LAG(max_clusters) OVER w <=> max_clusters, FALSE)
           AND LAG(epoch_start) OVER w IS NOT NULL) AS capacity_break
    FROM config_epochs
    WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY epoch_start)
  ) b
), cur_period AS (
  SELECT workspace_id, warehouse_id, epoch_start AS cur_start, demand_period_start, capacity_period_start FROM periods WHERE epoch_end IS NULL
)"""


CARRY_PERIOD_COLUMNS = {"demand": "demand_period_start", "capacity": "capacity_period_start"}


def build_carried_query_metrics_sql(family: str) -> str:
    """The query-metrics collector over a carry period of the current epoch (rows only when the period started earlier)."""
    col = CARRY_PERIOD_COLUMNS[family]
    sql = EPOCH_QUERY_METRICS_SQL
    for old, new in (
        ("h AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),",
         PERIODS_CTE.strip() + ",\nh AS (SELECT p.workspace_id, p.warehouse_id, c." + col + " AS epoch_start, p.epoch_start AS seg_start, p.epoch_end, c.cur_start "
         "FROM periods p JOIN cur_period c ON p.workspace_id = c.workspace_id AND p.warehouse_id = c.warehouse_id AND p." + col + " = c." + col
         + " WHERE c." + col + " < c.cur_start AND c.cur_start > CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),"),
        ("AND x.start_time >= h.epoch_start\n", "AND x.start_time >= h.seg_start\n"),
        ("    h.epoch_start,\n    h.epoch_end,\n", "    h.epoch_start,\n    h.epoch_end,\n    h.cur_start,\n"),
        ("SELECT\n  workspace_id,\n  warehouse_id,\n  epoch_start,\n  COUNT(*) AS epoch_queries,",
         "SELECT\n  workspace_id,\n  warehouse_id,\n  cur_start,\n  epoch_start AS period_start,\n  COUNT(*) AS epoch_queries,"),
        ("GROUP BY workspace_id, warehouse_id, epoch_start\n", "GROUP BY workspace_id, warehouse_id, cur_start, epoch_start\n"),
    ):
        if sql.count(old) != 1:
            raise ValueError(f"carried query metrics: template drifted at {old[:40]!r}")
        sql = sql.replace(old, new)
    return sql


def build_carried_spill_source_sql(t: Thresholds) -> str:
    """Spill concentration over the current epoch's demand period (rows only when it started earlier)."""
    sql = build_spill_source_sql(t)
    for old, new in (
        ("cur AS (SELECT workspace_id, warehouse_id, epoch_start FROM config_epochs WHERE epoch_end IS NULL),",
         PERIODS_CTE.strip() + ",\ncur AS (SELECT workspace_id, warehouse_id, cur_start AS epoch_start, demand_period_start AS seg_start FROM cur_period "
         "WHERE demand_period_start < cur_start AND cur_start > CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),"),
        ("AND x.start_time >= c.epoch_start\n", "AND x.start_time >= c.seg_start\n"),
    ):
        if sql.count(old) != 1:
            raise ValueError(f"carried spill sources: template drifted at {old[:40]!r}")
        sql = sql.replace(old, new)
    return sql


def build_carried_concurrency_sql() -> str:
    """RECENT and BUSY_START concurrency histograms over the current epoch's demand period (last 30 days), keyed by the
    current epoch; same arithmetic as ``build_concurrency_sql``, with minutes merged across the period's epochs."""
    return f"""
WITH {CONFIG_EPOCHS_CTE},
{PERIODS_CTE.strip()},
ep AS (
  SELECT p.workspace_id, p.warehouse_id, c.cur_start, p.epoch_start,
    GREATEST(p.epoch_start, CURRENT_TIMESTAMP() - INTERVAL 30 DAYS) AS e_start, COALESCE(p.epoch_end, CURRENT_TIMESTAMP()) AS e_end,
    CASE WHEN p.warehouse_type = 'SERVERLESS' THEN {SCALE_UP_LAG_MINUTES_SERVERLESS} ELSE {SCALE_UP_LAG_MINUTES_PRO_CLASSIC} END AS lag_min
  FROM periods p JOIN cur_period c ON p.workspace_id = c.workspace_id AND p.warehouse_id = c.warehouse_id AND p.demand_period_start = c.demand_period_start
  WHERE c.demand_period_start < c.cur_start AND c.cur_start > CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
    AND COALESCE(p.epoch_end, CURRENT_TIMESTAMP()) > CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
), q0 AS (
  SELECT p.workspace_id, p.warehouse_id, p.cur_start, p.lag_min,
    GREATEST(x.start_time, p.e_start) AS s,
    LEAST(COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time),
      CASE WHEN x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 1 DAY THEN CURRENT_TIMESTAMP() ELSE x.start_time END), CURRENT_TIMESTAMP()) AS t
  FROM system.query.history x
  JOIN ep p ON x.workspace_id = p.workspace_id AND x.compute.warehouse_id = p.warehouse_id
    AND x.start_time >= p.epoch_start AND x.start_time < p.e_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 31 DAYS
), q AS (
  SELECT * FROM q0 WHERE t > s
), qm AS (
  SELECT q.workspace_id, q.warehouse_id, q.cur_start, q.lag_min, m AS minute_start,
    TIMESTAMPDIFF(MILLISECOND, GREATEST(q.s, m), LEAST(q.t, TIMESTAMPADD(MINUTE, 1, m))) AS ms
  FROM q LATERAL VIEW EXPLODE(SEQUENCE(DATE_TRUNC('MINUTE', q.s), DATE_TRUNC('MINUTE', TIMESTAMPADD(MILLISECOND, -1, q.t)), INTERVAL 1 MINUTE)) mm AS m
), recent AS (
  SELECT *, CASE WHEN LAG(minute_start) OVER w IS NULL
      OR TIMESTAMPDIFF(MINUTE, LAG(minute_start) OVER w, minute_start) > lag_min THEN 1 ELSE 0 END AS starts_period
  FROM (SELECT workspace_id, warehouse_id, cur_start, lag_min, minute_start, CAST(CEIL(SUM(ms) / 60000.0) AS INT) AS level
        FROM qm GROUP BY workspace_id, warehouse_id, cur_start, lag_min, minute_start HAVING SUM(ms) > 0) m
  WHERE minute_start >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start)
), periods_c AS (
  SELECT *, SUM(starts_period) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS period_id
  FROM recent
), period_peaks AS (
  SELECT workspace_id, warehouse_id, cur_start, period_id, MAX(level) AS level
  FROM (SELECT *, MIN(minute_start) OVER (PARTITION BY workspace_id, warehouse_id, period_id) AS period_start FROM periods_c) x
  WHERE minute_start < TIMESTAMPADD(MINUTE, lag_min, period_start)
  GROUP BY workspace_id, warehouse_id, cur_start, period_id
)
SELECT workspace_id, warehouse_id, cur_start AS epoch_start, 'RECENT' AS scope, level, COUNT(*) AS weight
FROM recent GROUP BY workspace_id, warehouse_id, cur_start, level
UNION ALL
SELECT workspace_id, warehouse_id, cur_start AS epoch_start, 'BUSY_START' AS scope, level, COUNT(*) AS weight
FROM period_peaks GROUP BY workspace_id, warehouse_id, cur_start, level
ORDER BY workspace_id, warehouse_id, epoch_start, scope, level
"""


CONCURRENCY_SCOPES = ("RECENT", "EPOCH", "BUSY_START")


def build_concurrency_sql() -> str:
    """Per-minute query concurrency (running + queued, time-weighted) as histograms per epoch (last 365 days).

    Same arithmetic as ``concurrency_histograms``: EPOCH for every epoch, RECENT (last 30 days) and BUSY_START for the
    current epoch. A query occupies [start_time, end_time); milliseconds keep sub-second queries.
    """
    return f"""
WITH {CONFIG_EPOCHS_CTE},
ep AS (
  SELECT workspace_id, warehouse_id, epoch_start, epoch_end, warehouse_type,
    GREATEST(epoch_start, CURRENT_TIMESTAMP() - INTERVAL 365 DAYS) AS e_start, COALESCE(epoch_end, CURRENT_TIMESTAMP()) AS e_end,
    CASE WHEN warehouse_type = 'SERVERLESS' THEN {SCALE_UP_LAG_MINUTES_SERVERLESS} ELSE {SCALE_UP_LAG_MINUTES_PRO_CLASSIC} END AS lag_min
  FROM config_epochs
  WHERE COALESCE(epoch_end, CURRENT_TIMESTAMP()) > CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), q0 AS (
  SELECT p.workspace_id, p.warehouse_id, p.epoch_start, p.epoch_end, p.lag_min,
    GREATEST(x.start_time, p.e_start) AS s,
    LEAST(COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time),
      CASE WHEN x.start_time >= p.e_end - INTERVAL 1 DAY THEN p.e_end ELSE x.start_time END), p.e_end) AS t
  FROM system.query.history x
  JOIN ep p ON x.workspace_id = p.workspace_id AND x.compute.warehouse_id = p.warehouse_id
    AND x.start_time >= p.epoch_start AND x.start_time < p.e_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 366 DAYS
), q AS (
  -- Zero-length spans are dropped before the minute explode (SEQUENCE would be empty or reversed).
  SELECT * FROM q0 WHERE t > s
), qm AS (
  SELECT q.workspace_id, q.warehouse_id, q.epoch_start, q.epoch_end, q.lag_min, m AS minute_start,
    TIMESTAMPDIFF(MILLISECOND, GREATEST(q.s, m), LEAST(q.t, TIMESTAMPADD(MINUTE, 1, m))) AS ms
  FROM q LATERAL VIEW EXPLODE(SEQUENCE(DATE_TRUNC('MINUTE', q.s), DATE_TRUNC('MINUTE', TIMESTAMPADD(MILLISECOND, -1, q.t)), INTERVAL 1 MINUTE)) mm AS m
), minutes AS (
  SELECT workspace_id, warehouse_id, epoch_start, epoch_end, lag_min, minute_start, CAST(CEIL(SUM(ms) / 60000.0) AS INT) AS level
  FROM qm GROUP BY workspace_id, warehouse_id, epoch_start, epoch_end, lag_min, minute_start
  HAVING SUM(ms) > 0
), recent AS (
  SELECT *, CASE WHEN LAG(minute_start) OVER w IS NULL
      OR TIMESTAMPDIFF(MINUTE, LAG(minute_start) OVER w, minute_start) > lag_min THEN 1 ELSE 0 END AS starts_period
  FROM minutes
  WHERE epoch_end IS NULL AND minute_start >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start)
), periods AS (
  SELECT *, SUM(starts_period) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS period_id
  FROM recent
), period_peaks AS (
  -- Each busy period's highest level in its first lag minutes: demand the autoscaler cannot meet above the floor yet.
  SELECT workspace_id, warehouse_id, epoch_start, period_id, MAX(level) AS level
  FROM (SELECT *, MIN(minute_start) OVER (PARTITION BY workspace_id, warehouse_id, period_id) AS period_start FROM periods) x
  WHERE minute_start < TIMESTAMPADD(MINUTE, lag_min, period_start)
  GROUP BY workspace_id, warehouse_id, epoch_start, period_id
)
SELECT workspace_id, warehouse_id, epoch_start, 'EPOCH' AS scope, level, COUNT(*) AS weight
FROM minutes GROUP BY workspace_id, warehouse_id, epoch_start, level
UNION ALL
SELECT workspace_id, warehouse_id, epoch_start, 'RECENT' AS scope, level, COUNT(*) AS weight
FROM recent GROUP BY workspace_id, warehouse_id, epoch_start, level
UNION ALL
SELECT workspace_id, warehouse_id, epoch_start, 'BUSY_START' AS scope, level, COUNT(*) AS weight
FROM period_peaks GROUP BY workspace_id, warehouse_id, epoch_start, level
ORDER BY workspace_id, warehouse_id, epoch_start, scope, level
"""
