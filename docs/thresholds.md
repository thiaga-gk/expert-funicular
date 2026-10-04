# Threshold posture — v2.9.0

The defaults are **balanced-to-conservative overall**. They are policy defaults, not universal Databricks recommendations.

| Decision | Current policy | Posture | Why |
|---|---|---|---|
| Photon observed state | >=90% non-null feature coverage, >=1 current-epoch DBU, <=14d freshness; >=95% true=ENABLED, <=5%=DISABLED, else MIXED | Conservative | Observation only (v2.7.1): reported in `current_config.photon`, never turned into a recommendation. Databricks publishes no Photon on/off price for SQL warehouses, so no savings can be modeled without guessing. |
| Size down | >=500 queries, <=0.1% spill, <=0.5% queued, p95<=5s, p99<=30s, epoch>=7d, one size only, canary required | Conservative | Multiple independent safety signals; never auto-applies. |
| Size up | spill >=2% of queries and p95 spill **among spilled queries** >=1 GiB, **and** >=20 spilled queries, >=100 queries and epoch >=7d; otherwise `INSUFFICIENT_EVIDENCE` with no resize recommended | Balanced/conservative | Dual frequency+severity signal avoids reacting to isolated spills; sample/maturity gate stops a single query or an immature config regime from driving a resize. |
| Max clusters up | >=5% queued + p95 capacity wait **among queued queries** >=30s + >=20 queued queries + observed saturation; fewer than 20 queued → `INSUFFICIENT_EVIDENCE` | Conservative | Triple gate avoids over-scaling. |
| Max clusters down | <=0.5% queue, <=1% time at max, observed max below config | Conservative | Guardrail only; no fake direct savings. |
| Min clusters down | >=100 queries, >=25% time at min, <=0.5% queue, p95 capacity wait among queued queries <=5s (unknown wait severity blocks), <20% time at max; **canary required (`TEST_REQUIRED`)**, savings are an upper-bound range (low 0% / base 50% / high 100% of the time-at-min cluster-minutes) | Conservative | Time at the floor shows the autoscaler sat at min, not that a floor cluster was spare; without utilization evidence the reduction is a canary candidate, not an actionable change. |
| Auto-stop | 15/30/45 min by activity; Serverless 10 min target; >=30 gaps; cold-start-sensitive workloads blocked | Balanced; most aggressive cost rule | Uses measured idle gaps, but guards against cold-start latency. |
| Type migration | compatibility gate + pre-canary modeled range + canary | Conservative | IWM/runtime response is not assumed. |
| Failure guard on capacity cuts | failed/cancelled share >5% of queries, or unreported, holds every capacity cut (size down, max/min clusters down, lower auto-stop) | Conservative | A cut on a warehouse that is already failing queries would add risk where the evidence is least trustworthy. Blockers `FAILURE_CANCEL_RATE_HIGH` / `FAILURE_CANCEL_EVIDENCE_UNAVAILABLE`. |
| Forward DBU forecast | published only when every basis day is priced, the current epoch has >=7 billed (positive-cost) days (`min_economics_cost_history_days`) and the run rate is positive; otherwise null (never $0), and savings estimates and type migration are withheld | Conservative | A run rate from a few days, or a floor at zero, would guess the baseline every savings figure scales. Blocker `COST_HISTORY_INSUFFICIENT` (epoch younger than 7 calendar days), `BILLED_COST_DAYS_INSUFFICIENT` (mature epoch with too few billed days) or `DBU_COST_UNAVAILABLE`. |

## Missing pressure evidence fails closed

Spill frequency, queue frequency and p95/p99 execution latency stay null when the collector did not report them; they are never read as 0 ("no pressure"), and findings report them as null.

- Size down is held (`NO_CHANGE`) with `SPILL_EVIDENCE_UNAVAILABLE`, `QUEUE_EVIDENCE_UNAVAILABLE` or `LATENCY_EVIDENCE_UNAVAILABLE`.
- Max-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`, or `QUEUE_WAIT_EVIDENCE_UNAVAILABLE` when queries queued but their capacity-wait percentile is missing.
- Min-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`.
- Size up and max-cluster increase need known spill / queue evidence.

## Severity is measured among affected queries

Spill, capacity-wait and compute-wait severity percentiles are computed **only over the queries that were affected** (`spilled_local_bytes > 0`, `waiting_at_capacity_duration_ms > 0`, `waiting_for_compute_duration_ms > 0`), and the affected-query counts (`spill_queries_30`, `queued_queries_30`, `compute_wait_queries_30`) are collected beside them. A percentile over all queries hides rare-but-severe events behind the unaffected majority.

- Rules that add capacity/cost (size up, max clusters up) require `severity_min_affected_queries` (20) affected queries; below that a p95 is just the maximum observation.
- Guard rules that block savings (min-cluster reduction queue guard, auto-stop cold-start guard) fire on any affected queries.
- If the collector did not report the affected-query count (older evidence), severity is *unknown*, never zero: min-cluster reduction and auto-stop are blocked (`QUEUE_WAIT_EVIDENCE_UNAVAILABLE`, `COLD_START_EVIDENCE_UNAVAILABLE`). Size-up and max-cluster increase report `INSUFFICIENT_EVIDENCE` (`SPILL_SEVERITY_EVIDENCE_UNAVAILABLE`, `QUEUE_WAIT_EVIDENCE_UNAVAILABLE`) when their frequency threshold is crossed but severity is unknown, instead of a silent no-change.

## Cost-estimator guardrail

Serverless size economics may use the published SQL Serverless DBU/hour schedule. Pro/Classic size economics must **not** reuse that schedule. v2.9.0 uses same-workspace observed Pro/Classic DBU-per-cluster-hour ratios when both sizes exist; otherwise it falls back to nominal size-capacity ratios and preserves the A/B canary requirement.

## Tuning recommendation

Keep these defaults for the first real run. Review false positives/negatives across 10–20 representative warehouses before changing them. Prefer SLA/workspace overrides over globally loosening safety gates.
