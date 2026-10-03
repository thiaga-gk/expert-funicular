# Discovery Research Checklist

## Product semantics
- official product/resource name
- GA/Beta/Preview status
- resource types and migration paths
- configuration ownership and lifecycle
- service-specific autoscaling/serverless semantics

## Identity
- workspace/account/region scope
- immutable vs mutable identifiers
- parent-child relations (job/task/cluster, pipeline/update, endpoint/resource)

## Optimization boundary
- tunable dimensions
- constraints/policies
- settings that are observable but not safely tunable
- adjacent products that need separate analyzers/cost models

## Decision invalidation
List changes that make prior evidence incomparable: resource type, size/node family, autoscaling bounds, runtime, serverless migration, policy changes, major feature flags, etc.

## Evidence standard
Prefer official current Databricks docs/system-table schema docs. Record document date and URLs in `service-context.md`. Mark uncertain claims `RESEARCH_REQUIRED` rather than promoting them to requirements.
