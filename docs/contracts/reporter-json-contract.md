# Reporter JSON Contract v1.2.0 — Locked

**Version history:** 1.2.0 (M9 Stage 1) adds the nullable `recommended_plan.reevaluation` window, the top-level nullable `observed_change` (observed config diff, no savings) and the reserved nullable `carried_forward_plan` (always null until Stage 2 ships), plus the `WAIT_FOR_REEVALUATION` next action. The package hash payload gains `reevaluation`, `observed_change_fingerprint` and `carried_forward_fingerprint`, and `contract_version` is hashed into every identity, so all `EPOCH-/EVID-/CAND-/SCN-/REAL-/PKG-` ids differ from 1.1.0 packages. See section 19 for why this is a MINOR version.

**Earlier history:** 1.1.0 is an additive evolution of 1.0.0 (no field renamed or removed): plan steps may carry `projected_economics` and scenario economics may carry a separate `contingent_*` upside, so steps awaiting re-evaluation never feed authoritative scenario or package economics. `contract_version` is hashed into every identity, so all `EPOCH-/EVID-/CAND-/SCN-/PKG-` ids differ from 1.0.0 packages.

**Status:** LOCKED for implementation  
**Scope:** one immutable recommendation package per Databricks SQL warehouse / current configuration epoch  
**Schema:** `schema/recommendation-package.schema.json`  
**Python:** `contracts/reporter_contract.py`

The contract is intentionally independent of the workbook. The JSON contract is the canonical product/API contract; the workbook will become a projection of this model after the JSON contract is implemented and validated.

---

## 1. Locked hierarchy

```text
recommendation_package_id
contract_version

warehouse
current_config
workload_profile
cost_baseline
assessment
evidence
candidates
scenarios
recommended_plan
package_economics
realization
provenance
observed_change
carried_forward_plan
```

### Economic semantics

```text
candidate economics
= isolated one-technique counterfactual
= not additive

scenario economics
= ordered, stateful bundled counterfactual
= authoritative total estimate for that scenario

package economics
= opportunity envelope across economically assessable scenarios
= low  = minimum LOW total-savings bound across included scenarios
= base = BASE total-savings bound of the recommended scenario
= high = maximum HIGH total-savings bound across included scenarios
```

**Unknown is null, never zero.** An estimate with `estimation_status = UNAVAILABLE` (candidate `independent_economics`, scenario `portfolio_economics`, including the `contingent_*` figures) must carry null ranges: a semantic validator rejects any number on an unavailable estimate. A candidate that is `BLOCKED` or `INSUFFICIENT_EVIDENCE` has no counterfactual attempted, so it is `UNAVAILABLE` with null ranges; a scenario whose chain needs a canary or a cost model that does not exist is `UNAVAILABLE` with null ranges and is excluded from package economics (`SCENARIO_ECONOMICS_UNAVAILABLE`). The envelope's base is the recommended scenario, so when that scenario has no authoritative price every scenario is excluded; a scenario that is itself priced is then excluded with `RECOMMENDED_SCENARIO_ECONOMICS_UNAVAILABLE` (or `NO_RECOMMENDED_SCENARIO` when no scenario is recommended), and its own economics stay on its scenario row. A known zero is different and keeps its zero: a `NO_CHANGE` candidate, or a scenario with no steps, is `NO_DELTA_MODELED` with zero savings.

`package_economics` is **not a probability/confidence interval**. Low/base/high may originate from different scenarios. Therefore DBU/AWS component values are attached to the source `scenario_envelope` point rather than represented as a misleading package-level component range.

---

## 2. Required / nullable policy

1. Every top-level key is **required**.
2. Stable nested objects use required keys as well; unavailable values are represented by explicit `null` plus a status/availability field.
3. `null` means **unknown / unavailable / not measured**. It never means zero.
4. `0` means a known measured or modeled zero.
5. `[]` means the collection was assessed and contains no entries.
6. `{}` means a known empty map. It is not used as a substitute for unknown data.
7. Unknown economic components must never be silently coerced to `$0`.
8. Currency mixing within one package is prohibited.

---

## 3. Identity model

| Identifier | Required | Nullable | Format | Meaning |
|---|---:|---:|---|---|
| `recommendation_package_id` | Yes | No | `PKG-[A-F0-9]{24}` | Immutable content-addressed assessment for one warehouse/current epoch |
| `epoch_id` | Yes | No | `EPOCH-[A-F0-9]{24}` | Authoritative warehouse configuration regime |
| `evidence_id` | Yes | No | `EVID-[A-F0-9]{24}` | Content-addressed decision evidence item |
| `technique_id` | Yes | No | `WH-...-NNN` | Stable catalog identity; does not change by warehouse/run |
| `candidate_id` | Yes | No | `CAND-[A-F0-9]{24}` | One technique decision against one warehouse/epoch/evidence set |
| `scenario_id` | Yes | No | `SCN-[A-F0-9]{24}` | One ordered mutually-exclusive execution path |
| `realization_id` | When realization exists | No | `REAL-[A-F0-9]{24}` | One applied-change validation/measurement episode |

All displayed content IDs use the first 24 uppercase hex characters (96 bits) of SHA-256. Full package SHA-256 is retained in provenance.

### Canonical hash rules

Hash payloads use:

- UTF-8;
- sorted JSON object keys;
- no insignificant whitespace;
- UTC ISO-8601 timestamps;
- numbers normalized to canonical decimal strings for identity hashing;
- ordered arrays preserved when order has semantics;
- set-like arrays sorted before entering a hash payload;
- `null` retained;
- no NaN/Infinity.

Volatile presentation metadata (`generated_at`, filenames, logs, workbook layout) is excluded from recommendation identities.

### Epoch hash inputs

```text
contract_version
workspace_id
warehouse_id
epoch_start_time
warehouse_type
warehouse_size
min_clusters
max_clusters
auto_stop_minutes
warehouse_channel
```

Photon is deliberately excluded from the configuration epoch hash because this product infers Photon from billing evidence; it is not an authoritative `system.compute.warehouses` epoch field.

### Evidence hash inputs

```text
contract_version
workspace_id
warehouse_id
epoch_id
metric
value
unit
source
source_field
observation_window
threshold
comparator
```

Human-facing `interpretation` prose is not part of the evidence ID.

### Candidate hash inputs

```text
contract_version
workspace_id
warehouse_id
epoch_id
technique_id
knob
current_value
recommended_value
sorted evidence_refs
policy_version
analyzer_version
optimizer_version
estimator_version
reviewer_version
decision_graph_version
```

### Scenario hash inputs

```text
contract_version
workspace_id
warehouse_id
epoch_id
scenario_type
ordered candidate_ids
compatibility_status
policy_version
bundle_estimator_version
decision_graph_version
```

### Package hash inputs

```text
contract_version
workspace_id
warehouse_id
current_epoch_id
canonical current_config
evidence_fingerprint
policy_version
analyzer_version
optimizer_version
modeler_version
estimator_version
bundle_estimator_version
reviewer_version
decision_graph_version
technique_catalog_version
ordered candidate_ids
ordered scenario_ids
reevaluation (status, earliest_reevaluation_time, blocking_codes; null when absent; method prose excluded)
observed_change_fingerprint (SHA256 of the canonical observed_change; null when absent)
carried_forward_fingerprint (SHA256 of the canonical whole carried_forward_plan block; null when absent)
```

### Realization hash inputs

```text
contract_version
recommendation_package_id
scenario_id
candidate_id (nullable)
validation_window_start
validation_window_end
canonical applied_config
```

---

## 4. Shared enums

### Recommendation status

`ACTIONABLE | TEST_REQUIRED | REVIEW | BLOCKED | NO_CHANGE | NOT_APPLICABLE | INSUFFICIENT_EVIDENCE`

### Execution state

`READY | READY_FOR_CANARY | PENDING_REEVALUATION | BLOCKED | REQUIRED | COMPLETED | INVALIDATED`

### Scenario status

`RECOMMENDED | ALTERNATIVE | BLOCKED | INSUFFICIENT_EVIDENCE`

### Scenario type

`IN_PLACE_OPTIMIZATION | WAREHOUSE_TYPE_MIGRATION | NO_CHANGE_BASELINE`

### In-scope tunable knobs

`warehouse_type | warehouse_size | min_clusters | max_clusters | auto_stop_minutes | warehouse_channel | enable_photon`

`enable_photon` is **reserved**: it stays in the enum so 1.0.0 packages remain valid, but the publisher no longer emits a Photon candidate (v2.7.1). The billing-observed Photon state remains in `current_config.photon`.

### Confidence

| Score | Level |
|---:|---|
| `>= 0.80` | `HIGH` |
| `>= 0.60 and < 0.80` | `MEDIUM` |
| `< 0.60` | `LOW` |
| `null` | `UNKNOWN` only |

Confidence measures evidence quality / decision support, **not probability of achieving the modeled savings**.

### Estimation status

`MODELED_RANGE | PROVISIONAL_MODELED_RANGE | PRE_CANARY_MODELED_RANGE | MEASURED | NO_DELTA_MODELED | UNAVAILABLE`

### Realization lifecycle

`NOT_APPLIED | PENDING | APPLIED | VALIDATING | REALIZED | FAILED_VALIDATION | ROLLED_BACK | SUPERSEDED`

---

# 5. Top-level fields

| Field | Type | Req. | Nullable | Semantics / constraints |
|---|---|---:|---:|---|
| `recommendation_package_id` | string | Yes | No | Deterministic package hash; immutable |
| `contract_version` | string | Yes | No | Constant `1.2.0` |
| `warehouse` | `WarehouseIdentity` | Yes | No | Warehouse/owner/region/current epoch |
| `current_config` | `CurrentConfig` | Yes | No | Current authoritative config + billing-observed Photon |
| `workload_profile` | `WorkloadProfile` | Yes | No | Usage/source profile, not tuning evidence dump |
| `cost_baseline` | `CostBaseline` | Yes | No | Historical assessed cost + forward no-change forecast |
| `assessment` | `Assessment` | Yes | No | Package-level evidence/decision summary |
| `evidence` | map[`evidence_id`, `EvidenceRecord`] | Yes | No | Canonical evidence registry; define once/reference many |
| `candidates` | array[`CandidateRecommendation`] | Yes | No | One decision per in-scope tunable knob |
| `scenarios` | array[`Scenario`] | Yes | No | Alternative/mutually-exclusive execution paths |
| `recommended_plan` | `RecommendedPlan` | Yes | No | Deterministic HITL path selection; may have null `scenario_id` if none is viable |
| `package_economics` | `PackageEconomics` | Yes | No | Executive opportunity envelope across assessable scenarios |
| `realization` | `Realization` | Yes | No | Present from day one; initially `NOT_APPLIED` with zero records |
| `provenance` | `Provenance` | Yes | No | Reproducibility/version/source metadata |
| `observed_change` | `ObservedConfigChange` | Yes | **Yes** | Observed config diff at the start of the current epoch; null for a first epoch or an empty diff (section 14.2) |
| `carried_forward_plan` | `CarriedForwardPlan` | Yes | **Yes** | Reserved for Stage 2; always null in 1.2.0 producers (section 14.3) |

---

# 6. `warehouse`

| Field | Type | Req. | Nullable | Constraints / semantics |
|---|---|---:|---:|---|
| `workspace_id` | string | Yes | No | Workspace identity as observed in system tables |
| `warehouse_id` | string | Yes | No | SQL warehouse identity |
| `warehouse_name` | string | Yes | No | Current warehouse display name |
| `owner` | string | Yes | **Yes** | Billing-attributed SQL warehouse owner; null if unavailable |
| `region` | string | Yes | No | Source Databricks region used for regional system-table evidence |
| `current_epoch` | object | Yes | No | Current config epoch |

### `warehouse.current_epoch`

| Field | Type | Req. | Nullable | Constraints |
|---|---|---:|---:|---|
| `epoch_id` | string | Yes | No | Deterministic `EPOCH-*` |
| `start_time` | date-time | Yes | No | Epoch start / config `change_time`, UTC-normalized |
| `age_days` | integer | Yes | No | `>= 0`, evaluated at analysis time |
| `is_current` | boolean | Yes | No | Must be `true` in a recommendation package |

---

# 7. `current_config`

| Field | Type | Req. | Nullable | Constraints / source semantics |
|---|---|---:|---:|---|
| `warehouse_type` | string | Yes | No | System-table authoritative |
| `warehouse_size` | string | Yes | No | System-table authoritative |
| `min_clusters` | integer | Yes | No | `1..40` |
| `max_clusters` | integer | Yes | No | `min_clusters..40` |
| `auto_stop_minutes` | integer | Yes | No | `>= 0` |
| `warehouse_channel` | string | Yes | No | System-table authoritative |
| `photon` | object | Yes | No | Billing-observed current-epoch operational state |

### `current_config.photon`

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `status` | enum | Yes | No | `ENABLED | DISABLED | MIXED | UNKNOWN` |
| `evidence_source` | string | Yes | No | Expected billing/system-table source |
| `confidence` | `Confidence` | Yes | No | Confidence in inferred operational state |
| `enabled_usage_pct` | number | Yes | Yes | `0..100`; null if unobservable |
| `known_feature_coverage_pct` | number | Yes | Yes | `0..100`; non-null feature coverage |
| `freshness_days` | integer | Yes | Yes | `>=0`; null if no dated evidence |

`UNKNOWN` Photon status cannot carry `HIGH` or `MEDIUM` confidence.

---

# 8. `workload_profile`

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `primary_query_source` | string | Yes | No | Presentation-oriented primary source; must appear in `query_sources` when map non-empty |
| `query_sources` | map[string, `QuerySourceStat`] | Yes | No | Multi-label Databricks entity/source categories |
| `client_applications` | map[string, `ClientApplicationStat`] | Yes | No | Power BI/Tableau/SQL Editor/etc. attribution |
| `activity` | `WorkloadActivity` | Yes | No | Recent activity summary |
| `workload_characteristics` | object | Yes | No | Deterministic coarse workload classification |
| `multi_label` | boolean | Yes | No | Must be `true`; source percentages need not sum to 100% |

### Query source stat

- `query_count`: integer `>=0`
- `query_pct`: number `0..100`

### Client application stat

- `query_count`: integer `>=0`

### Activity

- `queries_30d`: integer `>=0`
- `active_days_30d`: integer `0..30`
- `queries_per_active_day`: number `>=0`

### Workload characteristics enums

- `latency_class`: `INTERACTIVE | MIXED | BATCH | UNKNOWN`
- `concurrency_pattern`: `STEADY | BURSTY | MIXED | UNKNOWN`
- `activity_pattern`: `CONTINUOUS | BUSINESS_HOURS | SCHEDULED | SPORADIC | UNKNOWN`

---

# 9. `cost_baseline`

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `currency` | string | Yes | No | Uppercase 3-letter currency; package-wide |
| `actual_365d` | `CostBreakdown` | Yes | No | Historical 365-day assessed cost; basis declares observed vs modeled components |
| `forward_365d` | `CostBreakdown` | Yes | No | No-change forward 365-day baseline |
| `forecast` | `ForecastInfo` | Yes | No | Forecast method/confidence/assumptions |

### `CostBreakdown`

| Field | Type | Req. | Nullable | Rules |
|---|---|---:|---:|---|
| `dbu_cost` | number | Yes | Yes | `>=0` when known |
| `aws_cost` | number | Yes | Yes | `>=0` when known |
| `total_cost` | number | Yes | Yes | `>=0`; when complete, must equal DBU + AWS within rounding tolerance |
| `availability` | enum | Yes | No | `COMPLETE | PARTIAL | UNAVAILABLE` |
| `dbu_basis` | enum | Yes | No | Source/model basis for DBU component |
| `aws_basis` | enum | Yes | No | Source/model basis for AWS component |

Availability semantics:

- `COMPLETE`: DBU, AWS, total all non-null.
- `PARTIAL`: exactly one of DBU/AWS known; total must be null.
- `UNAVAILABLE`: all cost values null.
- If a component is null its corresponding basis must be `UNAVAILABLE`; if populated, basis cannot be `UNAVAILABLE`.

Cost basis enum:

`OBSERVED_DATABRICKS_BILLING | OBSERVED_AWS_BILLING | MODELED_AWS_LIST_PRICE | MODELED_CURRENT_EPOCH | FORECAST | COUNTERFACTUAL | UNAVAILABLE`

#### Price semantics (apply to every cost and savings figure in the package)

- **DBU dollars are list-price dollars.** `OBSERVED_DATABRICKS_BILLING` and `FORECAST` DBU values are observed `system.billing.usage` quantity multiplied by the published `system.billing.list_prices` `pricing.effective_list.default`. They are not the customer's invoiced, discounted or contract-effective price, and they do not include commitment discounts, credits or taxes. Usage with no matching list-price row is not priced: when it falls in the trailing 365-day window the observed DBU cost is null, and when it falls in the forecast basis the forecast DBU cost is null (unknown cost is null, never zero).
- **AWS dollars are on-demand list-price dollars.** `MODELED_AWS_LIST_PRICE` values use the versioned regional on-demand EC2 registry. They do not model Spot. Pro/Classic warehouses can run with a Spot policy (the Warehouses API exposes `spot_instance_policy`, default `COST_OPTIMIZED`), so actual AWS spend and AWS savings can be lower than the modeled figures. The package does not observe the policy or the Spot mix.
- Consequently savings are *modeled list-price* savings. Treat them as relative opportunity sizing and reconcile against invoices or CUR before financial reporting. Integrating effective rates or a Spot mix is a separate, not yet scheduled source contract.

### Forecast

- `model`: required non-empty string.
- `confidence`: standard Confidence object.
- `assumptions`: unique string list; factual/modeling assumptions only.

---

# 10. `assessment`

### Overall status

`OPTIMIZATION_AVAILABLE | NO_CHANGE | REVIEW_REQUIRED | BLOCKED | INSUFFICIENT_EVIDENCE`

### Summary fields

All are required non-negative integers:

- `candidate_count`
- `actionable_count`
- `test_required_count`
- `review_count`
- `blocked_count`
- `no_change_count`
- `not_applicable_count`
- `insufficient_evidence_count`

`candidate_count` must equal the sum of all status counts and the actual candidate array length.

### Evidence quality

| Field | Type | Nullable | Constraints |
|---|---|---:|---|
| `status` | enum | No | `SUFFICIENT | PARTIAL | INSUFFICIENT` |
| `confidence` | Confidence | No | Standard confidence rules |
| `query_sample_sufficient` | bool | No | Explicit decision evidence flag |
| `cost_history_sufficient` | bool | No | Explicit economic evidence flag |
| `epoch_mature` | bool | No | Explicit current-epoch maturity flag |
| `source_coverage_pct` | number | No | `0..100` |

---

# 11. `evidence`

`evidence` is a map keyed by `evidence_id`; the key must exactly equal the nested record's `evidence_id`.

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `evidence_id` | string | Yes | No | Content-addressed evidence identity |
| `metric` | string | Yes | No | Stable normalized metric name |
| `value` | JSON value | Yes | Yes | Measured/derived value; null only if the evidence statement itself is about unavailable state |
| `unit` | string | Yes | No | e.g. `percent`, `ms`, `bytes`, `clusters`, `boolean` |
| `source` | string | Yes | No | System table / compatibility evidence / `derived` |
| `source_field` | string | Yes | No | Physical field or deterministic derivation name |
| `epoch_scope` | string | Yes | No | Normally `CURRENT`; explicit if historical calibration evidence |
| `observation_window` | string | Yes | No | e.g. `30d`, `current_epoch`, `point_in_time` |
| `threshold` | JSON value | Yes | Yes | Policy threshold where applicable |
| `comparator` | string | Yes | No | Human/audit comparator representation |
| `interpretation` | string | Yes | No | Human explainability; excluded from evidence ID |

Evidence is defined once and referenced from candidates/scenarios/realization.

---

# 12. `candidates[]`

Exactly one candidate decision is expected for every in-scope tunable knob assessed by the product.

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `candidate_id` | string | Yes | No | Deterministic recommendation instance |
| `technique_id` | string | Yes | No | Stable technique catalog key |
| `knob` | enum | Yes | No | In-scope tunable knob |
| `status` | enum | Yes | No | Recommendation status |
| `current_value` | scalar | Yes | Yes | Current knob value; null only when not applicable/unavailable |
| `recommended_value` | scalar | Yes | Yes | Proposed value; null when no meaningful target exists |
| `objectives` | enum[] | Yes | No | At least one; unique |
| `rationale` | string | Yes | No | Deterministic/explainable reason summary |
| `evidence_refs` | evidence_id[] | Yes | No | Unique; every reference must exist |
| `blockers` | code[] | Yes | No | Uppercase stable blocker codes; empty when none |
| `dependencies` | `DependencyRef[]` | Yes | No | Explicit prerequisites |
| `invalidates` | knob[] | Yes | No | Downstream decision evidence invalidated by applying candidate |
| `confidence` | Confidence | Yes | No | Candidate decision/evidence confidence |
| `independent_economics` | object | Yes | No | Isolated non-additive counterfactual |

Objectives:

`COST | PERFORMANCE | COST_AND_PERFORMANCE | COST_GUARDRAIL | STABILITY | RELIABILITY | OPERATIONS | MIGRATION`

Status-specific rules:

- `NO_CHANGE`: `current_value == recommended_value`.
- `BLOCKED` / `INSUFFICIENT_EVIDENCE`: at least one blocker required.
- `NOT_APPLICABLE`: cannot claim non-zero savings.

### Dependencies

Each dependency has:

- `dependency_type`: `CANDIDATE | TECHNIQUE | KNOB | COMPATIBILITY_CHECK | EVIDENCE`
- `ref_id`: non-empty stable reference
- `condition`: nullable human-readable deterministic condition

### Independent economics

| Field | Type | Req. | Semantics |
|---|---|---:|---|
| `is_additive` | bool | Yes | Must be `false` |
| `estimation_status` | enum | Yes | Economic maturity/state |
| `baseline_365d` | CostBreakdown | Yes | Same no-change economic baseline for the candidate |
| `estimated_cost_365d` | ComponentRanges | Yes | DBU/AWS/total low/base/high; component may be null if unavailable |
| `savings_365d` | ComponentRanges | Yes | DBU/AWS/total low/base/high; savings may be negative for a cost-increasing performance recommendation |

For candidate/scenario `ComponentRanges`, when DBU, AWS and total are all present, `total = DBU + AWS` at each low/base/high bound.

Estimated cost ranges must be non-negative. Savings ranges may be negative.

---

# 13. `scenarios[]`

Scenarios are mutually exclusive/alternative execution strategies, not candidate lists to be added together.

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `scenario_id` | string | Yes | No | Deterministic execution-path identity |
| `scenario_name` | string | Yes | No | Human-readable stable scenario name |
| `scenario_type` | enum | Yes | No | In-place / migration / no-change baseline |
| `status` | enum | Yes | No | Exactly one `RECOMMENDED` when recommended plan exists |
| `candidate_refs` | candidate_id[] | Yes | No | Ordered candidate references used to construct scenario ID |
| `evidence_refs` | evidence_id[] | Yes | No | Scenario-level evidence references |
| `confidence` | Confidence | Yes | No | Scenario confidence; recommended default method is bottleneck/minimum step confidence |
| `compatibility` | Compatibility | Yes | No | Explicit compatibility status/checks |
| `portfolio_economics` | ScenarioEconomics | Yes | No | Stateful non-overlapping economics |
| `execution_plan` | PlanStep[] | Yes | No | Contiguous steps starting at 1 |

### Compatibility

- status: `COMPATIBLE | REQUIRES_VALIDATION | INCOMPATIBLE | UNKNOWN`
- mode: `B_READ_ONLY_API | A_HITL_FALLBACK | B_READ_ONLY_API_WITH_A_FALLBACK | SYSTEM_TABLE_ONLY`
- checks[]: `check`, `status`, `requires_hitl`, `evidence_refs`, nullable `details`

Check status: `PASS | FAIL | UNKNOWN | NOT_APPLICABLE`.

### Scenario economics

Same DBU/AWS/total `ComponentRanges` structure as candidate economics, but produced by the **stateful bundle estimator**.

- `is_additive` must be false.
- `method` is required.
- `estimated_cost_365d` / `savings_365d` are the **authoritative** economic estimate for that scenario: they price only steps whose evidence has not been invalidated upstream, i.e. every step whose `execution_state` is not `PENDING_REEVALUATION`. Package economics and the scenario envelope derive from these figures only.
- `contingent_estimated_cost_365d` / `contingent_savings_365d` (1.1.0, `ComponentRanges`, nullable) are the stateful upside of the whole chain including `PENDING_REEVALUATION` steps, priced from pre-change evidence. They are **not authoritative**, never feed package economics, and must be regenerated after upstream remeasurement. They are published together or both null, and null when no step is pending or when the chain cannot be priced (unknown cost is null, never zero).
- Savings are signed: positive = savings, negative = cost increase.

### Execution plan step

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `step` | integer | Yes | No | `>=1`; scenario steps contiguous |
| `step_type` | enum | Yes | No | Candidate step or explicit control action |
| `candidate_id` | string | Yes | Yes | Required for `CANDIDATE` step |
| `technique_id` | string | Yes | Yes | Required for `CANDIDATE` step |
| `knob` | enum | Yes | Yes | Required for `CANDIDATE` step |
| `action` | string | Yes | Yes | Required for non-candidate action step |
| `current_value` | scalar | Yes | Yes | Current value where applicable |
| `recommended_value` | scalar | Yes | Yes | Target value where applicable |
| `execution_state` | enum | Yes | No | Readiness/invalidated state |
| `confidence` | Confidence | Yes | No | Must come from candidate for candidate steps |
| `validation` | ValidationGate | Yes | No | Canary/HITL/remeasure requirements |
| `dependencies` | DependencyRef[] | Yes | No | Explicit prerequisites |
| `invalidates` | knob[] | Yes | No | Downstream knobs invalidated if step is applied |
| `projected_economics` | StepProjection | Yes | Yes | 1.1.0. Cumulative stateful savings after this step and its pricing basis; null when the chain could not price the step |

`StepProjection`: `pricing_basis` (`AUTHORITATIVE | CONTINGENT_PENDING_REEVALUATION`) and `running_savings_365d` (`ComponentRanges`, signed, cumulative: the savings of the plan up to and including this step). Cumulative ranges are published rather than per-step increments because the difference of low/base/high ranges is not itself a low/base/high range.

Invariants (semantic validators):

- A `PENDING_REEVALUATION` step is priced `CONTINGENT_PENDING_REEVALUATION`; every other priced step is `AUTHORITATIVE`.
- Scenario `savings_365d` equals the `running_savings_365d` of the last `AUTHORITATIVE` step (within 0.03).
- `contingent_savings_365d` is present only if at least one step is `PENDING_REEVALUATION`, and equals the `running_savings_365d` of the last contingent step.

Re-validation on application: Phase 1 has no apply API. Contingent figures are a big-picture view only; after an upstream step is applied and remeasured, the next publisher run produces a new package whose authoritative economics include the next knob.

Step type:

`CANDIDATE | REMEASURE | COMPATIBILITY_VALIDATION | CANARY | COMPARE | ROLLBACK_GATE`

Validation type:

`NONE | A_B_CANARY | HITL | COMPATIBILITY | REMEASURE`

`remeasure_days`, when present, must be `1..90`.

---

# 14. `recommended_plan`

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `scenario_id` | string | Yes | **Yes** | Null only if no scenario can be recommended |
| `selection_reason_code` | code | Yes | No | Stable machine-readable reason |
| `next_action` | string | Yes | No | Next HITL/workflow action |
| `requires_hitl` | bool | Yes | No | Explicit approval/validation flag |
| `decision_summary` | string | Yes | No | Human-facing summary; not part of recommendation identity |
| `reevaluation` | `ReevaluationWindow` | Yes | **Yes** | Time-clearable gates that currently hide the next step; null once both are clear |

If `scenario_id` is non-null, exactly one scenario must be `RECOMMENDED` and it must match this ID.

## 14.1 `reevaluation` (new in 1.2.0)

| Field | Type | Nullable | Semantics |
|---|---|---:|---|
| `status` | enum | No | `WAITING_FOR_EVIDENCE` (the only value) |
| `earliest_reevaluation_time` | ISO-8601 UTC | No | Later of the active gates. **Earliest only**: billing ingestion lag and query-sample gates can delay it |
| `blocking_codes` | string[] | No | Non-empty, sorted, unique subset of `COST_HISTORY_INSUFFICIENT`, `CURRENT_CONFIG_EPOCH_TOO_NEW` |
| `method` | string | No | Prose; excluded from identity |

`earliest_reevaluation_time` is a lower bound rounded up to the next second. When the only active gate is `COST_HISTORY_INSUFFICIENT` on a mature epoch, the missing billing days have already elapsed but are not yet ingested (the billing watermark counts only covered days), so the time can already be in the past; the next run after ingestion clears the gate. The publisher then uses `selection_reason_code = BILLING_HISTORY_ACCRUING` instead of `CURRENT_EPOCH_IMMATURE`.

Gates (constants embedded in the validator): `CURRENT_CONFIG_EPOCH_TOO_NEW` is active while `warehouse.current_epoch.age_days < 7`; `COST_HISTORY_INSUFFICIENT` is active while the package's `cost_history_days` evidence value is `< 7`. The window is non-null exactly when at least one gate is active, and `blocking_codes` equals the active gates.

```text
t_maturity = epoch start + 7 days
t_cost     = 00:00Z of (epoch start date + 7 days)
earliest_reevaluation_time = max(t_x for each active gate)
```

Example: epoch start `2026-10-01T15:00:00Z` with both gates active gives `2026-10-08T15:00:00Z`.

`next_action = WAIT_FOR_REEVALUATION` (with `selection_reason_code = CURRENT_EPOCH_IMMATURE`) is used only when `reevaluation` is non-null **and** the recommended scenario has no step in `READY` or `READY_FOR_CANARY`. A young epoch can still carry a ready step (for example a max-cluster increase for queueing); the usual next action then applies. Changing the 7-day gate lengths in the publisher policy requires a contract version bump.

## 14.2 `observed_change` (new in 1.2.0)

A record that the authoritative configuration (`system.compute.warehouses`) changed at the start of the current epoch. It is **not** a realization record and has no cost or savings fields; `realization.status` stays `NOT_APPLIED`.

| Field | Type | Nullable | Semantics |
|---|---|---:|---|
| `previous_epoch_id` | `EPOCH-*` | No | The immediately preceding epoch of the same `(workspace_id, warehouse_id)` |
| `previous_epoch_start` | ISO-8601 | No | Start of that epoch |
| `changed_at` | ISO-8601 | No | Must equal `warehouse.current_epoch.start_time` |
| `changes[]` | `{knob, previous_value, current_value}` | No | Non-empty, sorted and unique by `knob`; values differ |
| `measurement_status` | enum | No | `NOT_MEASURED` (the only value) |

Null when the current epoch is the first in the retained history window or the config is identical. The change can be months old; consumers should read `changed_at`.

## 14.3 `carried_forward_plan` (reserved; always null in 1.2.0 producers)

A contingent replay of the preceding epoch's plan, planned for Stage 2. Its structure and invariants are defined and validated now so the contract does not change again when it ships. It never feeds candidate, scenario or package economics (pricing basis `CONTINGENT_CARRIED_FORWARD`, which plan steps may not use). Fields: `source_epoch_id`, `as_of`, `match_policy` (`EXACT_SINGLE_KNOB`), `match_outcome` (`MATCHED` | `MISMATCH` | `NOT_REPLAYABLE`), `match_reason_code`, `replay_fingerprint`, `steps[]` (`order`, `knob`, `technique_id`, `previous_value`, `recommended_value`, `disposition` of `COMPLETED` | `PENDING_REEVALUATION` | `INVALIDATED`, `reason_code`, `rebased_running_savings_365d`), `remaining_upside_365d`, `pricing_basis`. Savings are **rebased**: running savings after the last `COMPLETED` step are subtracted track by track, so an applied step is not counted again as upside. A non-null block requires `observed_change` and an active `CURRENT_CONFIG_EPOCH_TOO_NEW` gate (the epoch is younger than the maturity gate), and `source_epoch_id` must equal `observed_change.previous_epoch_id`. The package hash covers the whole block (`carried_forward_fingerprint`); `replay_fingerprint` additionally binds it to the replay evidence. The validator applies the same patterns as the schema (uppercase hex fingerprints, `^[A-Z][A-Z0-9_]*$` codes).

---

# 15. `package_economics`

`package_economics` is an executive opportunity envelope over **economically assessable scenarios**.

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `currency` | string | Yes | No | Must match package `cost_baseline.currency` |
| `baseline_365d` | CostBreakdown | Yes | No | Common forward no-change baseline |
| `estimated_savings_365d` | Range3 | Yes | **Yes** | **Total-only** scenario envelope; null if no assessable scenarios |
| `estimated_cost_365d` | Range3 | Yes | **Yes** | Total-only future-cost envelope; null if no assessable scenarios |
| `scenario_envelope` | object | Yes | No | Exact source scenario/bound for low/base/high |
| `included_scenario_ids` | string[] | Yes | No | Every scenario with defensible economics |
| `excluded_scenarios` | object[] | Yes | No | Every non-assessable scenario + reason code |
| `semantics` | object | Yes | No | Must declare non-additive/non-probabilistic envelope |

Every scenario must be classified as either included or excluded; no silent omission.

### Total package savings

```text
low  = minimum scenario LOW total-savings bound across included scenarios
base = recommended scenario BASE total-savings bound
high = maximum scenario HIGH total-savings bound across included scenarios
```

### Total package future cost

If total forward baseline is known:

```text
low  = baseline - high_savings
base = baseline - base_savings
high = baseline - low_savings
```

### Why package DBU/AWS are not represented as a Range3

Low/base/high can come from different scenarios. A DBU low from one scenario and AWS low from another would create a synthetic combination that corresponds to no real scenario. Instead each envelope point stores the exact source scenario economics:

```text
scenario_envelope.minimum_savings
scenario_envelope.recommended_base
scenario_envelope.maximum_savings
```

Each point carries:

- source `scenario_id`
- source `bound` (`LOW | BASE | HIGH`)
- exact DBU/AWS/total savings at that scenario bound
- exact DBU/AWS/total estimated cost at that scenario bound

Semantics must be:

```json
{
  "method": "SCENARIO_ENVELOPE",
  "is_additive": false,
  "is_probability_interval": false
}
```

---

# 16. `realization`

The section is **always present**, including before any recommendation is applied.

Initial state:

```json
{
  "status": "NOT_APPLIED",
  "realization_records": [],
  "aggregate": {
    "measured_candidates": 0,
    "successful_candidates": 0,
    "rolled_back_candidates": 0,
    "currency": "USD",
    "realized_savings_to_date": 0,
    "annualized_realized_savings": 0
  }
}
```

### `realization_records[]`

| Field | Type | Req. | Nullable | Semantics |
|---|---|---:|---:|---|
| `realization_id` | string | Yes | No | Deterministic measurement episode identity |
| `recommendation_package_id` | string | Yes | No | Immutable source package reference |
| `scenario_id` | string | Yes | No | Applied/validated scenario |
| `candidate_id` | string | Yes | Yes | Null for scenario-wide realization episode |
| `technique_id` | string | Yes | Yes | Technique when candidate-specific |
| `applied` | object | Yes | No | Applied timestamp/actor/previous/applied configs |
| `validation` | object | Yes | No | Measurement window, guardrails, finding refs |
| `observed_cost` | object | Yes | No | DBU/AWS/total observed/assessed cost over validation window |
| `realized_savings` | object | Yes | No | Measured savings and annualized total |
| `outcome` | object | Yes | No | Success/rollback/learning outcome |

Realized savings may be negative. Observed costs cannot be negative.

`recommendation_accuracy`, when available, is `0..1` and represents agreement between predicted and realized outcome under the product's defined accuracy formula; the formula must be versioned outside presentation prose.

Realization never changes the original recommendation package ID.

---

# 17. `provenance`

All fields are required; `package_hash_full_sha256` is required as a key but nullable until final package hash materialization.

| Field | Semantics |
|---|---|
| `contract_version` | Must equal `1.0.0` |
| `policy_version` | Threshold/policy bundle version |
| `analyzer_version` | Analyzer implementation version |
| `optimizer_version` | Optimizer version |
| `modeler_version` | Forward model version |
| `estimator_version` | Candidate estimator version |
| `bundle_estimator_version` | Stateful scenario estimator version |
| `reviewer_version` | Invariant/review implementation version |
| `decision_graph_version` | Dependency/sequencing rules version |
| `technique_catalog_version` | Technique definitions version |
| `evidence_fingerprint` | Full `SHA256:<64 uppercase hex>` over decision evidence |
| `source_tables` | Unique non-empty source list |
| `source_regions` | Unique non-empty region list |
| `generated_at` | ISO-8601 output metadata; excluded from IDs |
| `package_hash_full_sha256` | Full 64-char package identity SHA-256 |

---

# 18. Semantic validations beyond JSON Schema

JSON Schema enforces shape, types, enums, nullability, patterns, and basic numeric ranges. The Python dataclass validator additionally enforces cross-field/business invariants that JSON Schema should not attempt to encode opaquely:

1. epoch ID recomputes from current authoritative config;
2. every evidence ID recomputes from its declared evidence payload;
3. evidence fingerprint recomputes from the evidence registry;
4. every candidate ID recomputes from current package/provenance inputs;
5. every scenario ID recomputes from its ordered candidate set and compatibility state;
6. package ID and full package SHA-256 recompute;
7. realization IDs recompute;
8. confidence level matches confidence score thresholds;
9. cost availability agrees with nullable components;
10. complete cost totals equal DBU + AWS;
11. candidate/scenario low/base/high ranges are ordered;
12. candidate/scenario total ranges equal DBU + AWS when all components are present;
13. candidate assessment counts equal actual statuses;
14. all candidate/scenario/evidence references resolve;
15. scenario steps are contiguous;
16. exactly one recommended scenario matches `recommended_plan` when a recommendation exists;
17. every scenario is classified as economically included or explicitly excluded;
18. package low/base/high savings are recomputed from scenario ranges;
19. package future-cost envelope is recomputed from baseline and savings;
20. package low/base/high envelope source scenarios are verified;
21. currency mixing is rejected;
22. `NOT_APPLIED` realization cannot carry measurement records;
23. `reevaluation` is non-null exactly when a gate is active, `blocking_codes` equals the active gates, and `earliest_reevaluation_time` recomputes from the epoch start and gates (never before the epoch start);
24. `next_action = WAIT_FOR_REEVALUATION` requires a `reevaluation` window and no `READY`/`READY_FOR_CANARY` step in the recommended scenario;
25. `observed_change.changed_at` equals the current epoch start, its `previous_epoch_id` differs from the current epoch, and its entries are sorted, unique and differ;
26. `carried_forward_plan` (when present) satisfies the structural rules in section 14.3 and the cross-references to `observed_change` and `reevaluation`;
27. plan steps may only use the `AUTHORITATIVE` or `CONTINGENT_PENDING_REEVALUATION` pricing basis.

---

# 19. Contract versioning

Reporter contract versions follow semantic versioning:

- **MAJOR:** breaking field removal/rename, semantic change, required field addition, enum removal/meaning change, hash-input change.
- **MINOR:** backward-compatible optional extension or newly recognized enum value where consumers are defined to tolerate unknown future values.
- **PATCH:** documentation/description clarification with no schema or hash-semantic change.

Because the v1 schema is deliberately strict (`additionalProperties: false`), implementation must explicitly upgrade contract support before emitting a new field.

Any change to deterministic hash inputs is a **major contract change** unless a new hash-version field is introduced and both schemes coexist.

**Amendment (1.2.0):** MINOR also covers adding required-but-nullable keys that are null for existing semantics, including new keys in the package hash payload, provided `contract_version` is part of every hash (so old and new IDs can never collide) and the schema is regenerated. Consumers must still upgrade before reading the new keys because `additionalProperties` is `false`. Contract 1.1.0 already followed this reading.

---

# 20. Source-of-truth artifacts

1. **Semantic contract:** `docs/contracts/reporter-json-contract.md`
2. **Python executable contract:** `contracts/reporter_contract.py`
3. **Schema builder:** `contracts/reporter_schema.py`
4. **Generated JSON Schema:** `schema/recommendation-package.schema.json`
5. **Canonical example:** `examples/recommendation-package.example.json`
6. **Contract tests:** `tests/test_reporter_contract.py`

The workbook is intentionally not part of Reporter Contract v1.2.0. It will be designed only after the publisher emits this JSON contract without adapter-specific semantic loss.
