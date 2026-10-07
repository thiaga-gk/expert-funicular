# Realizer JSON Contract v1.2.0 + Realization Ledger Contract v1.1.0

> The Realizer specification (realization package contract v1.2.0, ledger envelope v1.1.0; v1.2.0 added the open episodes, realized to date with its coverage, the owner of a change another lineage claims (`attribution.claimed_by`) and the successor package, and ledger v1.1.0 the events `APPLICATION_CLAIMED_BY_OTHER_LINEAGE`, `SUCCESSOR_PACKAGE_LINKED` and `POLICY_DIFF_RECORDED`; v1.1.0 added the step role, the `SAFETY_ACTION_APPLIED`, `PERFORMANCE_VALIDATED` and `CAPACITY_VALIDATED` states, the application's compatibility status and annotations, and the source package's run scope, cost profile and observed change; RPKG and RLE ids hash only the major version). The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

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
1 warehouse -> 0..N RPKG (one per package realized into the state directory)
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

No defensible counterfactual means no claimable realized savings. A warehouse-type migration is the diagnostic case that is still measured: its baseline is the pre-change window carried forward at the pre-change query mix (method `CORRELATED_ONLY`, [`source-evidence-contract.md`](source-evidence-contract.md) section 18), so it is reported as an observed delta and never claim-eligible.

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
- a warehouse-type migration: the scenario's range, which is pre-canary, so it is informational (`classification_basis` `INFORMATIONAL`): the realization (the observed delta of the migration, with its modeled AWS component) and its ratio are reported, the state stays `MONITORING` (`MIGRATION_RANGE_INFORMATIONAL`), never `REALIZED` or `UNDER_REALIZED` against it; a migration measured without a published range stays `MONITORING` with `VALIDATED_VALUE_UNCLASSIFIED`;
- any denominator when the measured query volume moved outside the comparable band after the change: reported, informational (`WORKLOAD_SHIFT`, [`policy-contract.md`](policy-contract.md) section 7).

The component compared is the measurement's cost basis (DBU only, or DBU and modeled AWS); a caller-supplied observation that gives a DBU component and no AWS component is compared with the DBU range (its costs are DBU only), any other caller-supplied observation with the total, and a package without a total is `UNAVAILABLE`, never the DBU range in its place. The expected savings per component (DBU, AWS, total) of the chosen figure are reported separately as `expected_components`.

## 17. Attribution

Economic realization and recommendation attribution are separate. A valid observed economic improvement can exist when origin is `UNKNOWN` or `INFERRED`; strict recommendation-attributed KPIs require policy-qualified causal evidence.

When multiple changes are deployed together and contribution cannot be separated defensibly, Realizer reports scenario-level realization rather than fabricating candidate allocations.

A warehouse-type migration's observed delta is never claimed: its attribution is `OBSERVABLE_ONLY` (`CORRELATED_ONLY`, or `PARTIAL_ATTRIBUTION` for an inferred origin) with the hard disqualifier `NO_REVERSE_MIGRATION_MODEL` and nothing attributed to the recommendation. There is no model that prices the query mix on the type the warehouse left, so the delta cannot be separated from whatever else moved the bill; the disqualifier lifts only when such a model exists.

## 18. Economic exclusivity

One source economic atom / economic basis / interval belongs to at most one authoritative realization lineage. Unresolved overlap blocks financial-grade portfolio publication.

Several packages of one warehouse can be realized into one state directory (later Publisher runs of the same epoch, cost-profile or scope alternatives), and one configuration change can realize a step of each. A change is a knob set to a value over an interval (from the application's effective time to the drift or rollback that ends it); two applications are of the same change when they set the same knob to the same value over intersecting intervals. Of the lineages whose authoritative application (`EXACT` or `MODIFIED`) realizes the change, the owner is the one whose package was generated most recently at or before the change; at equal generation time `EXACT` before `MODIFIED`, then the lowest package id. A package generated after the change already started from the applied value and never owns it. Every other such lineage records `APPLICATION_CLAIMED_BY_OTHER_LINEAGE` on its application episode (payload: the knob, value and effective time, the owner's RPKG, PKG, generation time, conformance and application episode, and the rule `MOST_RECENT_PACKAGE_BEFORE_CHANGE`); its realization is still measured and reported but is observable only: hard disqualifier `CLAIMED_BY_OTHER_LINEAGE`, `claimed_by` naming the owner, nothing attributed (section 4). A scenario-scope application is a claim on each knob of its applied steps. The decision is the same whichever package is realized first: a lineage that claimed a change alone yields when the owner's application is recorded later, and when several entries name owners the projection keeps the best-ranked one.

Example: an aggressive-profile package (auto-stop 365 to 15) and a balanced-profile package (365 to 210) of one epoch, both generated 2026-10-01 12:00; the owner sets 210 on 2026-10-08 14:32 and the replay measures 1,069.73 over 28 days (13,944.69 a year). Alone, each lineage reports 1,069.73 claimable; together the balanced lineage (`EXACT`) claims 1,069.73 and the aggressive lineage (`MODIFIED`) is observable only, naming it, in either run order.

The caller's `overlap_count` remains an extra restriction (`PORTFOLIO_SCOPE_OVERLAP`); it never removes one.

## 19. Supersession and handoff

Generating a newer PKG does not transfer economic ownership. The old RPKG continues owning its valid applied interval until a new applied state, rollback, drift or invalidation boundary closes it. Replacement packages receive no retroactive credit.

Across the lineages of a warehouse in one state directory, each run records:

- **Same epoch, newer package.** A lineage is superseded (`SOURCE_PACKAGE_SUPERSEDED` and `REPLACEMENT_PACKAGE_LINKED`, reason `NEWER_PACKAGE_SAME_EPOCH`) by the newest package of its epoch generated after it: the Publisher saw the same configuration again, so nothing was applied in between. Daily runs therefore leave one active package per epoch instead of an accumulating list of never-applied ones. Packages generated at the same time (profile alternatives) never replace each other.
- **Successor.** A package whose `observed_change.previous_epoch_id` is the lineage's epoch follows it: the earliest such package is linked as its successor (`SUCCESSOR_PACKAGE_LINKED`, `source_recommendation.successor_package_id`). When the configuration change that started the successor's epoch applied none of the lineage's authority-eligible steps, the lineage is superseded by the successor (reason `CONFIGURATION_CHANGED`). When it applied one, the lineage keeps realizing and is superseded only when the successor's expected savings (its recommended scenario's total base, else DBU base) are a material improvement over the lineage's, at least the policy's `minimum_material_savings_delta_pct` (5 percent) more (reason `MATERIAL_IMPROVEMENT`); an unpriced successor is never one.
- **Rollback by a later package.** When a later package's canary rollback step (step role `SAFETY_ROLLBACK`) is applied, returning a knob to the value it had before an earlier lineage's application of that knob, and the later package's observed change starts from the earlier lineage's epoch, the earlier lineage records `ROLLBACK_OBSERVED` (reason `CANARY_ROLLBACK`, naming the rollback package and its application) effective at the time the rollback was applied; its application state becomes `ROLLED_BACK` from then.

The supersession, succession and rollback events take their times from the facts they follow from (the newer package's generation time; the rollback's effective time and the later of the two applications' record times). Supersession never stops a valid realization: a superseded lineage that owns an applied change keeps measuring and claiming it. A caller can still name a replacement explicitly (`replacement_package_id`: a package id in the contract format, never the package itself); the explicit replacement is the one the snapshot reports when both exist.

Example: PKG-A (2026-10-01, auto-stop 365 to 210) is applied 2026-10-08; PKG-B (2026-10-15) has auto-stop 210 as its current value and recommends a size cut. PKG-A owns the auto-stop interval from 2026-10-08 and keeps claiming 1,069.73; PKG-B is linked as its successor; PKG-B's plan is unpriced, so PKG-A stays active; PKG-B can claim only its own size change.

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

## 24. Lineages of one warehouse

The history of every warehouse is one state directory: the ledgers, evidence, regeneration requests, measurement evidence, source package copies, policies and state index below, all under `realization/` (`RealizationRequest.state_dir`; the CLI's `--state-dir`, by default `<output-dir>/state`). A run writes its realization packages and snapshots (`realization/packages/`, `realization/snapshots/`) under its output directory instead: for the CLI a new run folder `<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/` per run, which also holds the run log `realizer.log`. In the single-folder layout (the facade's default, the CLI's `--flat-output`) the state directory is the output directory. The paths under each root are the same in both layouts, so runs into fresh run folders that share one state continue one history and decide what runs into one directory decide.

A run reads, besides its own ledger, every other lineage of its warehouse in the state directory, and writes what it learns about each of them:

- `realization/sources/<workspace>__<warehouse>/<PKG>.json` is the copy of the source package a lineage was created from, written when the lineage is created (the Publisher's package is unchanged; the copy is how another run finds the lineage and re-projects its snapshot). A copy is checked like any package when it is read (schema, id and full hash): an edited copy, or a corrupt ledger of another lineage, fails the run before anything else is written. A lineage created before its copy existed is seen once its package is realized again.
- The cross-lineage events of sections 18 and 19 are decided by the Reconciler from all lineages together (the run's own including the applications it is about to record) and appended after the run's own events: to the run's own ledger, or to the other lineage's ledger under that ledger's advisory lock for the append (a ledger that stays locked keeps the event for a later run, which decides it again). An event already recorded is suppressed by its id. The other lineages whose ledgers grew are re-projected: their realization package and a new snapshot are written.
- `realization/policies/<PSNAP>.json` holds the settings of each policy a validation or a PolicyDiff was recorded under ([policy contract](policy-contract.md) section 4).
- `realization/state/<workspace>__<warehouse>.json` is the warehouse's state index, a view rebuilt from the ledgers on every run and never read back (never a second source of truth): `state_index_version` (1.0.0), the warehouse, `as_of_utc`, `active_package_ids` (the packages not superseded), `applied_knob_owners` (per applied knob, the lineage that claims it) and `lineages`, one row per lineage, oldest package first: its RPKG, PKG and snapshot ids, epoch and previous epoch, generation time, cost profile, disposition, replacement, successor, application state, applied knob, value and time, realization state, claim status, attributed savings, `claimed_by` and `as_of_utc`.

Realizing the same packages with the same observations in any order gives every lineage the same final decisions (owner, claim status, disposition, replacement, successor, rollback). When each lineage learns the same facts in either order, as for two packages of one generation time, the ledgers, realization packages and state index are identical files too. Otherwise a ledger can hold extra history of what was known in between (a package superseded first by the next day's package, then by the newest), so its snapshot id differs while its decisions do not; snapshots already written are history and stay.

**Fleet run.** A fleet run (`realizer --publisher-run`) realizes the warehouses of a Publisher run one at a time, in workspace and warehouse order. For each it holds the warehouse's advisory lock in the state (`realization/state/<workspace>__<warehouse>.lock`, present only while held; a single-package CLI run holds it too), so a second run of the same warehouse at the same time is refused for that warehouse (`warehouse <workspace>/<warehouse> is locked by another run`) instead of deciding the cross-lineage events from ledgers another run is changing; the per-ledger lock of section 9 stays. Under it the run realizes every open lineage kept in the state, oldest package first, and then the Publisher run's package (once, also when it is itself an open lineage), each with its own observation from the run's source. A lineage is open when its package is not superseded (`active_package_ids`) or it owns an applied knob (`applied_knob_owners`), both decided from the ledgers, never from the state index file. The owner of a change therefore records its application, validation and measurement in every daily run, whichever package the Publisher generated last: with PKG-A (auto-stop 365 to 210, applied 2026-10-08) kept in the state and a Publisher run carrying only PKG-B (2026-10-15), the run realizes PKG-A first, which claims 1,069.73, then PKG-B, linked as its successor. A failure is isolated to its warehouse: a refusal is recorded with its message, any other exception by its type only, and the other warehouses are realized. The run writes a run manifest (`manifest.json`, run manifest version 1.0.0: versions and contracts, source, as-of time, the Publisher run folder and its package counts, the state directory, the workbook file, status counts, and per warehouse its package, status (`OK`, `REFUSED`, `CRASHED`), reason, realization package ids and the realization packages and snapshots it wrote, relative to the run folder; sorted keys, no wall-clock time) and the realization workbook (`realization_workbook.xlsx`) into its run folder. The exit status is 1 when any warehouse crashed, else 2 when any was refused, else 0.

**Retention.** Nothing is pruned by the Realizer. A run folder holds projections only (realization packages, snapshots, run manifest, realization workbook, log), rebuilt from the state by any later run, so old run folders can be deleted; a fleet run of the 41-warehouse demo writes about 1 MB. Measurement evidence (`realization/measurements/`) grows by one file per package and measurement time, about 85 KB per warehouse, so a daily live run of 41 warehouses adds about 3.4 MB a day (about 1.2 GB a year); it backs `--from-evidence` replay and the audit of a recorded measurement, so keep at least each lineage's newest file and any file whose measurement a ledger records that may need to be replayed. Ledgers, source package copies and policies are never deleted: the ledger is the history, and a lineage without its source copy cannot be read by another run.
