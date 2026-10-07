# Reporter / Workbook Compatibility Contract

> The Realizer specification, kept in line with the code. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED FOR PHASE-1 IMPLEMENTATION  
**Canonical inputs:** Reporter JSON Contract 3.1.0 (`PKG-*`) + Realizer Contract v1.2.0 (`RPKG-*`)  
**Existing Workbook Contract:** Publisher Workbook Contract 1.6.0 remains immutable  
**Realization workbook:** Realization Workbook Contract v2.0.0 (sections 10 to 16; `REALIZATION_WORKBOOK_CONTRACT_VERSION` in `src/realizer/contracts/report.py`)

## 1. Purpose

Define how richer Realizer truth is projected into legacy Reporter realization structures and into the Realizer's own realization workbook, without mutating immutable recommendation packages or changing the Publisher's Reporter/Workbook contracts.

## 2. Authority

```text
PKG = recommendation authority
RPKG = realization authority
Legacy Reporter realization = lossy compatibility view only
Workbook = presentation only
```

No compatibility adapter may write realization facts back into the canonical PKG.

## 3. Canonical reporting input

New Realizer-aware reporting consumes:

```text
validated PKG
+
validated latest RSNAP/RPKG
```

joined by `recommendation_package_id` / `source_recommendation.recommendation_package_id`.

## 4. Legacy state mapping

| Realizer canonical state | Legacy Reporter status | Compatibility rule |
|---|---|---|
| NOT_APPLIED | NOT_APPLIED | lossless |
| APPLIED | APPLIED | lossless when legacy record can be represented |
| VALIDATING | VALIDATING | lossless at coarse state level |
| REALIZED | REALIZED | lossless at coarse state level |
| UNDER_REALIZED | REALIZED | **lossy**; outcome=`PARTIAL`, learning code `REALIZER_UNDER_REALIZED` |
| VALIDATION_FAILED | FAILED_VALIDATION | lossless at coarse state level |
| ROLLED_BACK | ROLLED_BACK | lossless |
| MONITORING | REALIZED | lossy; represents last successful validated realization while canonical state remains MONITORING |
| DRIFTED | none | canonical RPKG required |
| INVALIDATED | none | canonical RPKG required |
| REGENERATING | none | canonical RPKG required |
| BLOCKED | none | canonical RPKG required |

A source package disposition of `SUPERSEDED` MUST NOT automatically map to legacy `SUPERSEDED` while an already-applied realization interval is still economically active.

## 5. Actor rule

The legacy Reporter contract requires non-empty `applied_by`. Realizer allows unknown actor. The compatibility adapter MUST NOT fabricate `SYSTEM`, `UNKNOWN`, a service principal, or a user identity.

If actor is unknown, the adapter either:

1. suppresses the incompatible legacy realization record and returns an explicit compatibility warning; or
2. emits only canonical RPKG/workbook realization fields outside the legacy record.

## 6. Evidence rule

Fresh post-change evidence uses `REVID-*`. Legacy Reporter validation `finding_refs` can reference only valid package `EVID-*` IDs. The adapter MUST NOT inject REVID identifiers into the immutable package evidence registry.

Consequently, legacy validation records are intentionally lossy. Canonical RPKG retains full post-change evidence references.

## 7. Scope mapping

- Candidate-scoped realization may populate `candidate_id` only when attribution is defensibly isolated.
- Scenario/plan realization uses `candidate_id=null` and preserves scenario reference when representable.
- No fabricated candidate allocation is permitted.

## 8. Modified implementation

Legacy Reporter has no first-class `MODIFIED` conformance field. When a legacy record is otherwise representable:

- `applied.applied_config` carries the actual applied config, never the recommended target;
- outcome/learning codes include `REALIZER_MODIFIED_IMPLEMENTATION`;
- original Publisher recommendation remains unchanged in PKG;
- canonical RPKG remains the source for recommended-vs-actual comparison and denominator provenance.

## 9. Economics mapping

Legacy fields may receive only the matching canonical basis:

- observed DBU cost -> `RealizationObservedCost.dbu_cost`;
- observed attributable AWS cost -> `aws_cost` only when the semantic basis matches;
- realized DBU/AWS/total -> realized-savings fields;
- annualized -> `annualized_total_savings` only when maturity is REPRESENTATIVE or higher.

Modeled AWS economic realization MUST NOT be mislabeled as hard-cash AWS realization.

## 10. Realization workbook

A person sees realization in the **realization workbook**, a separate `.xlsx` the Realizer writes, never in the Publisher's workbook. The Publisher's workbook is unchanged: its realization columns show the package as generated (an immutable package never learns that it was applied). The Realizer's fleet run writes the realization workbook into each run folder, as `realization_workbook.xlsx`, by calling `write_realization_workbook(path, warehouse_runs)` (`src/realizer/reporter/workbook.py`) with one `WarehouseRun` per warehouse of the run: its status (`OK`, `REFUSED`, `CRASHED`) and reason, the realization packages it projected, their ledger entries, the warehouse's state index and, per package, the first step of its plan a person applies (the first authority-eligible step of the recommended plan: number, knob, current and recommended value).

Realization Workbook Contract v2.0.0 replaces the Realizer Workbook Extension v1.0.0 (two sheets, Realization Summary and Realization Timeline, rendered only by the single-file release). The summary and timeline row projections of v1.0.0 remain, unchanged, as the lineage projection (`project_summary`, `project_timeline`, `project_realizer_workbook`), which the stored scenario baseline pins and the Lineages and Timeline sheets reuse.

`src/realizer/contracts/report.py` is the only home of the sheet names, the column order, headers and groups (`WAREHOUSE_COLUMNS`, `LINEAGE_COLUMNS`, `TIMELINE_COLUMNS` and their `*_GROUPS`), the lineage selection and the next-action table; the renderer consumes them and keeps no second list.

## 11. Sheets and grains

Exactly three visible sheets, in this order:

| Sheet | Grain | Order |
|---|---|---|
| Warehouses | one row per `WarehouseRun`, a failed warehouse included | workspace, then warehouse |
| Lineages | one row per realization package of every `WarehouseRun` | the warehouses' order, then oldest package first (generation time, then package id), as the state index orders lineages |
| Timeline | one row per ledger entry of every lineage | the lineages' order, then ledger order (recorded time, then append order) |

### Which lineage a Warehouses row shows

1. The lineage that claims the warehouse's latest applied change: from the state index's `applied_knob_owners` when the run gives one (the owner whose change took effect last), else the `CLAIMABLE` lineage whose application took effect last. A lineage whose change was rolled back still claims the interval it was in force, so it can be shown. `Lineage shown` is `CLAIMS_LATEST_CHANGE`.
2. Else the newest package that is not superseded (latest generation time). `Lineage shown` is `NEWEST_ACTIVE_PACKAGE`.
3. Else none: the row carries the run's identity, status, reason and package id, and every lineage cell is empty.

Ties go to the highest package id, so the choice never depends on input order. The owners, claim statuses and dispositions are the Realizer's ([`ledger-contract.md`](ledger-contract.md) sections 18, 19 and 24); the workbook only reads them.

## 12. Columns

**Warehouses** (`WAREHOUSE_COLUMNS`, groups in brackets):

| Column | Value |
|---|---|
| [Identity] Workspace ID, Warehouse ID, Warehouse | the run's warehouse; the name from the lineage shown |
| Package ID, Realization package ID | the lineage shown; with none, the run's package id and an empty RPKG id |
| Lineage shown | `CLAIMS_LATEST_CHANGE` or `NEWEST_ACTIVE_PACKAGE` (section 11), empty with none |
| [Run] Run status, Run reason | the `WarehouseRun`'s |
| [Applied change] Applied knob, From, Applied, Recommended, Applied at, Applied until | `application.knob`, `source_value`, `observed_applied_value`, `recommended_value`, `effective_from_utc`, `effective_to_utc` |
| [State] Conformance, Validation, Realization, Maturity | `application.implementation_conformance`, `validation.state`, `current_state.realization_state`, `maturity.state` |
| [Realized economics] Currency | `economics.currency` |
| Realized to date, Days covered, First day, Last day | `economics.realized_savings_since_application` and `economics.realized_to_date` ([`ledger-contract.md`](ledger-contract.md) section 15) |
| Annualized realized, Expected annual (same scope), Realization ratio | `economics.annualized_realized_savings`, `expected_same_scope_annual_savings`, `realization_ratio` |
| [Attribution] Claim | `attribution.attribution_claim_status` |
| Claimed by package | the owner's package id when another lineage claims this lineage's change (`attribution.claimed_by`), empty otherwise |
| Other lineages | the warehouse's other lineages: the state index's lineages when given, else the run's realization packages, less the one shown |
| [Action] Next action | section 13 |
| Last observed (UTC) | the lineage's `as_of_utc`, else the state index's |
| [Audit] Application JSON, Economics JSON, Attribution JSON, Applied knob owners JSON | the lineage's sections and the state index's `applied_knob_owners` |

**Lineages** (`LINEAGE_COLUMNS`): the lineage projection's summary columns (identity and disposition; conformance, scope, intent, recommended and applied value, application state, plan progress; validation, realization and maturity states; origin and claim; observed and attributed realization, annualized, mature TTM, expected same-scope savings, ratio, economic basis, denominator source; regeneration status and replacement; as of) plus Claimed by package, Currency, Realized to date, Days covered and Next action; the Application, Validation, Economics and Attribution JSON at the far right.

**Timeline** (`TIMELINE_COLUMNS`): the lineage projection's timeline columns: ledger entry, realization package and package ids; event type, effective and recorded time; workspace and warehouse; application episode, validation episode, measurement and corrected entry ids; the evidence references and the payload as JSON at the far right.

## 13. Next action

One fixed label per state (`NEXT_ACTIONS`). The state is the lineage's realization state; the regeneration status or the plan progress refines it where it changes what a person does. A Warehouses row whose run failed shows the run's label whatever its lineage; one whose run projected no lineage shows `None: no realization to show`. A realization state the table does not name fails the projection (closed, never a guess).

| State | Condition | Next action |
|---|---|---|
| run `REFUSED` | | Fix the run: REFUSED |
| run `CRASHED` | | Fix the run: CRASHED |
| no lineage | | None: no realization to show |
| `NOT_APPLIED` | the package's first plan step is given | Apply step *n*: *knob* *current* → *recommended* (for example "Apply step 1: auto_stop_minutes 30 → 10") |
| `NOT_APPLIED` | | Apply the recommended change |
| `NOT_APPLIED` | the plan has no authority-eligible step | None: no step to apply |
| `APPLIED`, `VALIDATING` | | Wait for validation |
| `APPLIED` | validation passed, economics unavailable (a knob without a counterfactual, or one the evidence could not establish: the measurement's not-measured reason) | Keep monitoring: savings not measured for this change yet |
| `MONITORING` | a warehouse-type migration's observed delta (measurement method `MIGRATION_BILLED_OBSERVED`) | Keep monitoring: migration observed, savings not modeled |
| `REALIZED`, `MONITORING`, `PERFORMANCE_VALIDATED`, `CAPACITY_VALIDATED` | | Keep monitoring |
| `SAFETY_ACTION_APPLIED` | | Keep monitoring: safety rollback in force |
| `UNDER_REALIZED` | | Review: re-optimization recommended |
| `VALIDATION_FAILED` | | Roll back: a guard failed |
| `ROLLED_BACK` | | None: rolled back |
| `ROLLED_BACK` | regeneration `REQUESTED` | Run the Publisher: regeneration requested |
| `DRIFTED`, `REGENERATING` | | Run the Publisher: regeneration requested |
| `ROLLED_BACK`, `DRIFTED`, `REGENERATING` | regeneration `REPLACED` | None: replacement package linked |
| `BLOCKED`, `INVALIDATED` | | Review the blocker |

The realization package does not carry the steps of a plan not yet applied (its application section is empty until an application is recorded), so a not-applied lineage names no step; the Publisher's workbook lists them.

## 14. Presentation rules

The Publisher workbook's rules, implemented by the Realizer's own renderer (the products share no workbook code):

- exactly the three visible sheets; no formulas, macros, hidden sheets, defined names or conditional formats;
- row 1 holds the column groups, row 2 the headers (frozen, with the leading identity columns), one row per record from row 3, each sheet an Excel table;
- identifiers, times and days are written as text (a workspace id never becomes a number);
- money is a signed number (positive = savings, negative = cost increase), next to an explicit Currency column; ratios, days covered and counts are numbers;
- an unknown value is an empty cell, never zero;
- audit JSON is at the far right; a cell longer than Excel allows is cut with a visible marker (the full content is in the realization package and ledger);
- the document's created and modified times are the latest time the inputs were observed, so identical inputs give identical content.

## 15. No-recalculation invariant

The workbook MAY format, sort and filter, and the projection MAY choose which lineage a Warehouses row shows by section 11 and a label by section 13. It MUST NOT:

- run application matching;
- calculate M08 counterfactuals;
- price realized cost;
- select denominators;
- classify realization;
- infer attribution;
- resolve overlap;
- regenerate recommendations;
- aggregate realization across lineages (one change can be realized by several lineages; only the owner claims it).

## 16. Package-chain presentation

Workbook/UI MAY derive a warehouse optimization journey by traversing predecessor/replacement PKG/RPKG links. It MUST NOT merge the immutable RPKG histories into one canonical object.

## 17. Compatibility result

Adapters return an explicit status:

`LOSSLESS | LOSSY | UNREPRESENTABLE`

with machine-readable reasons. Canonical RPKG is always authoritative when compatibility is lossy/unrepresentable.
