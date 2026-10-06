# Reporter / Workbook Compatibility Contract v1.0.0

**Status:** LOCKED FOR PHASE-1 IMPLEMENTATION  
**Canonical inputs:** Reporter JSON Contract v1.0.0 (`PKG-*`) + Realizer Contract v1.0.0 (`RPKG-*`)  
**Existing Workbook Contract:** v1.1.0 remains immutable

## 1. Purpose

Define how richer Realizer truth is projected into legacy Reporter realization structures and human workbook views without mutating immutable recommendation packages or silently changing the locked Reporter/Workbook contracts.

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

## 10. Workbook policy

Workbook Contract v1.1.0 is preserved exactly for legacy package-only projection.

Realizer-aware workbook rendering is a **separately versioned extension**, not a silent edit to v1.1.0. Phase-1 implementation introduces `Workbook Realizer Extension v1.0.0`, consuming PKG + RPKG.

The extension may add Realizer fields/views but MUST retain all v1.1.0 columns/grains unchanged for existing views.

## 11. Realizer-aware human fields

At minimum the Realizer extension exposes:

```text
realization_package_id
snapshot_id
source_package_disposition
plan_progress
implementation_conformance
recommended_value
actual_applied_value
application_effective_from
application_effective_to
intent_state
origin_attribution
validation_state
maturity_state
realization_state
expected_same_scope_annual_savings
expected_denominator_source
realized_savings_to_date
annualized_realized_savings
mature_ttm_realized_savings
realization_ratio
observed_economic_delta
successful_realized_savings
recommendation_attributed_realized_savings
aws_economic_realized
aws_cash_realized
commitment_freed
attribution_claim_status
drift_state
regeneration_status
replacement_package_id
blockers
warnings
```

## 12. Four existing workbook views

Existing v1.1.0 views remain intact:

- Executive Summary;
- Warehouse Portfolio;
- Scenario Plans;
- Recommendation Registry.

The Realizer extension enriches those views only through a separately versioned projection and MAY add a `Realization` detail view in that extension. The v1.1.0 renderer remains available unchanged for backward compatibility.

## 13. Executive summary

Executive realization metrics MUST use economically exclusive authoritative lineages and expose at least:

```text
recommended annual opportunity
applied expected scope
validated observed realization
confirmed recommendation-attributed realization
under-realized value
validation-failed observed delta
rolled-back count/value
mature TTM realized value
attribution coverage
```

Standalone candidate potential is never added to scenario/plan realized value.

## 14. Workbook no-recalculation invariant

The workbook MAY format, aggregate economically exclusive canonical values, sort and filter. It MUST NOT:

- run application matching;
- calculate M08 counterfactuals;
- price realized cost;
- select denominators;
- classify realization;
- infer attribution;
- resolve overlap;
- regenerate recommendations.

## 15. Package-chain presentation

Workbook/UI MAY derive a warehouse optimization journey by traversing predecessor/replacement PKG/RPKG links. It MUST NOT merge the immutable RPKG histories into one canonical object.

## 16. Compatibility result

Adapters return an explicit status:

`LOSSLESS | LOSSY | UNREPRESENTABLE`

with machine-readable reasons. Canonical RPKG is always authoritative when compatibility is lossy/unrepresentable.
