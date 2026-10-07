# Realizer JSON + Realization Ledger Contract v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Product:** Databricks SQL Warehouse Optimizer — Realizer  
**Publisher release:** v2.8.0

## 1. Purpose

Define the canonical post-change machine contract and append-only event ledger used to represent application, validation, realized economics, attribution, drift, corrections, rollback and recommendation regeneration without mutating an immutable Publisher `PKG-*`.

## 2. Core identities

- `PKG-*` — immutable Recommendation Package from Publisher.
- `RPKG-*` — stable realization lineage bound 1:1 to exactly one PKG.
- `RLE-*` — immutable Realization Ledger Entry.
- `RSNAP-*` — deterministic current snapshot projected from a ledger checkpoint.
- `APP-*` — application episode.
- `VAL-*` — validation episode.
- `MEAS-*` — realized-value measurement.
- `CF-*` — realization counterfactual.
- `REVID-*` — post-change evidence.
- `RERUN-*` — regeneration request.

Mental model:

```text
PKG   = immutable recommendation version
RPKG  = stable realization lineage for exactly one PKG
RLE   = immutable facts/events
RSNAP = derived current view of one RPKG
```

## 3. Cardinality and immutability

```text
1 PKG -> 1 RPKG
1 RPKG -> 0..N APP
1 APP -> 0..N VAL
1 APP -> 0..N MEAS
1 RPKG -> 0..N RLE
1 RPKG -> 0..N RSNAP
```

A new recommendation always creates a new PKG and therefore a new RPKG. Ledger additions create new snapshots but never change the RPKG identity. Publisher packages and prior ledger entries are immutable.

## 4. Top-level realization package

```json
{
  "contract_version": "1.0.0",
  "realization_package_id": "RPKG-...",
  "snapshot_id": "RSNAP-...",
  "source_recommendation": {},
  "warehouse": {},
  "as_of_utc": "...",
  "current_state": {},
  "application": {},
  "validation": {},
  "economics": {},
  "attribution": {},
  "maturity": {},
  "drift": {},
  "regeneration": {},
  "history": {},
  "provenance": {}
}
```

Unknown material facts are `null`, never synthetic zero.

`source_recommendation` identifies the immutable PKG (id, contract version, full hash, epoch, recommended scenario, generation time, disposition and the superseding PKG) and states the conditions it was produced under, copied from it: `run_scope` (the restricted scope of a scoped run), `cost_profile` (the name of the profile the warehouse was evaluated under) and `observed_change` (the configuration change that started the current epoch). Each is `null` when the package has none. The `REALIZATION_CREATED` entry carries the same three values with the PKG id. Plan progress is derived from the package's own plan, which a run scope has already cut to the in-scope steps (and a canary rollback kept outside it); a pending-reevaluation step of that plan still keeps the plan partially completed.

## 5. Orthogonal state dimensions

### Intent
`UNKNOWN | ACCEPTED | REJECTED | DEFERRED`

### Source package disposition
`ACTIVE | SUPERSEDED | EXPIRED | BLOCKED`

### Implementation conformance
`EXACT | MODIFIED | NOT_APPLIED | CONTRARY | UNRELATED`

### Validation
`NOT_READY | READY | RUNNING | PASSED | FAILED | INCONCLUSIVE`

### Realization
`NOT_APPLIED | APPLIED | VALIDATING | REALIZED | UNDER_REALIZED | VALIDATION_FAILED | MONITORING | ROLLED_BACK | DRIFTED | INVALIDATED | REGENERATING | BLOCKED`

### Maturity
`NOT_APPLIED | COLLECTING | PROVISIONAL | REPRESENTATIVE | MATURE`

### Origin attribution
`CONFIRMED | INFERRED | UNKNOWN | INDEPENDENT | NOT_ATTRIBUTABLE`

### Attribution state
`HIGH_CONFIDENCE | PARTIAL_ATTRIBUTION | CORRELATED_ONLY | NOT_ATTRIBUTABLE`

### Attribution claim status
`CLAIMABLE | OBSERVABLE_ONLY | NOT_ATTRIBUTABLE`

These dimensions MUST remain independent. A package can be `SUPERSEDED` while an already-applied state remains `MONITORING` until a physical/configuration boundary closes the interval.

## 6. Application episode

Each application episode records the exact observed effective state and relation to Publisher intent. Required concepts include:

```text
APP id
PKG/RPKG/scenario/step/candidate references
source value
recommended value
observed applied value
implementation_conformance
effective_from/effective_to
detection source/evidence
source/recommended/observed config hashes
origin attribution
actor when known
```

`PARTIALLY_APPLIED` is not a normal v2.8.0 step state. Plan progress may be partial while a completed individual step is fully `EXACT` or `MODIFIED`.

## 7. Modified implementation

A modified implementation can realize value.

Example:

```text
Publisher: auto_stop 120 -> 30
Actual:    auto_stop 120 -> 60

implementation_conformance = MODIFIED
```

Realizer validates and measures the actual 60-minute state. The original 30-minute expected savings MUST NOT be reused as the realization denominator. Expected economics for the exact actual transition are re-estimated.

## 8. Plan progress

Aggregate plan progress is separate from step conformance:

`NOT_STARTED | PARTIALLY_COMPLETED | COMPLETED | STOPPED`

A partially completed Plan may contain fully realized completed steps. `PENDING_REEVALUATION` downstream steps are never credited from the old package.

## 9. Ledger envelope

```json
{
  "ledger_entry_id": "RLE-...",
  "ledger_contract_version": "1.0.0",
  "realization_package_id": "RPKG-...",
  "recommendation_package_id": "PKG-...",
  "event_type": "APPLICATION_OBSERVED",
  "effective_at_utc": "...",
  "recorded_at_utc": "...",
  "workspace_id": "...",
  "warehouse_id": "...",
  "application_episode_id": "APP-...",
  "validation_episode_id": null,
  "measurement_id": null,
  "payload": {},
  "evidence_refs": ["REVID-..."],
  "caused_by_entry_id": null,
  "corrects_entry_id": null,
  "provenance": {}
}
```

## 10. Event types

```text
REALIZATION_CREATED
INTENT_RECORDED
APPLICATION_OBSERVED
MODIFIED_APPLICATION_OBSERVED
INDEPENDENT_CHANGE_OBSERVED
VALIDATION_READY
VALIDATION_STARTED
VALIDATION_COMPLETED
ECONOMIC_MEASUREMENT_RECORDED
COUNTERFACTUAL_RECORDED
DRIFT_DETECTED
INVALIDATION_RECORDED
ROLLBACK_REQUESTED
ROLLBACK_OBSERVED
SOURCE_PACKAGE_SUPERSEDED
SOURCE_PACKAGE_EXPIRED
BILLING_CORRECTION_RECORDED
FINANCIAL_BASIS_CHANGED
SOURCE_QUALITY_CHANGED
REGENERATION_REQUESTED
REPLACEMENT_PACKAGE_LINKED
```

## 11. Deterministic IDs

Full semantic hash uses SHA-256. Display IDs use the first 96 bits / 24 uppercase hex characters. Canonical hash payloads exclude filenames, log metadata and wall-clock projection time.

```text
RPKG = SHA256("REALIZATION_PACKAGE", contract_major, PKG)
RSNAP = SHA256(RPKG, ledger_checkpoint_hash, projector_version)
RLE = SHA256(RPKG, event_type, domain_business_key, canonical_payload)
```

## 12. Time semantics

Ledger is bitemporal:

- `effective_at_utc` — when the business fact became true.
- `recorded_at_utc` — when Realizer recorded it.

Application/economic ownership intervals use half-open `[start, end)` semantics.

`RSNAP.as_of_utc` is deterministic and derived from the ledger checkpoint, not projection wall clock.

## 13. Validation

Validation records readiness dimensions, post-change Analyzer references, guardrail decisions, Policy snapshot, and findings. Hard performance/reliability failure prevents successful realization even when monetary savings are positive.

## 14. Counterfactual

Old-state counterfactual is generated under actual post-change workload. Preferred evidence hierarchy:

1. paired canary/control;
2. calibrated prior state;
3. matched normalized historical;
4. controlled pre/post;
5. correlated-only diagnostics.

No defensible counterfactual means no claimable realized savings.

## 15. Realized economics

```text
CounterfactualOldCost = Price(Modeler(old config, actual post-change workload))
ObservedNewCost = corrected Databricks + attributable AWS cost
RealizedSavings = CounterfactualOldCost - ObservedNewCost
```

Distinct outputs:

- observed economic delta;
- successful realized savings;
- savings to date;
- annualized realized run-rate;
- mature TTM realized savings;
- DBU component;
- AWS economic component;
- AWS cash component;
- commitment freed.

Negative realization is preserved.

## 16. Same-scope denominator

`RealizationRatio = AnnualizedRealizedSavings / ExpectedAnnualSavingsForExactAppliedScope`

Allowed denominator sources:

`PUBLISHER_SCENARIO | PUBLISHER_CANDIDATE | REESTIMATED_APPLIED_SCOPE | UNAVAILABLE`

Modified applications always require `REESTIMATED_APPLIED_SCOPE`. Pending, contingent, invalidated or unapplied steps are forbidden denominator sources.

## 17. Attribution

Economic realization and recommendation attribution are separate. A valid observed economic improvement can exist when origin is `UNKNOWN` or `INFERRED`; strict recommendation-attributed KPIs require policy-qualified causal evidence.

When multiple changes are deployed together and contribution cannot be separated defensibly, Realizer reports scenario-level realization rather than fabricating candidate allocations.

## 18. Economic exclusivity

One source economic atom / economic basis / interval belongs to at most one authoritative realization lineage. Unresolved overlap blocks financial-grade portfolio publication.

## 19. Supersession and handoff

Generating a newer PKG does not transfer economic ownership. The old RPKG continues owning its valid applied interval until a new applied state, rollback, drift or invalidation boundary closes it. Replacement packages receive no retroactive credit.

## 20. Corrections

Billing/evidence corrections are append-only:

`ORIGINAL | RETRACTION | RESTATEMENT`

Corrections reference prior facts and cause re-projection; historical records are never overwritten.

## 21. Drift

`CONFIGURATION | WORKLOAD | REGIME | POLICY | FINANCIAL | SOURCE_QUALITY`

Historical realized value before a valid drift boundary remains valid.

## 22. Regeneration

Status:

`NOT_REQUIRED | REQUESTED | RUNNING | COMPLETED | FAILED | REPLACED`

Re-evaluation always starts from the actual observed current state. Recommendation truth changes only through a new immutable PKG emitted by Publisher.

## 23. Core invariants

1. Exact applied scope only in the realization denominator.
2. No economic double counting.
3. Publisher package immutable.
4. Intent/application independent.
5. Hard guardrails dominate savings.
6. Actual-state measurement for `MODIFIED`.
7. No pending-step economics.
8. No unsupported causal claim.
9. No counterfactual -> no claimable realization.
10. Corrections append, never overwrite.
11. Negative outcomes preserved.
12. Annualization requires representative evidence.
13. Same ledger + versions + policy -> identical snapshot.
14. Reporting is derived, not authoritative.
15. Regeneration creates a new PKG/RPKG.
16. One RPKG belongs to exactly one PKG.
17. Ledger additions create new RSNAP, not new RPKG.
18. Supersession alone does not stop existing applied measurement.
19. New package gets no retroactive credit.
20. Old `PENDING_REEVALUATION` steps cannot become authoritative without regeneration.
21. Intervals are half-open.
22. Snapshot time derives from ledger.
23. Unknown material economic data is null, not zero.
24. Observed and recommendation-attributed savings are distinct.
