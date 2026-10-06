# Workbook Reporter Contract v1.5.0

**Version history:** 1.5.0 projects the Reporter 3.0.0 `cost_profile`. *Warehouse Portfolio* adds `cost_profile` and `profile_source` (the source, with the tag or flag that chose it in parentheses) at the end of the *Identity / epoch* group; *Recommendation Registry* adds `cost_profile` at the end of the *Identity* group; *Executive Summary* adds a *Cost profiles* section with one row per profile (warehouse counts, zeros included). Sheets, grains and all other columns are unchanged.

1.4.0 projects Reporter 1.3.0 per-step savings and separates plan savings from experiment upside. Execution-plan text (Warehouse Portfolio and Scenario Plans) appends `; step saves N/yr (base)` (or `; step costs N/yr more (base)`) to each priced step. *Recommendation Registry* adds `plan_step`, `plan_step_savings_base_365d` and `plan_step_pricing_basis` (group *Recommended plan step*) for candidates in the recommended scenario, and `—` for other candidates. *Executive Summary* adds the rows *Excluded (unsupported type)*, *Plan savings low/base/high* (section *Recommended plan savings*) and *Migration experiment high* (section *Experiment upside*). Sheets, grains and all other columns are unchanged.

**Earlier history:** 1.3.0 adds three text columns to *Warehouse Portfolio* (`earliest_reevaluation_time`, `reevaluation_blockers`, `observed_change`, in the *Recommended plan* group) projecting the Reporter 1.2.0 `recommended_plan.reevaluation` and `observed_change`. Null values render as the explicit `—` state, never blank. `observed_change` text includes the change date, for example `warehouse_size MEDIUM→LARGE (2026-09-29)`. Sheets, grains and all other columns are unchanged.

1.2.0 adds three Scenario Plans columns (`contingent_total_low/base/high_impact`, group *Contingent upside (pending re-evaluation)*) and a `; priced AUTHORITATIVE|CONTINGENT` tag per plan step in the execution-plan text. All other sheets, grains and columns are unchanged. Executive Summary and Warehouse Portfolio use authoritative values only.

**Status:** Locked  
**Source of truth:** validated Reporter JSON Contract v3.0.0 packages  
**Implementation:** `src/publisher/contracts/workbook_contract.py` + `src/publisher/reporter/workbook_reporter.py`

The workbook is a human decision/reporting projection of immutable `RecommendationPackage` JSON. It never recalculates optimization decisions, confidence, deterministic IDs, candidate economics, scenario economics, or package economics.

## Global invariants

- Exactly four visible sheets; no hidden sheets or macros.
- No optimization/business formulas in Excel cells.
- Every detailed row carries `recommendation_package_id`.
- Financial fields are numeric when available, not decorated text.
- Currency is explicit on every detailed economic grain.
- Signed annual impact semantics are preserved: **positive = savings; negative = cost increase; zero = known no change**.
- An adjacent impact classification (`SAVINGS`, `COST_INCREASE`, `MIXED`, `NO_CHANGE`, `UNAVAILABLE`) makes direction explicit.
- Unavailable/null economics render as `UNAVAILABLE`, never financial zero.
- Candidate economics are independent/non-additive.
- Scenario/package economics are copied from the stateful JSON contract.
- Verbose JSON/audit fields are placed at the far right; primary decision fields remain readable/filterable.

## 1. Executive Summary

**Persona:** executive/product sponsor, FinOps lead, platform owner.  
**Grain:** run/portfolio aggregation over the supplied packages.

Presents coverage, including the count of warehouses excluded by the Governor as unsupported types. That count comes from the run manifest's `excluded_warehouses` and is `UNAVAILABLE` when the caller passes no exclusion list. It also shows the actual/forward baseline, recommended annual savings, recommended annual cost increase and signed net annual impact, followed by two separate blocks:

- **Recommended plan savings:** sums of the recommended scenario's total low, base and high over warehouses whose recommended scenario is priced. The note states how many are priced; unpriced plans are excluded, never counted as zero. The base equals the net annual impact.
- **Pricing basis:** the *Forward 365d baseline* and *Plan savings* notes state that DBU cost is at the account's list price (negotiated discounts lower real savings proportionally) and AWS cost at on-demand EC2 prices (Spot, Reserved Instances and Savings Plans are not modeled).
- **Experiment upside:** the sum of the positive total `high` of each package's priced, non-blocked migration-experiment scenario. It is an alternative to the recommended plan, not added to it, and unvalidated until the canary.

The package scenario envelope (which an experiment can set), confidence posture, the number of warehouses evaluated under each cost profile (aggressive, balanced, conservative; zeros included) and up to 15 highest-impact warehouses follow. Financial cells are numeric and chartable.

## 2. Warehouse Portfolio

**Grain:** one row per `recommendation_package_id`.

Groups: identity/epoch (ending with the cost profile and how it was chosen); workload; current configuration; currency and actual/forward baseline; signed package low/base/high annual impacts and counterfactual costs; recommended scenario; assessment; compact human decision/plan views; traceability/audit JSON.

The exact ordered set is `PORTFOLIO_COLUMNS` in `src/publisher/contracts/workbook_contract.py` (**63 columns**) and is regression-tested.

## 3. Scenario Plans

**Grain:** one row per `scenario_id`.

Groups: package/warehouse identity; scenario; confidence; compatibility; currency and baseline; signed DBU/AWS/total low/base/high annual impacts; estimated total cost; execution plan; dependency/invalidation/remeasure fields; audit JSON.

The exact ordered set is `SCENARIO_COLUMNS` (**45 columns**) and is regression-tested.

## 4. Recommendation Registry

**Grain:** one row per `candidate_id`.

Groups: package/candidate/technique identity (ending with the warehouse's cost profile); decision/current/recommended value; confidence; currency and independent counterfactual economics; realization summary; decision rule; candidate-specific evidence, thresholds, dependencies, invalidations, blockers, scenario membership and realization audit JSON. The *Recommended plan step* group (after independent economics) shows the candidate's step number, its own base savings and its pricing basis in the recommended scenario: `—` when the candidate is not in that scenario, `UNAVAILABLE` when the step is unpriced.

The exact ordered set is `REGISTRY_COLUMNS` (**57 columns**) and is regression-tested.

## Excel presentation contract

- Rows 1–2: title/context.
- Row 3: section bands.
- Row 4: filterable column headers.
- Freeze title/header rows and key identity columns.
- Excel tables on detailed sheets.
- Numeric cost/impact format: `#,##0.00;[Red](#,##0.00);-` (accounting-style negative parentheses).
- Cost and impact figures are modeled **list-price** dollars (DBU at `system.billing.list_prices`, AWS at on-demand EC2 list price, no Spot or contract discounts); see *Price semantics* in the Reporter JSON Contract. The column set and headers are unchanged.
- Percent/confidence fields use numeric percentage formats.
- IDs are text, preventing precision loss.
- Status/impact/confidence fields use presentation-only conditional formatting.
- Human rationale/plan fields wrap; large JSON audit fields remain compact/unwrapped to prevent pathological row heights.
- Executive charts use positive helper series for display only; underlying package economics remain signed and authoritative.

## Null / zero semantics

| Value | Meaning | Workbook |
|---|---|---|
| `null` | unavailable / unknown | `UNAVAILABLE` / explicit state |
| `0` | known zero impact/cost | numeric zero |
| `> 0` impact | savings | positive numeric + `SAVINGS` |
| `< 0` impact | cost increase | negative numeric shown with parentheses + `COST_INCREASE` |

## Version change from v1.0.1

v1.1.0 replaces decorated economic strings such as `Save USD ...` / `Cost +USD ...` with professional numeric cells plus explicit `currency` and `impact_type` fields. Sheet grains remain unchanged.
