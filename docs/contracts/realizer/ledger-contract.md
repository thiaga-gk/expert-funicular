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

A rollback is read from the configuration, never from a flag: a later observation in which every knob an open application changed (the step's knob; for a scenario-scope application, every applied step's knob) is back at its source value is recorded as `ROLLBACK_OBSERVED`, with no `DRIFT_DETECTED` and no regeneration request, because those knobs are at the package's own configuration again. The entry is recorded on the latest episode it rolls back and lists every episode it rolls back (`rolled_back_episode_ids`); the other open episodes go on. A package's own safety-rollback step (a canary rollback) is an application of that package like any other step, so rolling it back means returning to the value it replaced. `ROLLBACK_REQUESTED` records the caller's intent and changes no state.

While episodes are open, a later observation is read against the configuration last recorded (the latest application, independent change or observed rollback); only the decision knobs that moved since are new:

- a later step of the same plan (authority-eligible, applied exactly or modified, held by no open episode) is plan progress: its own application episode on the same realization package, effective at the epoch boundary that brought it, while the earlier episodes stay open and go on being measured;
- a step the package holds for re-evaluation (or blocks) applied early is an independent change with no credit, and the run requests regeneration (`PENDING_STEP_APPLIED`); the open episodes go on;
- open episodes whose knobs are all back at their source value are rolled back (above);
- any other change of the configuration (a knob no step relates to, a contrary or unrelated value, or an open step's value changed again) is a drift: `DRIFT_DETECTED` on the latest open episode, which closes every open episode, and a `CONFIGURATION_DRIFT` regeneration request.

A move of the warehouse channel, which the configuration hash does not cover, is recorded as an independent change and is never a step, a rollback or a drift. An independent change is recorded once, when it moves, not on every later observation of the same configuration.

A drift or an observed rollback closes the episodes it applies to. A closed episode never absorbs a new application: the applied configuration observed again after it closed (or another value of a plan step) is a new application episode with its own APP id and its own effective time, measured from then on, while the same configuration observed on later days stays in the open episode it opened. The closing configuration observed again on a later run records nothing new (a drift still ends that run with its regeneration request, which is a duplicate). `DRIFT_DETECTED`, `ROLLBACK_OBSERVED` and `ROLLBACK_REQUESTED` carry the time they took effect in their payload (`effective_from_utc`), so a second drift or rollback, of the same configuration or not, is a new entry; a rollback request still pending on the episode (requested, no rollback observed since) is not recorded again.

The snapshot shows one episode: the open episode of the plan's earliest applied step (the one a live run measures; a scenario-scope application counts as the earliest), else the latest application. Its application state is `ROLLED_BACK` or `DRIFTED` when a rollback or a drift closed it, else `APPLIED`; its validation, measurements, realization state and `drift` section are that episode's own (an earlier episode's are history); `application.open_episodes` lists every open episode (id, step, knob, effective time).

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

A partially completed Plan may contain fully realized completed steps. `PENDING_REEVALUATION` downstream steps are never credited from the old package. The applied steps are those the open episodes hold while any is open (a step rolled back alone no longer counts), else those of every application recorded; a later step applied while earlier ones are open is progress of the same plan (section 6), never a drift.

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

One ledger file per realization package (`realization/ledger/<RPKG>.jsonl`), one entry per newline-terminated line, appended and never rewritten.

A realize run holds an advisory lock on its package's ledger for the whole run: from before the lineage entry is recorded until the realization package and the snapshot are written. The lock is an exclusive file lock on `<RPKG>.jsonl.lock` next to the ledger, present only while a run holds it. A second run on the same ledger waits a short, bounded time and is then refused (`LedgerError`, "locked by another run") without writing anything; a run that fails releases the lock.

A write cut short can leave one unterminated last line. Holding the lock, the run repairs it before anything else: a last line that is a valid entry gets its newline; one that is not is appended to `<RPKG>.jsonl.quarantine` next to the ledger, the ledger is truncated to its last newline, `ledger.quarantined` is logged (with the byte count, never the content) and the run continues. A malformed line that is newline-terminated, or anywhere but last, is never repaired: the run fails closed and the file is left as it is. A read without the lock (a replay or a lineage view) ignores such an unterminated last line when it is not a valid entry and never changes the file; an append without the lock refuses a ledger that ends with one.

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
CF = SHA256(APP, old_config_hash, method, window, replay_target, assumptions, evidence_fingerprint, modeler_version, policy_snapshot_id)
```

The counterfactual's inputs are defined in the [source and evidence contract](source-evidence-contract.md) section 9.

## 12. Time semantics

Ledger is bitemporal:

- `effective_at_utc` — when the business fact became true.
- `recorded_at_utc` — when Realizer recorded it.

Application/economic ownership intervals use half-open `[start, end)` semantics.

An application episode starts at the observation's applied time (else its observed time). A drift or an observed rollback takes effect at the boundary of the configuration epoch that brought it, stated in its payload (`effective_from_utc`): the observation's measured `observed_epoch_start` when it has a measurement, else its applied time when that is after the episode's start (an applied time carried over from the application itself is not), else its observed time. That time must be strictly after the start of the episode it closes; a history in which it is not (a measured epoch that began before the application it would close, an observation no later than it) is refused with a one-line message before any lifecycle event of the observation is recorded. The snapshot's `application.effective_to_utc` is the effective time of the first drift or rollback that closed the active episode (a rollback after a drift ends nothing more).

The ledger file is append-only, one entry per line. Its projection order is the recorded time, then the append order (the line sequence of the file): at equal recorded time the entry appended last is the latest, so a window measured again shows its newer value. The entry id never breaks a tie, and the checkpoint hash covers the entry ids in this order.

Times are compared and ordered as instants, never as text: a time written with fractional seconds (`...T12:00:00.500000Z`) is later than the whole second before it (`...T12:00:00Z`), although its text sorts first. This holds for the ledger order, the replay cut, the checkpoint's latest times, the configuration-drift check and the snapshot's rollback and drift precedence.

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

Savings to date (`realized_savings_since_application`) are the realized savings of the episode the snapshot shows over every instant its measurements cover: daily runs measure overlapping windows (the latest whole weeks after the change, up to twelve), so each instant takes the value of the latest measurement whose window covers it, pro rata over that window's length, and the savings to date are the signed sum. A window measured again (a restatement, section 20) replaces the earlier value of its days. `economics.realized_to_date` states the coverage: the first and last covered UTC day, the days covered and the whole days between them no measurement covers (runs more than twelve weeks apart, or the last days before a rollback). The annualized run-rate, the ratio and the classification stay those of the latest measurement's window. The episode is `MATURE` when its measurements cover 365 days; `mature_ttm_realized_savings` is then the sum over the latest 365 covered days, and it is null before. Neither figure is taken from the observation. A claimable attribution claims the savings to date (`recommendation_attributed_realized_savings`).

An episode closed by a rollback or a drift keeps its savings to date, still claimable for the interval it was in force (`application.effective_from_utc` to `effective_to_utc`), and has no annualized run-rate and no ratio any more (both null): the change no longer runs.

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

## 19. Supersession and handoff

Generating a newer PKG does not transfer economic ownership. The old RPKG continues owning its valid applied interval until a new applied state, rollback, drift or invalidation boundary closes it. Replacement packages receive no retroactive credit.

## 20. Corrections

Billing/evidence corrections are append-only:

`ORIGINAL | RETRACTION | RESTATEMENT`

Corrections reference prior facts and cause re-projection; historical records are never overwritten.

A measured window is identified by its application episode and its start and end, not by the measurement id: the measurement id hashes the evidence, so a window restated with other costs gets a new one. The comparison with the earlier measurement is made when the run measures, against the ledger the run started from (the collector cannot see the ledger). The earlier measurements are those recorded before the run's measurement first was, so reprocessing an observation decides what its first run decided.

- A window measured earlier with the same economics is a duplicate: nothing new is recorded.
- A window measured earlier with other economics is a restatement. The run records the new measurement and a `BILLING_CORRECTION_RECORDED` whose `corrects_entry_id` names the latest earlier measurement of that window, whether or not the observation carries a correction object. Without one, the correction's key is the new measurement id, its type `RESTATEMENT`, its version null and its reason `WINDOW_REMEASURED_WITH_DIFFERENT_ECONOMICS`, and the new measurement's `correction_status` is `RESTATEMENT`.
- A window not measured earlier is a correction only when the observation carries a correction object; it corrects the entry the object names, else the latest earlier measurement.
- A correction object keeps its key, type, version and reason. A correction key and version the ledger already holds is not recorded again, so reprocessing a restatement records nothing new.

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

Reasons: `CONFIGURATION_DRIFT` (a drift), `CONTRARY_IMPLEMENTATION` (the opposite of the recommendation applied), `UPSTREAM_CHANGE_INVALIDATES_DOWNSTREAM` (an invalidating or modified application once its remeasurement window passed) and `PENDING_STEP_APPLIED` (a step held for re-evaluation applied while the plan is in progress; the run goes on).

The request id does not depend on the evidence. The same request on a later run (the configuration unchanged) is a duplicate: the ledger keeps the first entry, and the RERUN file keeps the evidence and window of the run that first asked.

A run that writes a file (evidence, regeneration request, realization package, snapshot) whose content is unchanged leaves the existing file alone.

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
