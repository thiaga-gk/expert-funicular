# AWS EC2 price registry

`aws_ec2_on_demand.json` is an explicit, versioned cost-model input.

- No regional fallback is permitted.
- The shipped example covers `us-east-1` I3 Linux/shared-tenancy on-demand rates used by the Databricks-documented Pro/Classic SQL warehouse topology.
- If the collected workspace region is absent, AWS cost is reported as `UNAVAILABLE` rather than assuming `us-east-1`.
- For production effective-cost accounting, extend the registry for every region in scope and preferably replace on-demand list rates with organization-effective CUR-derived rates when CUR becomes available.
- Databricks DBU dollar prices come from `system.billing.list_prices`, not this file.
- Serverless direct customer EC2 cost is modeled as zero because Serverless SQL runs on Databricks-managed infrastructure; its compute economics are represented by the Serverless Databricks SKU.

The file is a model input and must be reviewed/versioned like code when prices change.
