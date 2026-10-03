# Workbook Reporter Contract v1.2.0

**Version history:** 1.2.0 adds three Scenario Plans columns (`contingent_total_low/base/high_impact`, group *Contingent upside (pending re-evaluation)*) and a `; priced AUTHORITATIVE|CONTINGENT` tag per plan step in the execution-plan text. All other sheets, grains and columns are unchanged. Executive Summary and Warehouse Portfolio use authoritative values only.

**Status:** Locked  
**Source of truth:** validated Reporter JSON Contract v1.1.0 packages  
**Implementation:** `contracts/workbook_contract.py` + `reporters/workbook_reporter.py`

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

Presents coverage, actual/forward baseline, recommended annual savings, recommended annual cost increase, signed net annual impact, scenario low/high envelope, confidence posture, exclusions, and up to 15 highest-impact warehouses. Financial cells are numeric and chartable.

## 2. Warehouse Portfolio

**Grain:** one row per `recommendation_package_id`.

Groups: identity/epoch; workload; current configuration; currency and actual/forward baseline; signed package low/base/high annual impacts and counterfactual costs; recommended scenario; assessment; compact human decision/plan views; traceability/audit JSON.

The exact ordered set is `PORTFOLIO_COLUMNS` in `contracts/workbook_contract.py` (**58 columns**) and is regression-tested.

## 3. Scenario Plans

**Grain:** one row per `scenario_id`.

Groups: package/warehouse identity; scenario; confidence; compatibility; currency and baseline; signed DBU/AWS/total low/base/high annual impacts; estimated total cost; execution plan; dependency/invalidation/remeasure fields; audit JSON.

The exact ordered set is `SCENARIO_COLUMNS` (**42 columns**) and is regression-tested.

## 4. Recommendation Registry

**Grain:** one row per `candidate_id`.

Groups: package/candidate/technique identity; decision/current/recommended value; confidence; currency and independent counterfactual economics; realization summary; decision rule; candidate-specific evidence, thresholds, dependencies, invalidations, blockers, scenario membership and realization audit JSON.

The exact ordered set is `REGISTRY_COLUMNS` (**53 columns**) and is regression-tested.

## Excel presentation contract

- Rows 1–2: title/context.
- Row 3: section bands.
- Row 4: filterable column headers.
- Freeze title/header rows and key identity columns.
- Excel tables on detailed sheets.
- Numeric cost/impact format: `#,##0.00;[Red](#,##0.00);-` (accounting-style negative parentheses).
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
