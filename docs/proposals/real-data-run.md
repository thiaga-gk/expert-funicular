# Real-data run checklist

Items that cannot be closed from code or simulation alone. Each query is read-only and returns aggregates only (no
warehouse IDs, owner emails or SQL text), so its output can be pasted back for review. Until each is answered the
publisher fails safe for that item: the value is unknown, never guessed.

## Warehouse snapshot retention (tracker G4b)

Can 365-day retention of `system.compute.warehouses` drop the only snapshot of a warehouse that has not changed for
over a year? If `missing_from_warehouses_table > 0`, those warehouses are invisible to the publisher and would need to
be discovered from billing with an unknown configuration.

```sql
WITH billed AS (SELECT DISTINCT workspace_id, usage_metadata.warehouse_id AS warehouse_id FROM system.billing.usage
  WHERE billing_origin_product='SQL' AND usage_metadata.warehouse_id IS NOT NULL AND usage_date >= current_date() - 30),
snap AS (SELECT workspace_id, warehouse_id, MAX(change_time) AS last_change FROM system.compute.warehouses GROUP BY ALL)
SELECT COUNT(*) AS billed_warehouses, COUNT_IF(s.warehouse_id IS NULL) AS missing_from_warehouses_table,
       COUNT_IF(s.last_change < current_timestamp() - INTERVAL 365 DAYS) AS unchanged_over_365d
FROM billed b LEFT JOIN snap s USING (workspace_id, warehouse_id);
```

## SQL SKU names, tiers and prices (tracker G5)

Unlocks pricing a Pro/Classic to Serverless migration from the billed SKU's tier and region (golden C15), and the
`--source-region` cross-check against the region token of billed Serverless SKUs. Also note the AWS region passed as
`--source-region`.

```sql
SELECT sku_name, usage_unit, currency_code, pricing.default AS list_price FROM system.billing.list_prices
WHERE sku_name ILIKE '%SQL%' AND price_end_time IS NULL ORDER BY sku_name;

SELECT sku_name, product_features.sql_tier, product_features.is_serverless, COUNT(*) AS rows FROM system.billing.usage
WHERE billing_origin_product='SQL' AND usage_date >= current_date() - 30 GROUP BY ALL ORDER BY sku_name;
```

## Statement types (tracker S4)

The `statement_type` values are not documented. A sample lets metadata and keep-alive statements be excluded from
latency and idle evidence (golden C04, C21).

```sql
SELECT statement_type, COUNT(*) AS queries, PERCENTILE(total_duration_ms, 0.5) AS p50_ms, COUNT_IF(from_result_cache) AS cache_hits
FROM system.query.history WHERE compute.type = 'WAREHOUSE' AND start_time >= current_timestamp() - INTERVAL 7 DAYS
GROUP BY ALL ORDER BY queries DESC;
```

## Seasonal demand profile (tracker G45)

Deferred until the cadence-aware forecast window lands. Tuning needs 365 days of per-minute query starts from real
warehouses with known month-end, quarter-end or yearly peaks.

## Order of the run (after batch 8)

1. Run the three queries above and paste their output.
2. One live read-only publisher run on the account (`python publisher.py --profile <oauth-profile> --sql-warehouse-id <warehouse that runs the collector SQL> --source-region <aws-region> --output-dir <dir>`), keeping `evidence.json` for replay.
3. The backtest below on the same account.
4. Review together: which assumptions held, which policy values to recalibrate (each recalibration goes through its own approval).

## Backtest of the savings math (tracker G56)

For every warehouse whose configuration changed in the last 365 days, predict the new period's cost from the old
period's evidence and compare it with the cost actually billed in the new period. Older configuration periods are
allowed for calibration and backtest (CLAUDE.md); they never choose a change. Output per knob: predicted vs billed
annualized change, bias and spread. It calibrates the assumed values:

| Assumed value | Today | What the backtest measures |
|---|---|---|
| Auto-stop realization (optimistic/base/conservative) | 1.0 / 0.75 / 0.5 of the replayed idle saving | Billed change after past auto-stop changes vs the replay's prediction (example: C01 replay 124.1k removable, base reports 93.1k) |
| Size slowdown on busy time | x1.10 / x1.30 / x1.55 (worst case widened to x2.0 in batch 8) | Busy time and billed cost after past size changes (example: C06 LARGE to MEDIUM saves 36.6k at x1.0, 31.7k at x1.3, 20.4k at x2.0) |
| Floor realization | 1.0 / 0.5 / 0 of the cluster-minutes at the minimum | Billed change after past min-cluster changes |

## Assumptions to confirm (they move whole classes of numbers)

- **Restart billing:** are Pro/Classic starting minutes billed (about 4 minutes per restart at the floor cluster count)? Check billed DBU around warehouse STARTING events. Drives restart cost and every auto-stop raise.
- **Pro/Classic DBU per cluster-hour by size:** not published; the publisher infers it from billing and accepts it only within 25% of the published size ratio. Compare billed DBU per running cluster-hour by size.
- **Prices:** DBU dollars are list price (`system.billing.list_prices`); with a negotiated discount real savings are lower in the same proportion. AWS is on-demand (Spot, Reserved Instances and Savings Plans not modeled). Note the account's discount and AWS purchase options so the reported totals can be read correctly.
- **Serverless speed and price on this account:** compare a Serverless warehouse's billed DBU per busy hour with the Pro model's prediction, if the account runs both.

## Also confirm on the first real run

- Whether a warehouse with no state change emits any `system.compute.warehouse_events` row within 365 days (affects
  warehouses running all year with no event).
- `cluster_count` on STARTING/STOPPING events.
- Whether a tag or rename creates a `system.compute.warehouses` row.
