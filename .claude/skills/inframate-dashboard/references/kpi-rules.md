# KPI rules

Every headline figure, where it comes from and the rule that combines it. Sums are per currency; nothing is summed
across currencies. All money figures are annual: the trailing 365 days for actual spend, 365-day forward for
savings, the measured run-rate for realized savings. The workbook rules are those of `project_executive` in
`src/publisher/contracts/workbook_contract.py`; `--verify` checks the dashboard against each run's workbook.

| Figure | Source (per package or RPKG) | Rule |
|---|---|---|
| Actual spend total / DBU / AWS | `cost_baseline.actual_365d.{total_cost,dbu_cost,aws_cost}` | Sum when every warehouse has the figure, else unavailable (workbook "Actual trailing 365d"). The known subset ("K of N complete") is shown beside it. DBU is observed billing at list price; AWS is modeled from billed DBU for Pro/Classic (0 for Serverless). |
| Under one year of billing | replay `evidence.json` `daily_cost_365[].usage_date` | Count of warehouses whose first billed day is less than 365 days before the analysis time; unknown without `evidence.json`. A badge, never a scaling factor. |
| Forward run-rate | `cost_baseline.forward_365d.total_cost` | Same rule as the actual (workbook "Forward 365d baseline"). |
| Identified savings low / base / high | recommended scenario `portfolio_economics.savings_365d.total` | Sum over recommended scenarios that are not BLOCKED or INSUFFICIENT_EVIDENCE and are priced; unpriced plans are excluded, not zero (workbook "Plan savings"). Net: capacity adds count negative. |
| Gross savings / cost increase / net | `package_economics.estimated_savings_365d.base` | Positive bases summed, negative bases summed as an increase, net = difference (workbook "Recommended annual ..."). |
| DBU · AWS (base) | recommended scenario `savings_365d.{dbu,aws}.base` | Sum over the same priced plans. |
| Migration upside | warehouse-type migration scenarios (not recommended, not blocked, not incompatible) `savings_365d.total.high` | Highest per warehouse, positive part, summed (workbook "Migration experiment high"). An experiment, not part of the plan. |
| Realized savings (run-rate) | RPKG `economics.annualized_realized_savings` | Sum over lineages whose `attribution.attribution_claim_status` is CLAIMABLE. The Realizer gives a superseded or overlapping lineage a non-claimable status, so a change is never claimed twice. |
| Realized to date | RPKG `economics.realized_savings_since_application` | Same lineages. |
| Observed, not claimable | RPKG `attribution.validated_observed_realization` | Lineages with OBSERVABLE_ONLY. |
| Applied, validating | RPKG `current_state.realization_state` | APPLIED or VALIDATING. |
| Open recommendations | recommended plan `execution_plan[]` CANDIDATE steps | Candidate status ACTIONABLE, TEST_REQUIRED or REVIEW and not observed applied (a lineage of the warehouse with application APPLIED on the step's knob at the step's value). BLOCKED counted separately. Value = net base step savings. |
| Aging | open/blocked rows across runs | Key = (warehouse, technique, target value). Age = days from the first run of the unbroken streak to this run; the streak breaks when a run lacks it or it is applied. Buckets 0-30, 31-60, 61-90, 90+. |
| Warehouses analysed | packages in the run; `manifest.excluded_warehouses` | Count, workspaces, excluded (unsupported type), priced plans. |

## Realization view

| Figure | Source | Rule |
|---|---|---|
| Realized run-rate, realized to date | as above | Claimable lineages of the visible warehouses. |
| Realization rate | `economics.annualized_realized_savings` / `economics.expected_range.base` | Over claimable lineages that have both; a ratio of sums, not an average of ratios. |
| Applied changes | lineages with an applied state | One per distinct change (warehouse, knob, applied value): when a later package supersedes an earlier one, both lineages observe the same change, so the claimable (else active, else newest) lineage stands for it. The lineage count is shown beside it. |
| Against the expected range | claimable lineages with a run-rate | Below `expected_range.low`, within, or above `expected_range.high`. |

## As-of join

For Publisher run *i*, the Realizer data shown is, per realization lineage, the latest RPKG snapshot whose
`as_of_utc` is before run *i+1*'s `generated_at` (the newest run takes the latest snapshot). Every lineage of the
warehouse counts, not only the run's own package: realized savings come from earlier packages that were applied.
Ledger entries for the timeline are filtered on `recorded_at_utc` the same way. Lineages that a later package
replaced before anything was applied are counted, not shown.

## Filters

Unfiltered, the tiles show the generator's figures (the ones reconciled with the workbook). With a filter, the page
sums the same per-warehouse fields over the visible warehouses with the same rules; envelope figures, which need
every package, are then not shown.
