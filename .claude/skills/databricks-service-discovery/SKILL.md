---
name: databricks-service-discovery
description: Research and define the domain boundary and first-principles system model for a Databricks service before an optimization publisher is designed or implemented. Use this skill whenever a publisher effort needs to understand a Databricks resource, its variants, lifecycle, supported/excluded types, tunable configuration surface, neighboring services, resource identity, personas, GA/Beta/Preview status, fundamental cost/performance drivers, or open product decisions. Trigger it early for job clusters, Lakeflow/DLT, all-purpose clusters, SQL warehouses, serverless compute, or any unfamiliar Databricks service—even if the user only asks what is in scope or what should be optimized.
---

# Databricks Service Discovery

Act as a read-only domain research advisor. Do not edit publisher implementation code.

Read the sibling orchestrator's `references/artifact-protocol.md` and `references/optimization-thinking-framework.md`. Read `references/research-checklist.md` before researching.

## Procedure

1. Resolve `service_id`, `resource_noun`, and canonical Databricks product terminology.
2. Research current official Databricks documentation first; use release notes/current docs when service behavior can change.
3. Identify all resource variants/types and mark each `SUPPORTED`, `EXCLUDED`, or `RESEARCH_REQUIRED`. Record GA/Beta/Preview status.
4. Define stable resource identity keys and parent scopes (account/workspace/region/job/pipeline/etc.).
5. Build the **F1 first-principles system model**: workload/demand → configurable resource state → utilization/pressure → runtime/reliability/SLA → consumption → cost. Identify controllable vs non-controllable variables.
6. Define the product objective hypotheses and the constraints that must be protected. Do not optimize yet; record them for technique/policy design.
7. Map lifecycle/configuration changes that create a new decision regime or invalidate recommendations.
8. Inventory tunable configuration dimensions without deciding optimization policy yet.
9. Identify adjacent services/features that must remain outside scope to prevent product creep.
10. Identify target personas and the decisions they need from the publisher.
11. Record unresolved decisions with severity and a recommended owner. Never guess a material domain fact.
12. Write/update:
   - `discovery/service-profile.yaml`
   - `discovery/system-model.yaml`
   - `discovery/service-context.md`
   - `discovery/open-decisions.yaml`
13. Mark the artifacts `REVIEW_REQUIRED`; only the orchestrator/user moves them to `APPROVED`.

## Research pattern

When subagents are available, use parallel read-only advisors for:
- service/capability semantics;
- configuration/API semantics;
- system-table coverage;
- pricing/billing ownership;
- performance/reliability drivers and constraints;
- limitations/preview features.

Synthesize conflicts explicitly; do not vote by majority.

## System-model minimum

The system model must identify:
- demand/workload variables;
- configurable state;
- capacity/consumption variables;
- pressure/bottleneck variables;
- performance/reliability outputs;
- direct and indirect cost drivers;
- candidate hard constraints;
- observable evidence for each causal edge;
- major feedback loops / second-order hypotheses.

## Quality bar

Reject discovery that cannot answer:
- What exact thing receives a recommendation?
- How is it uniquely identified?
- Which variants are supported now and which fail closed?
- What fundamentally drives its cost and performance?
- What business/engineering outcome are we optimizing?
- What constraints must never be silently traded for savings?
- What constitutes a configuration regime change?
- Which adjacent optimization problems are intentionally excluded?
