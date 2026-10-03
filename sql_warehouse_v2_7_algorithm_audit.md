# SQL Warehouse Publisher v2.7.0 — Optimization & Cost Algorithm Audit

Audit scope: `publisher.py` in the sealed v2.7.0 baseline. Focus: optimizer decisions, evidence metrics, independent candidate estimates, stateful scenario economics, baseline forecast, AWS cost model, confidence, and migration models.

## Overall result

- **Critical/P0 defects:** 0
- **High-priority confirmed issues:** 4
- **Medium-priority modeling/algorithm issues:** 8
- **Existing regression suite:** 71/71 passes
- **Conclusion:** architecture is sound and read-only operation is safe, but several outputs can be materially misleading until the high-priority items are fixed.

## High-priority confirmed issues

### H1 — Plan A prices steps already marked `PENDING_REEVALUATION`

**Code:** `ScenarioPlanner.scenarios` (~2533–2547), `StatefulBundleEstimator.estimate` (~2410–2479).

The planner correctly marks downstream knobs `PENDING_REEVALUATION` after an upstream invalidating change, but the bundle estimator still executes their pre-change effects when computing Plan A economics.

Reproduced with the standard BI-Sales synthetic fixture:

- Step 1 size: `READY_FOR_CANARY`, running savings base ≈ $12,029
- Step 2 max clusters: `PENDING_REEVALUATION`
- Step 3 min clusters: `PENDING_REEVALUATION`, running savings base rises to ≈ $18,190
- Step 4 auto-stop: `PENDING_REEVALUATION`, running savings base rises to ≈ $21,240

Thus the published package base includes economics for recommendations that the execution plan says must be regenerated first.

**Impact:** material portfolio/package savings overstatement and violation of the intended second-order semantics.

**Recommended fix:** stop authoritative scenario pricing at the first invalidating/canary step. Mark later economics `CONTINGENT_UNPRICED` until remeasurement, or expose a separately labeled contingent-upside range that is never used as package/recommended base.

---

### H2 — Size-up can become `ACTIONABLE` with insufficient evidence

**Code:** size optimizer ~1851–1864.

Upsize requires only:

- spill frequency >= 2%
- p95 spill >= 1 GiB

It does **not** require a minimum query sample or a mature/stable config epoch. Downsize does require both.

Reproduced:

- 1 query in 30d, 100% spill, 2 GiB p95 => `ACTIONABLE LARGE -> X_LARGE`, no blockers.
- Very new current epoch with high spill => still `ACTIONABLE`.

**Impact:** false-positive performance changes from isolated queries or immature regimes.

**Recommended fix:** require minimum sample and epoch maturity for `ACTIONABLE`; otherwise use `TEST_REQUIRED` or `INSUFFICIENT_EVIDENCE`. Consider workload/source segmentation so one pathological query class does not resize a shared warehouse by itself.

---

### H3 — Spill/queue/cold-start severity percentiles are computed over all queries, not affected queries

**Code:** `EPOCH_QUERY_METRICS_SQL` ~600–610.

Examples:

```sql
PERCENTILE_APPROX(waiting_at_capacity_duration_ms, 0.95)
PERCENTILE_APPROX(COALESCE(spilled_local_bytes,0), 0.95)
PERCENTILE_APPROX(waiting_for_compute_duration_ms, 0.95)
```

Because zero-valued unaffected queries remain in the percentile population, rare-but-severe events are hidden.

Examples:

- 3% of queries spill 10 GiB; 97% spill 0 => all-query p95 spill is 0, even though the policy threshold says 2% spill frequency is enough to consider upsizing.
- ~5% queue incidence can still yield a zero/unstable all-query p95 wait at the exact threshold boundary.
- Cold-start waits affecting only the first query after warehouse startup can disappear in all-query p95 compute-wait.

**Impact:** under-detection of real memory, concurrency, and cold-start pressure. The current frequency+severity rules do not mean what their names imply.

**Recommended fix:** collect incidence and conditional severity separately, e.g. p95 spill among queries where spill > 0, p95 capacity wait among queued queries, p95 compute wait among queries with compute wait > 0.

---

### H4 — Min-cluster savings model treats `time_at_min` as removable idle capacity

**Code:** min-cluster optimizer ~1900s; estimator `_factors` min-cluster branch ~2040s.

The estimator computes:

```text
cluster_minutes_to_remove = minutes_at_min * (current_min - target_min)
```

But `cluster_count == min_clusters` proves only that the autoscaler was at the configured floor. It does not prove one of those clusters was unnecessary. Demand might have required every cluster at the floor.

The standard BI-Sales fixture produces a raw savings fraction of 40% and an independent annual savings range of roughly $7.7K–$15.5K for min 2 -> 1 based largely on time-at-min evidence.

**Impact:** materially optimistic savings and an `ACTIONABLE` recommendation without a causal utilization measure.

**Recommended fix:** change min-cluster reduction to `TEST_REQUIRED` unless stronger evidence proves spare floor capacity. Treat the existing estimate as an upper bound, not a base counterfactual, or add concurrency/resource-utilization evidence and post-change validation.

## Medium-priority issues / modeling risks

### M1 — Auto-stop savings do not intersect idle gaps with actual RUNNING intervals

**Code:** `build_epoch_gap_metrics_sql` ~709–750; auto-stop estimator ~2030s.

The model infers removable idle time from inter-query gaps and configured auto-stop. If a warehouse was manually/scheduled-stopped earlier than the configured timeout, the model can credit minutes that were never billed. `auto_stop=0` is especially vulnerable to long inter-query gaps containing manually stopped periods.

**Fix:** intersect query-idle gaps with warehouse RUNNING segments / observed billed cluster-minutes before claiming removable time.

### M2 — Recommendation windows and cost/resource windows are inconsistent

Query pressure decisions mostly use 30 days, while event/min/gap/resource metrics use the entire current epoch (up to 365d). The DBU forecast weights recent 28/84 days, while AWS annualization uses full current-epoch cluster-minutes times a single trend factor.

**Impact:** if workload behavior changed without a warehouse config change, recent recommendation signals can be paired with stale resource/economic behavior.

**Fix:** add aligned recent 30/84-day event/gap/resource metrics or explicit workload-regime detection.

### M3 — Max-cluster saturation logic can be confounded by cluster recycling and has weak reduction headroom

**Code:** max-cluster optimizer ~1880s.

Increase requires queue pressure plus `max_observed >= configured max`, but those facts are not temporally correlated. Databricks can temporarily exceed configured max during cluster recycling. Reduction uses an absolute observed peak and can set the new max equal to the historical observed maximum (for example max 4, observed 3 -> target 3), leaving no explicit growth headroom.

**Fix:** use time-at-max / saturation duration and temporal correlation with queue pressure; exclude recycle transitions; define a headroom policy for max reduction.

### M4 — Historical size calibration matches config but not workload

**Code:** `_matched_epoch_factor` ~2001–2024.

The model can blend 50% historical `cost per 1,000 queries` from a target-size epoch when all other config knobs match. It does not require similar query mix, bytes read, concurrency, data volume, cache behavior, or workload source.

**Impact:** non-causal historical workload shifts can bias a size counterfactual.

**Fix:** require workload-similarity gates or remove the calibration when similarity cannot be established.

### M5 — AWS baseline uses on-demand list rates while SQL warehouses can use cost-optimized Spot

The AWS model intentionally uses the versioned on-demand registry. Databricks' Warehouse API exposes `spot_instance_policy` and defaults it to `COST_OPTIMIZED`.

**Impact:** actual Pro/Classic customer AWS cost and savings may be materially below the modeled on-demand values.

**Fix:** integrate AWS CUR/effective instance rates or a source for actual Spot/on-demand mix. Until then label AWS values clearly as list/on-demand modeled values.

### M6 — DBU dollars are observed usage multiplied by published list price, not customer effective/contract price

`system.billing.list_prices.pricing.effective_list` is still published list pricing. Therefore `actual_365d` is an observed-usage/list-price reconstruction, not necessarily invoice/effective cost.

**Fix:** either retain explicit `LIST_PRICE` semantics everywhere or support an effective-price/contract-rate registry for financial reporting.

### M7 — Billing freshness and trailing-365 boundary can bias current/forward values

The billing collector includes `usage_date >= current_date()-365`, which can span 366 calendar dates including the current partial day. The forward model excludes today but assumes yesterday is complete, while Databricks documents billing-table delays up to 24 hours.

**Fix:** use a billing completeness watermark and exact half-open intervals; exclude incomplete days from the forecast basis.

### M8 — Serverless Photon is asserted `true` instead of honoring contradictory observation

**Code:** optimizer Photon branch ~1820s.

For `SERVERLESS`, the optimizer always emits `NO_CHANGE {enable_photon:true}` even if the billing-derived `PhotonObservation` says `DISABLED` or `MIXED`. Current API docs expose `enable_photon` as a warehouse setting that defaults to true and separately expose serverless enablement; they do not document the hard invariant assumed by the code.

**Fix:** do not override contradictory billing evidence. Use `UNKNOWN/BLOCKED` or compatibility/API corroboration until the platform invariant is proven.

## Additional lower-risk observations

- Failure/cancel rate is collected in query history but is not included in size-down safety constraints. A warehouse with elevated failures can still qualify for a downsize if spill/queue/latency thresholds happen to pass.
- Cost-history thresholds exist, but recommendations/economics can still be generated from short cost histories; confidence degrades, but hard cost sufficiency is not enforced.
- CV is set to `0` when history is too short to compute it, which makes the stability confidence component look perfect rather than unknown.
- 5X-Large is supported in size ordering/AWS topology but the published Serverless DBU/hour map currently stops at 4X-Large; migration safely withholds when target DBU rate is missing, but Serverless size estimation can fall back to a nominal ratio. Prefer explicit preview/price availability treatment.
- Serverless migration economics are necessarily structural pre-canary approximations. Databricks recommends representative-workload benchmarking and billing analysis; the code correctly labels migration economics as pre-canary, so this is a limitation rather than a defect.

## Areas audited with no material defect found

- Current configuration epoch identity and same-input determinism.
- Supported-type fail-closed gate for CLASSIC/PRO/standard SERVERLESS.
- Separation of candidate economics from stateful scenario economics as an architectural pattern.
- DBU/AWS separation and no separate customer EC2 line for Serverless.
- Pro/Classic AWS topology constants match current Databricks sizing documentation.
- Max cluster hard limit 40 / min cluster hard limit 30 match the current API.
- Photon current-epoch billing inference for Pro/Classic has sensible coverage/freshness/purity guards.
- Size-down is appropriately canary-gated (`TEST_REQUIRED`) and uses spill, queue, p95 and p99 latency evidence.
- Warehouse-type migration is canary/compatibility gated and pre-canary economics are explicitly labeled.
- Candidate savings are explicitly non-additive.
- Reviewer gates and deterministic IDs are structurally sound.

## Recommended action before real validation

Fix H1–H4 and add regression tests for them before treating v2.7 economics/recommendations as a trusted real-data baseline. Running v2.7 in read-only mode is operationally safe, but these issues can create misleading recommendations or savings estimates. The medium items can be instrumented/observed during the first real-data run and prioritized using actual evidence.
