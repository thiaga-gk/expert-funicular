# Documentation index

Contract versions are declared in code (`src/publisher/contracts/reporter_contract.py`, `src/publisher/contracts/workbook_contract.py`) and in each contract document's header. File names carry no version.

Documents are grouped by type, then by product: `docs/<type>/<product>/`. The types are `product`, `architecture`, `contracts`, `reference`, `proposals` and `archive`. The products are `publisher`, `realizer` and `shared`; a `shared/` folder exists only where a document serves both products.

| Type | Product | Document | Read it when |
|---|---|---|---|
| `product/` | publisher | [`prd.md`](product/publisher/prd.md) | You need what the product must do, scope and acceptance criteria. |
| | realizer | [`prd.md`](product/realizer/prd.md) | You need what the Realizer must do: scope, requirements and its definition of done. |
| `architecture/` | publisher | [`context.md`](architecture/publisher/context.md) | You are new: domain language, invariants and concepts. Read first. |
| | | [`hld.md`](architecture/publisher/hld.md) | You need the component architecture and its traceability to requirements. |
| | | [`lld.md`](architecture/publisher/lld.md) | You are changing code: module responsibilities, CLI, internal contracts. |
| | | [`repo-reorg-design.md`](architecture/publisher/repo-reorg-design.md) | You are working on the package reorganization: target layout and approved decisions. |
| | realizer | [`repo-reorg-design.md`](architecture/realizer/repo-reorg-design.md) | You are working on the Realizer package layout: the target layout, phases, implementation rules and the outcome of the reorganization. |
| | | [`context.md`](architecture/realizer/context.md), [`hld.md`](architecture/realizer/hld.md), [`lld.md`](architecture/realizer/lld.md) | You need the Realizer's domain language, component architecture and low-level design (the v1.0.0 specification). |
| `contracts/` | shared | [`reporter-json-contract.md`](contracts/shared/reporter-json-contract.md) | You consume or change the `RecommendationPackage` JSON (the Publisher to Realizer hand-off). |
| | publisher | [`workbook-contract.md`](contracts/publisher/workbook-contract.md) | You consume or change the workbook projection. |
| | | [`evidence-format.md`](contracts/publisher/evidence-format.md) | You write, replay (`--from-evidence`) or change `evidence.json`. |
| | realizer | [`ledger-contract.md`](contracts/realizer/ledger-contract.md), [`policy-contract.md`](contracts/realizer/policy-contract.md), [`source-evidence-contract.md`](contracts/realizer/source-evidence-contract.md), [`workbook-compatibility-contract.md`](contracts/realizer/workbook-compatibility-contract.md) | You consume or change the Realizer's JSON, ledger, policy, observation or workbook-compatibility contract (`schema/realization-package.schema.json`, `schema/realization-ledger-entry.schema.json`). |
| `reference/` | publisher | [`source-mapping.md`](reference/publisher/source-mapping.md) | You need which system table or API feeds which field. |
| | | [`thresholds.md`](reference/publisher/thresholds.md) | You are tuning or explaining policy thresholds. |
| | | [`product-constants.md`](reference/publisher/product-constants.md) | You need a documented Databricks limit or behavior the rules rely on (scale-up lag, auto-stop range, cluster limits). |
| | | [`blocker-codes.md`](reference/publisher/blocker-codes.md) | You need what a blocker code means and which threshold gates it. |
| | | [`technique-catalog.md`](reference/publisher/technique-catalog.md) | You need stable technique IDs and the algorithm per knob. |
| | | [`aws-cost-model.md`](reference/publisher/aws-cost-model.md) | You are working on AWS (non-DBU) economics. |
| | | [`known-limitations.md`](reference/publisher/known-limitations.md) | You need the open limitations of the current release. |
| | realizer | [`known-limitations.md`](reference/realizer/known-limitations.md) | You need what the Realizer cannot do yet (input contract, no live collector, no `.xlsx` writer) and the original behaviors it keeps for parity. |
| | shared | [`skills.md`](reference/shared/skills.md) | You are building a publisher for another Databricks service. |
| `proposals/` | publisher | [`remediation-runbook.md`](proposals/publisher/remediation-runbook.md), [`golden-scenarios.md`](proposals/publisher/golden-scenarios.md), [`real-data-run.md`](proposals/publisher/real-data-run.md), [`gap-review-2.8.0.md`](proposals/publisher/gap-review-2.8.0.md), [`tracker.md`](proposals/publisher/tracker.md), [`repo-reorg-parity.md`](proposals/publisher/repo-reorg-parity.md) | You are resolving open findings: the process, the scenario table and the design record. |
| | realizer | [`repo-reorg-parity.md`](proposals/realizer/repo-reorg-parity.md), [`golden-scenarios.md`](proposals/realizer/golden-scenarios.md), [`implementation-release-plan.md`](proposals/realizer/implementation-release-plan.md), [`release-evidence.md`](proposals/realizer/release-evidence.md), [`readiness-review.md`](proposals/realizer/readiness-review.md), [`gap-review.md`](proposals/realizer/gap-review.md), [`tracker.md`](proposals/realizer/tracker.md), [`real-data-handoff.md`](proposals/realizer/real-data-handoff.md), [`batch3-handoff.md`](proposals/realizer/batch3-handoff.md), [`batch4-handoff.md`](proposals/realizer/batch4-handoff.md), [`batch5-handoff.md`](proposals/realizer/batch5-handoff.md) | You need the Realizer's reorganization record, golden scenarios, the frozen v1.0.0 release plan, evidence and readiness review, the open gap-review findings and their tracker, the plan of the real-data run, or the handoffs of the gap-review batches. |
| `archive/` | publisher | `archive/publisher/` | Historical designs and audits. You need the rationale behind a shipped design. Not maintained. |

## Where each topic lives

Every topic is stated in one place. Other documents point to it instead of restating it.

| Topic | Home |
|---|---|
| Product scope, requirements (FR-/NFR-), acceptance criteria, success metrics, personas, product decisions, out of scope | [`product/publisher/prd.md`](product/publisher/prd.md) |
| Domain language, semantics, invariants, anti-patterns, worked example, glossary, persona-to-sheet mapping | [`architecture/publisher/context.md`](architecture/publisher/context.md) |
| Component responsibilities, system context, runtime flows, topology, deployment, trade-offs, failure taxonomy | [`architecture/publisher/hld.md`](architecture/publisher/hld.md) |
| Algorithm shape and formulas (with named parameters), SQL semantics, state machines, validation steps, failure table, CLI semantics, manifest contents, package evidence | [`architecture/publisher/lld.md`](architecture/publisher/lld.md) |
| Reporter hierarchy, IDs and hashes, enums, realization, package economics fields | [`contracts/shared/reporter-json-contract.md`](contracts/shared/reporter-json-contract.md) and `src/publisher/contracts/reporter_contract.py` |
| Workbook sheets, grains, columns, presentation | [`contracts/publisher/workbook-contract.md`](contracts/publisher/workbook-contract.md) and `src/publisher/contracts/workbook_contract.py` |
| Replay evidence | [`contracts/publisher/evidence-format.md`](contracts/publisher/evidence-format.md) |
| Run scope: what `--scope`, `--scope-file` and `--warehouse` restrict and how each capability applies it | [`architecture/publisher/lld.md`](architecture/publisher/lld.md) section 5.4 (requirement FR-OPT-011 in [`product/publisher/prd.md`](product/publisher/prd.md); package field `run_scope` in the reporter contract; scope-file schema `schema/run-scope.schema.json`) |
| Every numeric default | [`reference/publisher/thresholds.md`](reference/publisher/thresholds.md) and `Thresholds` in `src/publisher/governor/thresholds.py` |
| Databricks product constants | [`reference/publisher/product-constants.md`](reference/publisher/product-constants.md) and `PRODUCT_CONSTANTS` in `src/publisher/config/defaults.py` |
| Every blocker and unavailability reason code: meaning, when it fires, gating parameter, resulting status | [`reference/publisher/blocker-codes.md`](reference/publisher/blocker-codes.md) |
| Technique IDs | [`reference/publisher/technique-catalog.md`](reference/publisher/technique-catalog.md) |
| Source-to-field mapping | [`reference/publisher/source-mapping.md`](reference/publisher/source-mapping.md) |
| AWS cost formulas | [`reference/publisher/aws-cost-model.md`](reference/publisher/aws-cost-model.md) |
| Known limitations | [`reference/publisher/known-limitations.md`](reference/publisher/known-limitations.md); Realizer: [`reference/realizer/known-limitations.md`](reference/realizer/known-limitations.md) |
| Realizer: scope and requirements, domain language, architecture, ledger/policy/observation/workbook-compatibility contracts, golden scenarios, the reorganization and its parity record (open limitations: the row above; runnable example: [`../examples/realizer/README.md`](../examples/realizer/README.md)) | [`product/realizer/prd.md`](product/realizer/prd.md), [`architecture/realizer/`](architecture/realizer/context.md), [`contracts/realizer/`](contracts/realizer/ledger-contract.md), [`proposals/realizer/`](proposals/realizer/golden-scenarios.md); code in `src/realizer/`, schemas `schema/realization-package.schema.json` and `schema/realization-ledger-entry.schema.json` |
| Neutral read-only Databricks access (OAuth, REST GET, the SQL client) and Databricks vocabulary shared by both products | `src/workflow_shared/` (it imports neither product); module table in [`architecture/publisher/lld.md`](architecture/publisher/lld.md) section 3.1 |
| Quickstart, install, output tree (run folders), run-scope examples | [`../README.md`](../README.md) |
| Precedence and documentation rules | this file |

## Precedence

For behavior, the code and its tests are authoritative, then the locked contracts (`docs/contracts/`, `schema/`, `src/publisher/contracts/*.py`). For product scope and requirements, the PRD is authoritative. Every other document owns the topics in the table above and explains the rest by pointer. When two documents disagree, fix the one that does not own the topic; a disagreement is a documentation bug, not a tie to break.

## Writing rules

- State a topic once, in its home, and point to it from everywhere else with one sentence.
- Move a block into its home before replacing it with a pointer.
- Never delete or renumber a numbered heading that other files cite; replace its body with a pointer.
- Numbers come from `Thresholds` in `src/publisher/governor/thresholds.py` and live only in `reference/publisher/thresholds.md` (Databricks product constants: `reference/publisher/product-constants.md`); prose names the parameter.
- Describe the current state in generic wording; keep finding and decision IDs out of these documents.
