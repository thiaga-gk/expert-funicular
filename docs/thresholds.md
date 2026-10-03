# Threshold posture — v2.7.0

The defaults are **balanced-to-conservative overall**. They are policy defaults, not universal Databricks recommendations.

| Decision | Current policy | Posture | Why |
|---|---|---|---|
| Photon observed state | >=90% non-null feature coverage, >=1 current-epoch DBU, <=14d freshness; >=95% true=ENABLED, <=5%=DISABLED, else MIXED | Conservative | Refuses to turn sparse/stale/mixed billing attribution into a config assertion. Disabled→enabled is canary-gated and no savings is fabricated before measurement. |
| Size down | >=500 queries, <=0.1% spill, <=0.5% queued, p95<=5s, p99<=30s, epoch>=7d, one size only, canary required | Conservative | Multiple independent safety signals; never auto-applies. |
| Size up | spill >=2% and p95 spill >=1 GiB | Balanced/conservative | Dual frequency+severity signal avoids reacting to isolated spills. |
| Max clusters up | >=5% queued + p95 capacity wait >=30s + observed saturation | Conservative | Triple gate avoids over-scaling. |
| Max clusters down | <=0.5% queue, <=1% time at max, observed max below config | Conservative | Guardrail only; no fake direct savings. |
| Min clusters down | >=100 queries, >=25% time at min, <=0.5% queue, p95 capacity wait<=5s, <20% time at max | Balanced/conservative | Uses actual resource-state evidence and capacity-pressure guardrails. |
| Auto-stop | 15/30/45 min by activity; Serverless 10 min target; >=30 gaps; cold-start-sensitive workloads blocked | Balanced; most aggressive cost rule | Uses measured idle gaps, but guards against cold-start latency. |
| Type migration | compatibility gate + pre-canary modeled range + canary | Conservative | IWM/runtime response is not assumed. |

## Cost-estimator guardrail

Serverless size economics may use the published SQL Serverless DBU/hour schedule. Pro/Classic size economics must **not** reuse that schedule. v2.7.0 uses same-workspace observed Pro/Classic DBU-per-cluster-hour ratios when both sizes exist; otherwise it falls back to nominal size-capacity ratios and preserves the A/B canary requirement.

## Tuning recommendation

Keep these defaults for the first real run. Review false positives/negatives across 10–20 representative warehouses before changing them. Prefer SLA/workspace overrides over globally loosening safety gates.
