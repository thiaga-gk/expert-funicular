# Product Requirements Document — Databricks SQL Warehouse Optimizer

**Document:** `docs/product/prd.md`  
**Product:** Databricks SQL Warehouse Optimizer  
**Reporter JSON Contract:** v1.3.0 (locked)  
**Workbook Contract:** v1.4.0 (locked)

Role: product scope, requirements and acceptance criteria.

---

## 1. Executive summary

The Databricks SQL Warehouse Optimizer is a deterministic, read-only optimization product that analyzes SQL warehouse configuration, workload telemetry, resource-state events, Databricks billing, and account pricing evidence to produce auditable configuration recommendations and scenario-level cost estimates.

The product is designed to answer five questions for every in-scope SQL warehouse:

1. **What is the warehouse and how is it currently configured and used?**
2. **What does it cost historically and what is its forward no-change cost baseline?**
3. **Which infrastructure/configuration changes are supported by current-epoch evidence?**
4. **What are the candidate and scenario-level economic consequences, with confidence and guardrails?**
5. **What should a human reviewer validate, canary, apply, remeasure, or reject?**

The product intentionally separates deterministic decision logic from presentation. The immutable per-warehouse `RecommendationPackage` JSON under Reporter Contract v1.3.0 is the source of truth. `workbook.xlsx` under Workbook Contract v1.4.0 is a presentation-only projection of those locked packages.

The current implementation is **advisory only**. It does not mutate SQL warehouse configuration. Human-in-the-loop approval and canary validation are mandatory for changes that can materially alter workload behavior or runtime.

---

## 2. Problem statement

SQL warehouses can become over-provisioned, under-provisioned, mis-scaled, inefficiently idle, or costly relative to alternative warehouse types as workload shape changes over time. Operators commonly lack a single deterministic and auditable workflow that combines:

- current and historical warehouse configuration,
- query pressure and execution characteristics,
- scale-state evidence,
- DBU consumption and list pricing,
- customer AWS infrastructure cost for Pro/Classic,
- Photon observations,
- ownership and workload-source attribution,
- configuration-change epochs,
- compatibility constraints,
- recommendation sequencing and invalidation,
- candidate and bundled economics,
- and post-change realization.

Manual tuning is difficult to scale across many warehouses and can produce inconsistent decisions. Simple cost reports are insufficient because reducing cost without runtime, queueing, compatibility, or operational guardrails can degrade business outcomes.

The product therefore needs to turn system evidence into **repeatable, explainable, sequenced, confidence-scored optimization decisions** while preserving safety and financial traceability.

---

## 3. Product vision

Create a trustworthy SQL warehouse optimization engine that can continuously identify and explain infrastructure/configuration opportunities across Databricks SQL warehouses, quantify their expected economic impact, provide an ordered HITL execution plan, and later measure realized outcomes without allowing probabilistic reasoning to override deterministic safety policy.

The product should be usable by platform engineering, FinOps, SRE, product leadership, and workload owners from the same canonical package while exposing different human views through the workbook.

---

## 4. Product principles

1. **Deterministic decisions** — identical normalized evidence plus identical policy/model versions must produce identical decisions, IDs, confidence values, scenarios, and economics.
2. **Current-epoch authority** — only evidence belonging to the current warehouse configuration epoch may satisfy decision thresholds. Historical epochs may calibrate models but must not justify current-state decisions.
3. **System-table first** — source configuration and optimization telemetry come from Databricks system tables. Read-only APIs are limited to execution-target resolution/validation and compatibility evidence.
4. **Read-only by default** — the product generates recommendations; it does not apply warehouse configuration changes.
5. **HITL mandatory** — configuration changes require human review; materially behavior-changing recommendations require canary validation.
6. **No fake certainty** — unavailable evidence is represented as unavailable/null, not zero or guessed values.
7. **No additive candidate savings** — candidate economics are independent counterfactuals. Only the stateful scenario estimator may produce bundled economics.
8. **Cost and performance together** — savings must be assessed with queueing, latency, spill, startup behavior, compatibility, and failure guardrails.
9. **Explicit provenance** — every recommendation must be traceable to its evidence, technique, versions, thresholds, and deterministic identifiers.
10. **Presentation does not decide** — JSON is authoritative; workbook rendering cannot recompute recommendation or savings logic.

---

## 5. Target personas and jobs to be done

### 5.1 Product / engineering executive

**Needs to know:**
- total assessed annual cost,
- low/base/high opportunity envelope,
- concentration of opportunities,
- confidence posture,
- blocked/unassessed coverage,
- recommended next actions.

**Primary surface:** Executive Summary workbook sheet.

### 5.2 FinOps / platform leader

**Needs to know:**
- which warehouses drive spend,
- package-level economics,
- owner and workload source,
- current configuration,
- recommended scenario,
- evidence quality,
- portfolio prioritization.

**Primary surface:** Warehouse Portfolio workbook sheet and package JSON.

### 5.3 Platform engineer / SRE / HITL approver

**Needs to know:**
- exact current and recommended values,
- dependency order,
- blockers,
- canary requirements,
- invalidated downstream recommendations,
- remeasurement requirements,
- compatibility checks.

**Primary surface:** Scenario Plans and Recommendation Registry workbook sheets.

### 5.4 Optimization engineer / auditor

**Needs to know:**
- technique identity,
- thresholds,
- evidence references,
- confidence calculation,
- cost-model inputs,
- deterministic hashes,
- source/provenance,
- realization outcome.

**Primary surface:** package JSON, evidence registry, Recommendation Registry, logs, source code.

### 5.5 Warehouse owner

**Needs to know:**
- why a change is proposed,
- expected impact,
- what must be validated,
- whether it affects performance or compatibility,
- when the recommendation should be re-evaluated.

**Primary surface:** human-readable package/workbook recommendation and execution plan.

---

## 6. Scope

### 6.1 In-scope warehouse types for Phase 1

The implemented optimization domain supports:

- **Classic SQL warehouses**
- **Pro SQL warehouses**
- **standard Serverless SQL warehouses**

The product must not claim full SQL-warehouse-type coverage beyond these types.

### 6.2 Explicit type exclusion

**Lakehouse Real-Time (Beta) is out of scope for Phase 1.** It has distinct query-size and DBU-based autoscaling semantics and currently cannot be directly upgraded/downgraded to other warehouse types. It requires a dedicated source contract, analyzer, optimizer, estimator, compatibility model, and golden tests before it can enter active optimization scope.

### 6.3 In-scope optimization domain

Phase 1 covers **SQL warehouse infrastructure/configuration optimization** only.

It uses query-level telemetry as evidence but does not rewrite, refactor, or automatically optimize SQL statements.

### 6.4 Active recommendation candidates

Each supported warehouse must produce exactly one candidate decision for each active candidate domain:

| Candidate domain | Purpose | Product classification |
|---|---|---|
| `warehouse_channel` | identify Preview→Current stability review | governance/safety guardrail |
| `warehouse_size` | downsize/upsize/right-size | cost/performance |
| `max_clusters` | reduce unused ceiling or increase constrained concurrency | performance/guardrail |
| `min_clusters` | reduce unnecessary baseline capacity | cost |
| `auto_stop_minutes` | reduce avoidable idle runtime | cost |
| `warehouse_type` | evaluate Pro/Classic↔Serverless migration experiment | cost/performance/compatibility |

`warehouse_channel` is intentionally retained as a candidate because a Preview dependency can invalidate other tuning assumptions; it is **not** counted as a direct savings technique unless economics are explicitly available.

Photon is observed and never recommended; it is not a candidate domain (rationale: `docs/reference/known-limitations.md`).

### 6.5 Configuration items currently excluded from active optimization

- spot instance policy,
- effective warehouse-level statement timeout,
- unsupported or undocumented configuration fields not present in the approved authoritative source contract,
- Lakehouse Real-Time query size/autoscaling,
- SQL text/query rewrite optimization,
- storage optimization,
- table layout optimization,
- query scheduling optimization,
- network architecture optimization.

---

## 7. Product policy decisions

### 7.1 Conservative scenario recommendation policy

For Phase 1, **Plan A — optimize the current warehouse type** is the deterministic recommended execution path.

A warehouse-type migration is emitted as an **alternative experimental scenario** requiring compatibility validation and a canary even if its modeled range appears economically attractive.

This is a deliberate Phase-1 safety policy, not an assertion that Plan A is economically superior.

A future product increment may add deterministic migration-selection policy that can promote a migration scenario to `RECOMMENDED` when compatibility, confidence, savings, runtime guardrails, and canary evidence meet explicit thresholds.

### 7.2 One authoritative recommendation package per warehouse/current epoch

One immutable package represents exactly:

`workspace + warehouse + current configuration epoch + normalized evidence + policy/model versions`.

A multi-warehouse run emits multiple independent package JSON files rather than one custom aggregate recommendation document.

### 7.3 Candidate and scenario separation

- Candidate = one technique applied independently to one warehouse/current epoch.
- Scenario = ordered bundle of candidates applied to evolving state.
- Package = complete warehouse assessment across all assessable scenarios.
- Realization = measured post-change outcome referencing the original immutable package.

---

## 8. Source and evidence requirements

### FR-SRC-001 — Configuration epochs

The product shall reconstruct current and historical warehouse configuration epochs using `system.compute.warehouses`.

A configuration epoch is a period during which warehouse type, channel, size, min/max clusters and auto-stop are unchanged. Snapshots that change only cosmetic properties (name, tags) do not start a new epoch.

The current epoch shall be the only source of configuration used for active decisions.

### FR-SRC-002 — Query evidence

The product shall derive workload and pressure evidence from `system.query.history`, including where available:

- query counts,
- execution latency,
- capacity queueing/wait,
- spill,
- cache evidence,
- query source,
- client application,
- statement visibility/compatibility indicators.

Raw SQL text shall not be required for the core optimizer and shall not be exported as recommendation evidence.

### FR-SRC-003 — Warehouse event evidence

The product shall use `system.compute.warehouse_events` to derive scale/resource-state evidence such as starts, stops, cluster minutes, observed maximum scale, and time at configured minimum/maximum.

### FR-SRC-004 — Billing evidence

The product shall use `system.billing.usage` for DBU consumption/cost attribution, billing corrections, Photon observation, owner attribution, and warehouse usage evidence.

Signed billing quantities shall be preserved when calculating corrected usage.

### FR-SRC-005 — DBU price evidence

The product shall use `system.billing.list_prices` for temporal Databricks SKU list pricing and target-SKU price evidence.

Usage with no matching price row shall be retained and reported as unpriced DBU, and shall make the affected DBU cost unavailable rather than understated or zero.

### FR-SRC-006 — AWS price model

For Pro/Classic, forward AWS compute cost shall be modeled separately from DBU cost using current-epoch resource time on the same recent window and blend as the DBU forecast, explicit SQL warehouse topology, and a versioned region-specific AWS on-demand price registry.

There shall be no silent default-region price fallback.

### FR-SRC-007 — Historical AWS actuals

If authoritative historical customer AWS billing/effective-rate data is unavailable, historical AWS actual cost shall remain unavailable/null rather than being labeled as actual from a model.

### FR-SRC-008 — Read-only API boundary

Read-only Databricks REST calls may be used only for:

- execution-target validation/resolution,
- directly authenticated workspace compatibility evidence,
- required workspace identity resolution.

REST values shall not override authoritative optimizer configuration from system tables.

### FR-SRC-009 — Multi-region operation

Regional system-table evidence shall be collected per region and merged by `(workspace_id, warehouse_id)` for account-wide analysis.

---

## 9. Configuration epoch requirements

### FR-EPOCH-001

Every recommendation must reference a deterministic `epoch_id` for the current configuration regime.

### FR-EPOCH-002

A configuration change shall invalidate prior current-epoch decision evidence where appropriate.

### FR-EPOCH-003

Historical epochs may be used for model calibration/backtesting only when configuration compatibility is explicitly satisfied.

### FR-EPOCH-004

Recommendations requiring a minimum stable configuration age shall be blocked/no-change until the configured policy age (`current_config_stability_days`) is satisfied. Default: `docs/reference/thresholds.md`.

---

## 10. Analyzer requirements

### FR-AN-001

The analyzer shall convert normalized source evidence into deterministic findings for every active candidate domain.

### FR-AN-002

Each finding shall include sufficient traceability to identify:

- metric,
- value,
- unit,
- source table/field,
- observation window,
- threshold/comparator where applicable,
- interpretation,
- current epoch.

### FR-AN-003

Analyzer outputs shall not directly mutate or apply recommendations.

### FR-AN-004

Analyzer evidence shall support explicit sufficiency and blocker evaluation rather than treating missing evidence as zero.

---

## 11. Optimization policy requirements

Every parameter named below is a `Thresholds` field. Defaults: `docs/reference/thresholds.md`. Meaning, firing condition and resulting status of each blocker code: `docs/reference/blocker-codes.md`. Missing evidence is never read as zero: an unreported value holds the change.

### FR-OPT-001 — Channel

If a warehouse uses `PREVIEW`, the optimizer shall emit a `REVIEW` candidate for `CURRENT` and require validation of Preview-only dependencies before change.

A channel change shall invalidate downstream performance/cost tuning assumptions.

### FR-OPT-002 — Photon observation

Photon state shall be treated as an observed current-epoch state derived from billing feature evidence, not as a direct warehouse-config read.

The observed state is `ENABLED`, `DISABLED`, `MIXED` or `UNKNOWN`:

- the state is inferred only when enough current-epoch DBU carries a known Photon feature flag (`photon_min_feature_coverage_pct`), the current epoch has enough total DBU (`photon_min_epoch_dbu`) and the latest supporting billing usage is recent enough (`photon_freshness_days`); otherwise it is `UNKNOWN`;
- a high enough share of Photon-enabled DBU (`photon_enabled_usage_pct`) gives `ENABLED`, a low enough share (`photon_disabled_usage_pct`) gives `DISABLED`, and anything in between is `MIXED`.

Photon is observed and never recommended; no Photon recommendation is produced for any observed state (`docs/reference/known-limitations.md`).

### FR-OPT-003 — Size down

A one-size down canary may be proposed only when all of the following hold:

- the current configuration epoch is old enough (`current_config_stability_days`) and the 30-day query sample is large enough (`strong_query_sample_size`);
- spill frequency is known and not above the downsize-safe level (`spill_query_pct_downsize_safe`);
- queue frequency is known and not above the safe level (`queue_query_pct_safe`);
- the failed/cancelled query share is known and not above the capacity-cut-safe level (`failure_cancel_pct_downsize_safe`);
- p95 and p99 execution latency are known and not above the downsize-safe levels (`p95_execution_ms_downsize_safe`, `p99_execution_ms_downsize_safe`);
- the warehouse size is known and a smaller size exists.

Evidence the collector did not report is null (never treated as zero pressure) and holds the downsize.

Downsize status shall be `TEST_REQUIRED`, not auto-actionable. When any condition fails the candidate is `NO_CHANGE` and carries the blockers that explain why (`docs/reference/blocker-codes.md`).

### FR-OPT-004 — Size up

An upsize canary may be proposed only when all of the following hold:

- spill frequency is at or above the upsize level (`spill_query_pct_upsize`) and p95 spill among spilled queries is at or above the upsize severity level (`spill_p95_bytes_upsize`);
- enough queries spilled for the p95 to be meaningful (`severity_min_affected_queries`);
- the 30-day query sample is large enough (`min_query_sample_size`) and the current configuration epoch is old enough (`current_config_stability_days`);
- a larger known size exists.

When spill frequency crosses the upsize level but spill severity is unreported, or the sample, affected-query count or epoch age is insufficient, the candidate is `INSUFFICIENT_EVIDENCE` and no resize is recommended. At the largest known size the candidate is `BLOCKED`. Unknown spill evidence is never treated as zero.

Upsize status shall be `TEST_REQUIRED`, not auto-actionable: the speed-up and its cost are modeled, not observed, so an A/B canary validates them.

### FR-OPT-005 — Max clusters increase

An increase of one cluster may be proposed only when all of the following hold:

- queue frequency is at or above the increase level (`queue_query_pct_increase_max`);
- p95 capacity wait among queued queries is at or above the increase level (`queue_wait_p95_ms_increase_max`);
- time at the configured maximum is saturated over the recent resource window (`time_at_max_pct_saturated`, `pressure_window_days`); saturation and queueing are required together but are not proven to coincide (`docs/reference/known-limitations.md`);
- enough queries queued for the p95 to be meaningful (`severity_min_affected_queries`), the 30-day query sample is large enough (`min_query_sample_size`) and the current configuration epoch is old enough (`current_config_stability_days`);
- the configured maximum is below the supported limit (`max_clusters_supported_limit`).

When the frequency and saturation signals are present but capacity wait is unreported, or the sample, queued-query count or epoch age is insufficient, the candidate is `INSUFFICIENT_EVIDENCE`. At the supported limit it is `BLOCKED`. Unknown queue evidence is never treated as zero.

### FR-OPT-006 — Max clusters reduction

A reduction may be proposed only when the configured maximum is demonstrably unused and queue pressure is known to be low:

- the recent resource window is available and the warehouse ran in it;
- the configured maximum is above the minimum, the current configuration epoch is old enough (`current_config_stability_days`) and the 30-day query sample is large enough (`min_query_sample_size`);
- time at the configured maximum is low (`time_at_max_pct_safe_reduce`);
- queue frequency is known and not above the safe level (`queue_query_pct_safe`);
- the failed/cancelled query share is known and not above the capacity-cut-safe level (`failure_cancel_pct_downsize_safe`);
- the new maximum is the peak plus headroom (the larger of `max_cluster_headroom_clusters` and `max_cluster_headroom_pct` of the peak, rounded up, never below the minimum), and it is below the current maximum; the peak is the larger of the recent-window peak and the whole current epoch's peak, so a seasonal peak outside the recent window is never cut away.

Unreported queue frequency holds the reduction; queries that queued with an unreported capacity wait hold it too. A reduction the recent window allows but the epoch peak does not is held as `NO_CHANGE` with `PEAK_OUTSIDE_RECENT_WINDOW` (`docs/reference/blocker-codes.md`).

This candidate is primarily a cost/operational guardrail; the system shall not fabricate direct savings if observed resource consumption would not change.

### FR-OPT-007 — Min clusters reduction

A reduction of one cluster may be proposed only when current-epoch evidence demonstrates substantial time at minimum capacity with low queue/scale pressure:

- the configured minimum is above 1, the current configuration epoch is old enough (`current_config_stability_days`) and the 30-day query sample is large enough (`min_query_sample_size`);
- time at the configured minimum is high enough (`time_at_min_pct_reduce`) and time at the configured maximum is below the blocking level (`time_at_max_pct_block_min_reduce`), both over the recent resource window;
- queue frequency is known and not above the safe level (`queue_query_pct_safe`), and p95 capacity wait among queued queries is known and not above the safe level (`queue_wait_p95_ms_safe`);
- the failed/cancelled query share is known and not above the capacity-cut-safe level (`failure_cancel_pct_downsize_safe`).

Unreported queue frequency or capacity wait holds the reduction (`docs/reference/blocker-codes.md`).

Time at the minimum does not prove a floor cluster was spare, so the status shall be `TEST_REQUIRED` and the savings an upper-bound range, not an actionable change.

### FR-OPT-008 — Auto-stop

The optimizer shall derive a deterministic target based on activity class and warehouse type: standard Serverless has its own target (`serverless_auto_stop_target_minutes`); Pro/Classic targets rise with queries per active day across the low, medium and high activity classes (`low_activity_queries_per_day`, `medium_activity_queries_per_day`, `low_activity_auto_stop_target_minutes`, `medium_activity_auto_stop_target_minutes`, `high_activity_auto_stop_target_minutes`).

No change is proposed when a non-zero current auto-stop is already at or below the target.

The optimizer shall block an auto-stop reduction (status `BLOCKED`) when any of the following holds:

- the current configuration epoch is too young (`current_config_stability_days`) or the 30-day query sample is too small (`min_query_sample_size`);
- too few idle gaps (`min_idle_gap_sample_size`) or too few observed cluster-minutes (`min_cluster_minutes_for_cost_model`) support the counterfactual;
- the workload is cold-start sensitive: p95 compute wait among queries that waited and warehouse starts per active day are both at or above their blocking levels (`cold_start_p95_ms_block`, `cold_starts_per_active_day_block`), or the compute-wait severity is unreported;
- the failed/cancelled query share is unknown or above the capacity-cut-safe level (`failure_cancel_pct_downsize_safe`).

### FR-OPT-009 — Warehouse type migration

The optimizer shall create a separate migration candidate/scenario:

- Pro/Classic → Serverless experiment,
- Serverless → Pro experiment.

Migration shall be `TEST_REQUIRED` and compatibility-gated when the forward DBU baseline is known; when it is unknown or too young the candidate is `INSUFFICIENT_EVIDENCE` with a DBU-baseline blocker (`docs/reference/blocker-codes.md`). Current-type tuning and type migration shall not be combined into the same execution scenario.

### FR-OPT-010 — Candidate completeness

Every supported warehouse shall emit exactly one candidate state for every active candidate domain, including `NO_CHANGE`, `BLOCKED`, `REVIEW`, or `TEST_REQUIRED` outcomes.

---

## 12. Candidate status requirements

The public contract shall support the recommendation states enumerated in `docs/contracts/reporter-json-contract.md` (Recommendation status).

Recommendation status and execution state shall remain separate concepts.

For example, an `ACTIONABLE` candidate may be `PENDING_REEVALUATION` in a scenario after an upstream invalidating change.

---

## 13. Dependency, invalidation, and execution requirements

### FR-PLAN-001

Plan A ordering shall follow:

`warehouse_channel → warehouse_size → max_clusters → min_clusters → auto_stop_minutes`.

### FR-PLAN-002

A change that invalidates downstream evidence shall mark affected steps `PENDING_REEVALUATION` rather than silently carrying forward stale recommendations.

### FR-PLAN-003

The standard post-invalidating-change remeasurement period (`remeasure_days_after_invalidating_change`) shall apply unless policy overrides it.

### FR-PLAN-004

A scenario execution plan shall contain explicit validation gates and required next action.

### FR-PLAN-005

The public plan shall distinguish the execution states enumerated in `docs/contracts/reporter-json-contract.md` (Execution state).

---

## 14. Estimation and economics requirements

### 14.1 Economic concepts

The product shall distinguish:

1. **Actual trailing 365-day cost** — observed historical cost.
2. **Forward 365-day baseline** — expected next-365-day cost if nothing changes.
3. **Candidate counterfactual** — isolated next-365-day economics if only one candidate is applied.
4. **Scenario counterfactual** — stateful ordered next-365-day economics for a bundled plan.
5. **Package opportunity envelope** — cross-scenario low/base/high headline economics.
6. **Realized savings** — measured outcome after approved implementation.

The forward DBU baseline shall be published only when it is known: every day in the current-epoch basis priced, enough billed days in the current epoch (`min_economics_cost_history_days`), and a positive run rate. Otherwise it shall be unavailable (null), never zero or a run rate guessed from a few days, and savings estimates and warehouse-type migration shall be withheld.

### FR-EST-001

DBU and customer AWS costs shall be represented separately where both are applicable.

### FR-EST-002

Candidate economics shall be marked non-additive.

### FR-EST-003

Scenario economics shall be produced by a stateful bundle estimator that updates the evolving resource/cost state as steps are applied.

### FR-EST-004

Package economics shall not sum independent candidate estimates.

### FR-EST-005

Package opportunity envelope semantics shall be:

- low = minimum LOW total-savings bound among economically assessable scenarios,
- base = BASE savings of the deterministic recommended scenario,
- high = maximum HIGH total-savings bound among economically assessable scenarios.

The package envelope is an opportunity range, not a statistical confidence interval.

### FR-EST-006

DBU/AWS component values for package envelope points shall remain attached to the scenario that generated the point; cross-scenario components shall not be combined into synthetic economics.

### FR-EST-007

Negative signed savings in the canonical JSON shall remain valid and mean **cost increase**, not malformed savings.

Human presentation shall label these as cost increases rather than displaying a negative number under a misleading savings heading.

### FR-EST-008

Pro/Classic size economics shall not reuse the Serverless DBU/hour size schedule as if it were a Pro/Classic capacity-cost curve.

Pro/Classic sizing shall use same-workspace billed DBU-per-cluster-hour only when the rates rise with size and the size-pair ratio stays within 25% of the published size ratio; otherwise the estimate is withheld and the change needs a canary.

### FR-EST-009

Migration economics shall be treated as pre-canary modeled ranges and must not be represented as realized outcomes.

---

## 15. Confidence requirements

### FR-CONF-001

Every candidate shall carry deterministic confidence:

- numeric score,
- level,
- components,
- calculation method.

### FR-CONF-002

Confidence levels (`HIGH`, `MEDIUM`, `LOW`) shall be derived from the score by the cut-offs `confidence_high_threshold` and `confidence_medium_threshold`, and the level shall be `UNKNOWN` only when a score is unavailable under the public contract (`docs/contracts/reporter-json-contract.md`, Confidence).

### FR-CONF-003

Scenario steps shall copy candidate confidence exactly.

### FR-CONF-004

Scenario confidence shall use the bottleneck/minimum step confidence unless an explicit future policy version replaces the method.

### FR-CONF-005

Confidence shall mean **quality/sufficiency of evidence for the decision**, not probability that the estimated savings will be realized.

---

## 16. Reviewer requirements

### FR-REV-001 — Candidate reviewer

Before scenario planning, the reviewer shall validate candidate invariants including:

- exactly one candidate per active domain,
- deterministic identity,
- current-epoch reference integrity,
- confidence range,
- blocker/status semantics,
- valid target configuration ranges,
- nonnegative component cost values where applicable,
- low ≤ base ≤ high ranges,
- evidence reference integrity,
- currency consistency.

### FR-REV-002 — Scenario reviewer

After scenario construction, the reviewer shall validate:

- step-to-candidate identity consistency,
- confidence propagation,
- dependency/invalidation behavior,
- contiguous step ordering,
- scenario economic range integrity,
- current-type vs migration separation,
- recommended-scenario uniqueness,
- package-envelope integrity.

### FR-REV-003

A validation failure shall stop publication of the affected package rather than downgrade silently to a successful report.

---

## 17. Public Reporter JSON Contract requirements

The only authoritative public recommendation serialization is **Reporter JSON Contract v1.3.0**.

### FR-JSON-001

Every warehouse/current epoch shall emit one immutable package with the top-level hierarchy locked in `docs/contracts/reporter-json-contract.md` (Locked hierarchy).

### FR-JSON-002

Stable unavailable fields shall use `null`; known zero shall use `0`; known empty collection shall use `[]`.

### FR-JSON-003

Every package shall validate against:

- Python dataclass validation,
- deterministic hash/ID recomputation,
- cross-reference and semantic invariants,
- JSON Schema Draft 2020-12.

### FR-JSON-004 — Identity hierarchy

The product shall use distinct identities:

- `recommendation_package_id` — immutable warehouse assessment,
- `epoch_id` — configuration regime,
- `technique_id` — stable catalog technique,
- `candidate_id` — technique decision instance,
- `scenario_id` — bundled execution path,
- `realization_id` — measurement episode,
- evidence IDs — content-addressed evidence records.

### FR-JSON-005

Volatile presentation metadata such as generation timestamp, workbook layout, filenames, logs, or prose shall not alter deterministic recommendation identity.

---

## 18. Workbook Contract requirements

The workbook shall be a pure projection of locked package JSONs and shall contain exactly four sheets under Workbook Contract v1.4.0.

### 18.1 Executive Summary

Portfolio/leadership view over the supplied packages. Contents: `docs/contracts/workbook-contract.md`.

### 18.2 Warehouse Portfolio

Grain: **one row per `recommendation_package_id`**. Columns and groups: `docs/contracts/workbook-contract.md`.

### 18.3 Scenario Plans

Grain: **one row per `scenario_id`**. Columns and groups: `docs/contracts/workbook-contract.md`.

### 18.4 Recommendation Registry

Grain: **one row per `candidate_id`**. Columns and groups: `docs/contracts/workbook-contract.md`.

### FR-XLSX-001

Workbook logic shall not recompute optimizer decisions, IDs, confidence, or scenario economics.

### FR-XLSX-002

No hidden business formulas or hidden worksheets shall be used.

### FR-XLSX-003

Unknown/unavailable semantic cells shall be explicit (`—`, `UNKNOWN`, `UNAVAILABLE`) and never silently rendered as zero.

### FR-XLSX-004

A cost increase is a negative annual-impact number (shown in red parentheses) with Impact Type `COST_INCREASE`; the sign is never flipped.

### FR-XLSX-005

Workbook sheets shall use frozen identity columns, filterable headers, section bands, readable number formats, and long-form audit JSON positioned after primary operational fields.

---

## 19. Realization requirements

### FR-REAL-001

The Reporter Contract shall include realization from initial publication, initialized as `NOT_APPLIED` with zero records.

### FR-REAL-002

A realization record shall reference the immutable original package, scenario, candidate and technique.

### FR-REAL-003

Realization shall capture, where available, the applied change, the validation window and guardrail result, observed DBU/AWS/total cost, realized and annualized realized savings, rollback outcome and learning codes. Record fields: `docs/contracts/reporter-json-contract.md` (`realization`).

### FR-REAL-004

Adding realization evidence shall not mutate the identity of the original recommendation package.

### FR-REAL-005

Realization lifecycle shall support the states enumerated in `docs/contracts/reporter-json-contract.md` (Realization lifecycle).

---

## 20. Functional execution requirements

### FR-RUN-001 — Live collection

The CLI shall support collection SQL executed using:

- SQL warehouse,
- Serverless SQL warehouse,
- all-purpose cluster when valid for SQL execution.

Jobs compute shall not be accepted as the execution target.

### FR-RUN-002 — Authentication

Live execution shall use Databricks CLI OAuth profile-based authentication. Static long-lived tokens shall not be logged or emitted.

### FR-RUN-003 — Evidence replay

The product shall support running from normalized evidence files without re-querying live source systems.

### FR-RUN-004 — Synthetic execution

The product shall include deterministic synthetic evidence sufficient to exercise one simulated warehouse per golden scenario through the same publisher path used by live/replayed evidence.

### FR-RUN-005 — Output directory

A normal run shall write a `packages/` directory with one package JSON per warehouse, `workbook.xlsx`, `evidence.json`, `manifest.json` and `publisher.log`; the tree is in `README.md` (Output directory). `evidence.json` is a versioned, schema-validated replay file that holds owner emails and tags (`docs/contracts/evidence-format.md`).

The workbook may be disabled explicitly without disabling authoritative JSON package generation.

### FR-RUN-006 — Logging

Logs shall provide meaningful lifecycle/provenance events while avoiding credentials, auth tokens, raw SQL statement text, and sensitive configuration values that are not required for diagnosis.

---

## 21. Non-functional requirements

### NFR-001 — Determinism

Same normalized evidence + same thresholds + same policy/model/catalog/contract versions must produce identical:

- recommendation decisions,
- IDs/hashes,
- scenario ordering,
- economic ranges,
- confidence values,
- package semantics.

Presentation timestamps or prose shall not alter decision identities.

### NFR-002 — Reproducibility

Every package shall retain sufficient version/provenance information to identify the decision implementation and source evidence used.

### NFR-003 — Read-only safety

The product shall contain no production configuration mutation path in Phase 1.

### NFR-004 — Financial integrity

Unavailable costs shall never be coerced to zero. Currency shall be explicit. Cross-currency package aggregation shall not occur without an explicit FX policy.

### NFR-005 — Scale

Source SQL shall aggregate telemetry in Databricks before Python processing. Python processing shall operate primarily on per-warehouse/per-epoch normalized evidence rather than raw query histories.

Downstream warehouse processing shall support deterministic batching; current default batch size is 100.

### NFR-006 — Performance

The product shall avoid requiring pandas for the production decision path. Set-based source aggregation and bounded Python objects/dataclasses are preferred for predictable memory and serialization behavior.

### NFR-007 — Maintainability

Stable public boundaries shall use typed contracts/dataclasses. Source-shaped telemetry may remain dictionary-based inside normalization/analyzer boundaries to avoid tight coupling to evolving system-table columns.

### NFR-008 — Observability

The run shall emit logs and a manifest sufficient to determine:

- publisher version,
- policy/model versions,
- packages emitted,
- output locations,
- source regions/evidence mode,
- validation success/failure.

### NFR-009 — Security and privacy

The product shall minimize copied source data, not export raw SQL text, not log credentials, and preserve only evidence required for recommendation explanation/replay.

### NFR-010 — Contract stability

Reporter JSON and Workbook contract versions shall be independently versioned from the publisher implementation.

Breaking public contract changes require explicit major-version review.

### NFR-011 — Testability

Every optimizer technique and critical invariant shall have deterministic unit/golden/integration coverage.

### NFR-012 — Failure behavior

Contract, reviewer, or invariant failures shall fail closed for package publication rather than silently producing a partially valid recommendation artifact.

---

## 22. Product success metrics

Phase-1 success is measured by correctness, safety, explainability, and economic usefulness before automation rate.

### 22.1 Coverage

- ≥95% of supported Classic/Pro/standard Serverless warehouses with required system-table access produce a valid package.
- 100% of packages identify owner/source as known or explicitly unavailable.
- 100% of supported warehouses produce one candidate state per active candidate domain.

### 22.2 Decision quality

For the first real validation cohort of 10–20 representative warehouses:

- zero known unsafe auto-application paths,
- no decision based on stale non-current-epoch evidence,
- no false conversion of unavailable evidence into zero,
- false-positive/false-negative threshold observations documented per technique,
- all canary-required techniques remain canary-gated.

### 22.3 Financial quality

- 100% of portfolio economics trace to scenario economics rather than summed candidates.
- 100% of package envelope points trace to a source scenario.
- DBU/AWS separation preserved wherever applicable.
- forecast and counterfactual basis explicit.

### 22.4 Auditability

- 100% of candidate/scenario/package IDs recomputable from declared hash inputs.
- 100% of evidence references resolve.
- 100% of emitted packages pass schema + semantic validation.

### 22.5 Realization

Once apply/measure workflow begins:

- realized savings captured for every applied candidate/scenario,
- rollback/failure outcomes retained,
- predicted vs realized variance available for estimator calibration.

---

## 23. Acceptance criteria for the current sealed implementation

The current product increment is accepted when all of the following remain true:

1. Classic, Pro, and standard Serverless evidence can traverse the canonical pipeline.
2. Current config epochs are reconstructed from authoritative source evidence.
3. Six active candidate domains (channel, size, max clusters, min clusters, auto-stop, warehouse-type migration) are emitted per supported warehouse.
4. Candidate and scenario reviewer gates pass before publication.
5. Candidate economics remain non-additive.
6. Scenario economics are stateful.
7. Package economics are a scenario envelope.
8. Packages validate semantically and against the committed JSON Schema of the locked Reporter JSON Contract.
9. The workbook is a pure four-sheet projection of the packages under the locked Workbook Contract.
10. The demo fleet run (one simulated warehouse per golden scenario) produces one package per simulated warehouse and one readable workbook.
11. Workbook financial-impact fields are numeric, explicitly currency-scoped, and use signed values: positive = savings, negative = cost increase; unavailable remains unavailable rather than zero.
12. The test suite passes in a clean checkout/extracted artifact.

**Current implementation evidence:** the repository regression suite and the clean synthetic end-to-end validation pass; reproduce the results with the commands in `CLAUDE.md`.

---

## 24. Known limitations and assumptions

Known limitations and assumptions are recorded in `docs/reference/known-limitations.md`.

---

## 25. Out of scope / future product increments

- automatic mutation/apply workflow,
- policy-based migration scenario promotion to recommended,
- Lakehouse Real-Time optimization,
- SQL/query rewrite recommendations,
- workload-group optimization,
- cross-warehouse topology consolidation,
- storage/table optimization,
- scheduling optimization,
- predictive ML thresholding,
- LLM decision authority,
- automatic FX conversion,
- CUR/effective AWS billing integration,
- negotiated Databricks pricing beyond available effective source data,
- enterprise workflow/approval UI,
- automated rollback execution.

Future ML or LLM components may assist explanation, anomaly diagnosis, or threshold research, but deterministic analyzers/optimizers/reviewers remain authoritative unless the product requirements are explicitly revised.

---

## 26. Dependencies

- access to required Databricks system tables,
- permissions across intended workspaces/regions,
- sufficient system-table retention for evidence windows,
- valid Databricks CLI OAuth profile for live collection,
- approved execution target,
- regional AWS price registry for modeled Pro/Classic infrastructure economics,
- JSON Schema/runtime validation dependencies,
- workbook backend for `workbook.xlsx` generation.

---

## 27. Locked contracts referenced by this PRD

- `docs/contracts/reporter-json-contract.md`
- `schema/recommendation-package.schema.json`
- `contracts/reporter_contract.py`
- `docs/contracts/workbook-contract.md`
- `contracts/workbook_contract.py`
- `docs/reference/technique-catalog.md`
- `docs/reference/thresholds.md`
- `docs/reference/source-mapping.md`
- `docs/reference/aws-cost-model.md`

Document ownership and precedence are defined once, in `docs/README.md`.

---

## 28. External platform references

The Databricks documentation pages the product's platform assumptions rest on. This is the only list of them; other documents point here.

| Topic | Page |
|---|---|
| System tables overview: regional/global scope and retention | `https://docs.databricks.com/aws/en/admin/system-tables/` |
| Warehouses system table: configuration snapshots over time | `https://docs.databricks.com/aws/en/admin/system-tables/warehouses` |
| Warehouse events system table: start/stop/scale events and cluster counts | `https://docs.databricks.com/aws/en/admin/system-tables/warehouse-events` |
| Query history system table: query execution evidence, query source and client application | `https://docs.databricks.com/aws/en/admin/system-tables/query-history` |
| Billable usage system table: usage corrections, ownership, product features and Photon attribution (list prices are read from the pricing system table) | `https://docs.databricks.com/aws/en/admin/system-tables/billing` |
| SQL warehouse types and capabilities | `https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-types` |
| Lakehouse Real-Time (Beta specialized serverless warehouse) | `https://docs.databricks.com/aws/en/compute/sql-warehouse/real-time` |
| SQL warehouse sizing and scaling behavior | `https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior` |
| SQL warehouse monitoring sources | `https://docs.databricks.com/aws/en/compute/sql-warehouse/monitor/queries` |

Current product support remains intentionally narrower than every newly available Databricks warehouse variant.

---

## 29. Product-owner decisions recorded / pending

### Recorded in this PRD

1. Reporter JSON Contract v1.3.0 is authoritative.
2. Workbook Contract v1.4.0 is presentation-only.
3. Phase-1 optimization is infrastructure/configuration only.
4. Candidate economics are never additive.
5. Scenario economics are stateful and package economics are scenario-envelope based.
6. HITL is mandatory and canaries are mandatory where explicitly required.
7. Classic, Pro, and standard Serverless are the Phase-1 supported warehouse types.
8. Lakehouse Real-Time Beta is excluded until a dedicated design is built.
9. `warehouse_channel` is retained as a governance/safety guardrail candidate.
10. Plan A current-type optimization is the Phase-1 recommended path; migration remains alternative/experimental.

### Future decision requiring explicit product approval

Whether a later policy may automatically select a migration scenario as `RECOMMENDED` when its compatibility, confidence, performance guardrails, and economics exceed explicit deterministic selection thresholds.

---

## 30. Approval gate

Change control. A change to product scope or policy in this document, or to a requirement in it, is shown as exact text and approved by the product owner before it merges (`docs/proposals/remediation-runbook.md`).
