# Realizer JSON Contract v1.1.0 + Realization Ledger Contract v1.0.0

> The Realizer specification (realization package contract v1.1.0, ledger envelope v1.0.0; v1.1.0 added the step role, the `SAFETY_ACTION_APPLIED`, `PERFORMANCE_VALIDATED` and `CAPACITY_VALIDATED` states, the application's compatibility status and annotations, and the source package's run scope, cost profile and observed change; RPKG ids hash only the major version). The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

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
1 warehouse -> 0..N RPKG (one per package realized into the output directory)
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

`source_recommendation` identifies the immutable PKG (id, contract version, full hash, epoch, recommended scenario, generation time, disposition, the package that replaced it, `superseded_by_package_id`, and the first package that followed its epoch, `successor_package_id`, section 19) and states the conditions it was produced under, copied from it: `run_scope` (the restricted scope of a scoped run), `cost_profile` (the name of the profile the warehouse was evaluated under) and `observed_change` (the configuration change that started the current epoch). Each is `null` when the package has none. The `REALIZATION_CREATED` entry carries the same three values with the PKG id. Plan progress is derived from the package's own plan, which a run scope has already cut to the in-scope steps (and a canary rollback kept outside it); a pending-reevaluation step of that plan still keeps the plan partially completed.

When another lineage of the warehouse owns the change the application realized (section 18), `attribution` carries the hard disqualifier `CLAIMED_BY_OTHER_LINEAGE` and `claimed_by`, one `{knob, realization_package_id, recommendation_package_id}` per owned knob; the key is absent otherwise.

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
`NOT_APPLIED | APPLIED | VALIDATING | REALIZED | UNDER_REALIZED | VALIDATION_FAILED | MONITORING | ROLLED_BACK | DRIFTED | INVALIDATED | REGENERATING | BLOCKED | SAFETY_ACTION_APPLIED | PERFORMANCE_VALIDATED | CAPACITY_VALIDATED`

### Step role
`SAVINGS | PERFORMANCE | CAPACITY | SAFETY_ROLLBACK`

What the applied plan step is for, from its candidate (recorded on the application as `step_role`, null for a scenario-scope application and an independent change): `SAFETY_ROLLBACK` when the candidate carries the Publisher's canary-rollback blocker `CANARY_GUARDRAIL_BREACHED`; `PERFORMANCE` when its objectives are a performance fix priced as a cost increase or not priced; `CAPACITY` when it is priced at exactly zero; `SAVINGS` otherwise. A safety rollback's application is recorded and validated but never measured or claimed: its realization state is `SAFETY_ACTION_APPLIED`.

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

The scenario an observation applied is inferred: the one whose authority-eligible steps the observed configuration applies most (exactly or modified), the recommended plan first on a tie and when none is applied; an `applied_scenario_id` in the observation overrides it. So a migration applied under a migration-only run scope (recommended "No change") is still an application of the package's migration scenario. The application records the scenario's `compatibility_status` and `annotations`: a warehouse-type migration whose compatibility the Publisher did not evidence (any status but `COMPATIBLE`) carries `COMPATIBILITY_NOT_EVIDENCED`.

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
APPLICATION_CLAIMED_BY_OTHER_LINEAGE
SUCCESSOR_PACKAGE_LINKED
POLICY_DIFF_RECORDED
```

The last three, and the `SOURCE_PACKAGE_SUPERSEDED`, `REPLACEMENT_PACKAGE_LINKED` and `ROLLBACK_OBSERVED` entries a run decides from another lineage, are described in sections 18, 19 and 24; `POLICY_DIFF_RECORDED` in the [policy contract](policy-contract.md) section 4.

## 11. Deterministic IDs

Full semantic hash uses SHA-256. Display IDs use the first 96 bits / 24 uppercase hex characters. Canonical hash payloads exclude filenames, log metadata and wall-clock projection time.

```text
RPKG = SHA256("REALIZATION_PACKAGE", contract_major, PKG)
RSNAP = SHA256(RPKG, ledger_checkpoint_hash, projector_version)
RLE = SHA256(RPKG, event_type, domain_business_key, canonical_payload)
CF = SHA256(APP, old_config_hash, method, window, replay_target, assumptions, evidence_fingerprint, modeler_version, policy_snapshot_id)
```

The counterfactual's inputs are defined in the [source and evidence contract](source-evidence-contract.md) section 9.

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

Which Publisher figure is the denominator:

- a candidate-scope step applied exactly: the candidate's isolated economics (`PUBLISHER_CANDIDATE`);
- a scenario-scope application of one step: that step's candidate economics; of several steps that are the plan's first authority-eligible steps (a prefix): the running savings of the last applied step (`PUBLISHER_SCENARIO`), never the whole scenario, whose later steps were not applied; of steps that are not a prefix: none (`UNAVAILABLE`);
- a warehouse-type migration: the scenario's range, which is pre-canary, so it is informational (`classification_basis` `INFORMATIONAL`): the realization and its ratio are reported, the state stays `MONITORING` (`MIGRATION_RANGE_INFORMATIONAL`), never `REALIZED` or `UNDER_REALIZED` against it;
- any denominator when the measured query volume moved outside the comparable band after the change: reported, informational (`WORKLOAD_SHIFT`, [`policy-contract.md`](policy-contract.md) section 7).

The component compared is the measurement's cost basis (DBU only, or DBU and modeled AWS); a caller-supplied observation that gives a DBU component and no AWS component is compared with the DBU range (its costs are DBU only), any other caller-supplied observation with the total, and a package without a total is `UNAVAILABLE`, never the DBU range in its place. The expected savings per component (DBU, AWS, total) of the chosen figure are reported separately as `expected_components`.

## 17. Attribution

Economic realization and recommendation attribution are separate. A valid observed economic improvement can exist when origin is `UNKNOWN` or `INFERRED`; strict recommendation-attributed KPIs require policy-qualified causal evidence.

When multiple changes are deployed together and contribution cannot be separated defensibly, Realizer reports scenario-level realization rather than fabricating candidate allocations.

## 18. Economic exclusivity

One source economic atom / economic basis / interval belongs to at most one authoritative realization lineage. Unresolved overlap blocks financial-grade portfolio publication.

Several packages of one warehouse can be realized into one output directory (later Publisher runs of the same epoch, cost-profile or scope alternatives), and one configuration change can realize a step of each. A change is a knob set to a value over an interval (from the application's effective time to the drift or rollback that ends it); two applications are of the same change when they set the same knob to the same value over intersecting intervals. Of the lineages whose authoritative application (`EXACT` or `MODIFIED`) realizes the change, the owner is the one whose package was generated most recently at or before the change; at equal generation time `EXACT` before `MODIFIED`, then the lowest package id. A package generated after the change already started from the applied value and never owns it. Every other such lineage records `APPLICATION_CLAIMED_BY_OTHER_LINEAGE` on its application episode (payload: the knob, value and effective time, the owner's RPKG, PKG, generation time, conformance and application episode, and the rule `MOST_RECENT_PACKAGE_BEFORE_CHANGE`); its realization is still measured and reported but is observable only: hard disqualifier `CLAIMED_BY_OTHER_LINEAGE`, `claimed_by` naming the owner, nothing attributed (section 4). A scenario-scope application is a claim on each knob of its applied steps. The decision is the same whichever package is realized first: a lineage that claimed a change alone yields when the owner's application is recorded later, and when several entries name owners the projection keeps the best-ranked one.

Example: an aggressive-profile package (auto-stop 365 to 15) and a balanced-profile package (365 to 210) of one epoch, both generated 2026-10-01 12:00; the owner sets 210 on 2026-10-08 14:32 and the replay measures 1,069.73 over 28 days (13,944.69 a year). Alone, each lineage reports 1,069.73 claimable; together the balanced lineage (`EXACT`) claims 1,069.73 and the aggressive lineage (`MODIFIED`) is observable only, naming it, in either run order.

The caller's `overlap_count` remains an extra restriction (`PORTFOLIO_SCOPE_OVERLAP`); it never removes one.

## 19. Supersession and handoff

Generating a newer PKG does not transfer economic ownership. The old RPKG continues owning its valid applied interval until a new applied state, rollback, drift or invalidation boundary closes it. Replacement packages receive no retroactive credit.

Across the lineages of a warehouse in one output directory, each run records:

- **Same epoch, newer package.** A lineage is superseded (`SOURCE_PACKAGE_SUPERSEDED` and `REPLACEMENT_PACKAGE_LINKED`, reason `NEWER_PACKAGE_SAME_EPOCH`) by the newest package of its epoch generated after it: the Publisher saw the same configuration again, so nothing was applied in between. Daily runs therefore leave one active package per epoch instead of an accumulating list of never-applied ones. Packages generated at the same time (profile alternatives) never replace each other.
- **Successor.** A package whose `observed_change.previous_epoch_id` is the lineage's epoch follows it: the earliest such package is linked as its successor (`SUCCESSOR_PACKAGE_LINKED`, `source_recommendation.successor_package_id`). When the configuration change that started the successor's epoch applied none of the lineage's authority-eligible steps, the lineage is superseded by the successor (reason `CONFIGURATION_CHANGED`). When it applied one, the lineage keeps realizing and is superseded only when the successor's expected savings (its recommended scenario's total base, else DBU base) are a material improvement over the lineage's, at least the policy's `minimum_material_savings_delta_pct` (5 percent) more (reason `MATERIAL_IMPROVEMENT`); an unpriced successor is never one.
- **Rollback by a later package.** When a later package's canary rollback step (step role `SAFETY_ROLLBACK`) is applied, returning a knob to the value it had before an earlier lineage's application of that knob, and the later package's observed change starts from the earlier lineage's epoch, the earlier lineage records `ROLLBACK_OBSERVED` (reason `CANARY_ROLLBACK`, naming the rollback package and its application) effective at the time the rollback was applied; its application state becomes `ROLLED_BACK` from then.

The supersession, succession and rollback events take their times from the facts they follow from (the newer package's generation time; the rollback's effective time and the later of the two applications' record times). Supersession never stops a valid realization: a superseded lineage that owns an applied change keeps measuring and claiming it. A caller can still name a replacement explicitly (`replacement_package_id`: a package id in the contract format, never the package itself); the explicit replacement is the one the snapshot reports when both exist.

Example: PKG-A (2026-10-01, auto-stop 365 to 210) is applied 2026-10-08; PKG-B (2026-10-15) has auto-stop 210 as its current value and recommends a size cut. PKG-A owns the auto-stop interval from 2026-10-08 and keeps claiming 1,069.73; PKG-B is linked as its successor; PKG-B's plan is unpriced, so PKG-A stays active; PKG-B can claim only its own size change.

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

A regeneration request (`RERUN-*`, recorded as `REGENERATION_REQUESTED` on the application episode and written as `realization/regeneration/RERUN-*.json`) carries the actual configuration and its hash, the reasons, the scenario and step, and:

- `evidence_id`: the observed-configuration evidence (`REVID-*`) of the run that made the request; the ledger entry references it in `evidence_refs`;
- `observation_window`: that observation's `start_utc` and `end_utc` (its window, else its observed time);
- `changed_knobs`: the decision knobs (type, size, min and max clusters, auto-stop, channel; never Photon) whose actual value differs from the package's current configuration, in contract order;
- `impacted_knobs`: the knobs the applied change invalidates downstream.

The request id does not depend on the evidence. The same request on a later run (the configuration unchanged) is a duplicate: the ledger keeps the first entry, and the RERUN file keeps the evidence and window of the run that first asked.

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

## 24. Lineages of one warehouse

A run reads, besides its own ledger, every other lineage of its warehouse in the output directory, and writes what it learns about each of them:

- `realization/sources/<workspace>__<warehouse>/<PKG>.json` is the copy of the source package a lineage was created from, written when the lineage is created (the Publisher's package is unchanged; the copy is how another run finds the lineage and re-projects its snapshot). A copy is checked like any package when it is read (schema, id and full hash): an edited copy, or a corrupt ledger of another lineage, fails the run before anything else is written. A lineage created before its copy existed is seen once its package is realized again.
- The cross-lineage events of sections 18 and 19 are decided by the Reconciler from all lineages together (the run's own including the applications it is about to record) and appended after the run's own events: to the run's own ledger, or to the other lineage's ledger under that ledger's advisory lock for the append (a ledger that stays locked keeps the event for a later run, which decides it again). An event already recorded is suppressed by its id. The other lineages whose ledgers grew are re-projected: their realization package and a new snapshot are written.
- `realization/policies/<PSNAP>.json` holds the settings of each policy a validation or a PolicyDiff was recorded under ([policy contract](policy-contract.md) section 4).
- `realization/state/<workspace>__<warehouse>.json` is the warehouse's state index, a view rebuilt from the ledgers on every run and never read back (never a second source of truth): `state_index_version` (1.0.0), the warehouse, `as_of_utc`, `active_package_ids` (the packages not superseded), `applied_knob_owners` (per applied knob, the lineage that claims it) and `lineages`, one row per lineage, oldest package first: its RPKG, PKG and snapshot ids, epoch and previous epoch, generation time, cost profile, disposition, replacement, successor, application state, applied knob, value and time, realization state, claim status, attributed savings, `claimed_by` and `as_of_utc`.

Realizing the same packages with the same observations in any order gives every lineage the same final decisions (owner, claim status, disposition, replacement, successor, rollback). When each lineage learns the same facts in either order, as for two packages of one generation time, the ledgers, realization packages and state index are identical files too. Otherwise a ledger can hold extra history of what was known in between (a package superseded first by the next day's package, then by the newest), so its snapshot id differs while its decisions do not; snapshots already written are history and stay.
