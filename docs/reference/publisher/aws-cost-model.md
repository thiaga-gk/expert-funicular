# AWS cost model

## Purpose
Expose customer AWS compute economics separately from Databricks DBU economics for Pro/Classic SQL warehouses.

## Formula
For the current Pro/Classic config epoch:

`annual AWS EC2 = annualized recent cluster-minutes / 60 × modeled cluster $/hour`

Cluster-minutes come from the daily resource series for the current epoch, on the same window, calendar and 28/84-day blend as the DBU forecast (so DBU and AWS describe the same period), times the forecast trend adjustment and 365. Without the series the AWS baseline is null (`RECENT_RESOURCE_WINDOW_UNAVAILABLE`). For a periodic workload the DBU forecast uses whole billing cycles (cadence model, [LLD §11.2](../../architecture/publisher/lld.md)); the cluster-minutes are then annualized over the same whole cycles inside event coverage (`annualized_resource_rates`), with no trend adjustment, and the AWS baseline is null (`CADENCE_CYCLES_NOT_COVERED_BY_EVENTS`) when events cover fewer than `forecast_min_cycles` cycles.

`cluster $/hour = driver instance $/hour + worker_count × i3.2xlarge $/hour`

Driver type and worker count come from Databricks SQL warehouse sizing documentation. Regional EC2 price comes from the versioned registry.

Billed node types (`usage_metadata.node_type`; driver and worker are not distinguished) must belong to that topology for the size. A node type outside it makes the AWS baseline null (`AWS_NODE_TYPE_MISMATCH`), because the modeled cluster would not describe the real one; `aws_cost_model.node_type_check` is `VERIFIED` when billed node types matched and `UNVERIFIED` when none were billed.

## Trailing actual
The package's trailing-365-day actual (`cost_baseline.actual_365d.aws_cost`) models customer AWS over every epoch in the trailing window, not only the current one, because the warehouse's size may have changed inside it. Billing records DBU, not hours, so each billing day's cluster-hours are recovered from its DBU:

`trailing AWS EC2 = Σ over billing days (billed DBU / published DBU per cluster-hour of the day's size × cluster $/hour of that size)`

- The window is the trailing DBU actual's: from the analysis date minus 365 days through the last settled billing day (`billing_complete_through`).
- The day's size is the size of the config epoch its billing row belongs to; the DBU rate is the published SQL warehouse rate per size (`SQL_DBU_PER_CLUSTER_HOUR`), the same for Pro, Classic and Serverless.
- A running cluster accrues its DBU, so a multi-cluster warehouse's DBU is already cluster-hours times the rate; unpriced DBU counts too (the cluster ran even when its DBU price is unknown).
- Serverless days add 0, so a Serverless warehouse that was Pro or Classic earlier in the window still carries the modeled AWS of those days.
- The figure is null (unknown, never zero) when any contributing day cannot be priced: a billing row without a quantity or outside every known epoch, a size without a published rate or topology, a missing regional price, billed node types outside the topology (`AWS_NODE_TYPE_MISMATCH`), a price registry currency that differs from the billing currency, or a negative net.

The rate assumption (billed DBU per cluster-hour equals the published size rate) is the same one the size estimator validates against billed rates; a warehouse billed at another rate would model proportionally more or fewer hours. The figure is a model, not an AWS bill: reconcile it with CUR like the forward baseline.

## Serverless
Serverless runs on Databricks-managed infrastructure, so the separate customer AWS EC2 line is `0`. Its infrastructure economics are reflected in the Serverless SKU/DBU price rather than a customer EC2 bill line.

## Regional registry
`pricing/aws_ec2_on_demand.json` is an explicit, versioned cost-model input: review and version it like code when prices change. The shipped example covers `us-east-1` I3 Linux/shared-tenancy on-demand rates, the topology Databricks documents for Pro/Classic SQL warehouses. Databricks DBU prices come from `system.billing.list_prices`, not this file.

The registry intentionally has no default-region fallback. Missing region => `AWS_PRICE_REGION_NOT_IN_REGISTRY`. A live run whose `--source-region` is not in the registry and that has a supported Pro/Classic warehouse stops after collection, before writing packages or the workbook (the collected `evidence.json` is already saved and replays with `--from-evidence`); `--allow-missing-aws-price` publishes with Pro/Classic AWS cost unavailable. Serverless-only fleets and `--from-evidence` replays are not affected.

## Scope exclusions
The model does not include network/data transfer, public IPv4, support, tax, RI/Savings Plans, Spot or negotiated discounts. In particular every EC2 hour is priced **on demand**: the Warehouses API's `spot_instance_policy` (default `COST_OPTIMIZED`) is neither read nor modeled, so real Pro/Classic AWS cost, and the AWS part of any savings, can be materially lower. For production financial reconciliation, replace the price registry with organization-effective rates, preferably CUR-derived when available.
