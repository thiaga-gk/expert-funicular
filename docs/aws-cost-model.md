# AWS cost model — v2.7.1

## Purpose
Expose customer AWS compute economics separately from Databricks DBU economics for Pro/Classic SQL warehouses.

## Formula
For the current Pro/Classic config epoch:

`annual AWS EC2 = annualized observed cluster-minutes / 60 × modeled cluster $/hour`

`cluster $/hour = driver instance $/hour + worker_count × i3.2xlarge $/hour`

Driver type and worker count come from Databricks SQL warehouse sizing documentation. Regional EC2 price comes from the versioned registry.

## Serverless
Serverless runs on Databricks-managed infrastructure, so the separate customer AWS EC2 line is `0`. Its infrastructure economics are reflected in the Serverless SKU/DBU price rather than a customer EC2 bill line.

## Regional registry
The shipped registry intentionally has no default-region fallback. Missing region => `AWS_PRICE_REGION_NOT_IN_REGISTRY`.

## Scope exclusions
The model does not include network/data transfer, public IPv4, support, tax, RI/Savings Plans, Spot or negotiated discounts. For production financial reconciliation, replace the price registry with organization-effective rates, preferably CUR-derived when available.
