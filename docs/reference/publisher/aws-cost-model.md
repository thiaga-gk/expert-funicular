# AWS cost model

## Purpose
Expose customer AWS compute economics separately from Databricks DBU economics for Pro/Classic SQL warehouses.

## Formula
For the current Pro/Classic config epoch:

`annual AWS EC2 = annualized recent cluster-minutes / 60 × modeled cluster $/hour`

Cluster-minutes come from the daily resource series for the current epoch, on the same window, calendar and 28/84-day blend as the DBU forecast (so DBU and AWS describe the same period), times the forecast trend adjustment and 365. Without the series the AWS baseline is null (`RECENT_RESOURCE_WINDOW_UNAVAILABLE`). For a periodic workload the DBU forecast uses whole billing cycles (cadence model, [LLD §11.2](../architecture/lld.md)); the cluster-minutes are then annualized over the same whole cycles inside event coverage (`annualized_resource_rates`), with no trend adjustment, and the AWS baseline is null (`CADENCE_CYCLES_NOT_COVERED_BY_EVENTS`) when events cover fewer than `forecast_min_cycles` cycles.

`cluster $/hour = driver instance $/hour + worker_count × i3.2xlarge $/hour`

Driver type and worker count come from Databricks SQL warehouse sizing documentation. Regional EC2 price comes from the versioned registry.

Billed node types (`usage_metadata.node_type`; driver and worker are not distinguished) must belong to that topology for the size. A node type outside it makes the AWS baseline null (`AWS_NODE_TYPE_MISMATCH`), because the modeled cluster would not describe the real one; `aws_cost_model.node_type_check` is `VERIFIED` when billed node types matched and `UNVERIFIED` when none were billed.

## Serverless
Serverless runs on Databricks-managed infrastructure, so the separate customer AWS EC2 line is `0`. Its infrastructure economics are reflected in the Serverless SKU/DBU price rather than a customer EC2 bill line.

## Regional registry
`pricing/aws_ec2_on_demand.json` is an explicit, versioned cost-model input: review and version it like code when prices change. The shipped example covers `us-east-1` I3 Linux/shared-tenancy on-demand rates, the topology Databricks documents for Pro/Classic SQL warehouses. Databricks DBU prices come from `system.billing.list_prices`, not this file.

The registry intentionally has no default-region fallback. Missing region => `AWS_PRICE_REGION_NOT_IN_REGISTRY`. A live run whose `--source-region` is not in the registry and that has a supported Pro/Classic warehouse stops after collection, before writing packages, evidence or the workbook; `--allow-missing-aws-price` publishes with Pro/Classic AWS cost unavailable. Serverless-only fleets and `--from-evidence` replays are not affected.

## Scope exclusions
The model does not include network/data transfer, public IPv4, support, tax, RI/Savings Plans, Spot or negotiated discounts. In particular every EC2 hour is priced **on demand**: the Warehouses API's `spot_instance_policy` (default `COST_OPTIMIZED`) is neither read nor modeled, so real Pro/Classic AWS cost, and the AWS part of any savings, can be materially lower. For production financial reconciliation, replace the price registry with organization-effective rates, preferably CUR-derived when available.
