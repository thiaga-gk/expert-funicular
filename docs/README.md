# Documentation index

Contract versions are declared in code (`src/publisher/contracts/reporter_contract.py`, `src/publisher/contracts/workbook_contract.py`) and in each contract document's header. File names carry no version.

| Folder | Document | Read it when |
|---|---|---|
| `architecture/` | [`context.md`](architecture/context.md) | You are new: domain language, invariants and concepts. Read first. |
| | [`hld.md`](architecture/hld.md) | You need the component architecture and its traceability to requirements. |
| | [`lld.md`](architecture/lld.md) | You are changing code: module responsibilities, CLI, internal contracts. |
| | [`repo-reorg-design.md`](architecture/repo-reorg-design.md) | You are working on the package reorganization: target layout, phases and approved decisions. |
| `product/` | [`prd.md`](product/prd.md) | You need what the product must do, scope and acceptance criteria. |
| `contracts/` | [`reporter-json-contract.md`](contracts/reporter-json-contract.md) | You consume or change the `RecommendationPackage` JSON. |
| | [`workbook-contract.md`](contracts/workbook-contract.md) | You consume or change the workbook projection. |
| | [`evidence-format.md`](contracts/evidence-format.md) | You write, replay (`--from-evidence`) or change `evidence.json`. |
| `reference/` | [`source-mapping.md`](reference/source-mapping.md) | You need which system table or API feeds which field. |
| | [`thresholds.md`](reference/thresholds.md) | You are tuning or explaining policy thresholds. |
| | [`blocker-codes.md`](reference/blocker-codes.md) | You need what a blocker code means and which threshold gates it. |
| | [`technique-catalog.md`](reference/technique-catalog.md) | You need stable technique IDs and the algorithm per knob. |
| | [`aws-cost-model.md`](reference/aws-cost-model.md) | You are working on AWS (non-DBU) economics. |
| | [`known-limitations.md`](reference/known-limitations.md) | You need the open limitations of the current release. |
| (root) | [`skills.md`](skills.md) | You are building a publisher for another Databricks service. |
| `proposals/` | [`remediation-runbook.md`](proposals/remediation-runbook.md), [`golden-scenarios.md`](proposals/golden-scenarios.md), [`real-data-run.md`](proposals/real-data-run.md), [`gap-review-2.8.0.md`](proposals/gap-review-2.8.0.md), [`tracker.md`](proposals/tracker.md) | You are resolving open findings: the process, the scenario table and the design record. |
| `archive/` | Historical designs and audits | You need the rationale behind a shipped design. Not maintained. |

## Where each topic lives

Every topic is stated in one place. Other documents point to it instead of restating it.

| Topic | Home |
|---|---|
| Product scope, requirements (FR-/NFR-), acceptance criteria, success metrics, personas, product decisions, out of scope | [`product/prd.md`](product/prd.md) |
| Domain language, semantics, invariants, anti-patterns, worked example, glossary, persona-to-sheet mapping | [`architecture/context.md`](architecture/context.md) |
| Component responsibilities, system context, runtime flows, topology, deployment, trade-offs, failure taxonomy | [`architecture/hld.md`](architecture/hld.md) |
| Algorithm shape and formulas (with named parameters), SQL semantics, state machines, validation steps, failure table, CLI semantics, manifest contents, package evidence | [`architecture/lld.md`](architecture/lld.md) |
| Reporter hierarchy, IDs and hashes, enums, realization, package economics fields | [`contracts/reporter-json-contract.md`](contracts/reporter-json-contract.md) and `src/publisher/contracts/reporter_contract.py` |
| Workbook sheets, grains, columns, presentation | [`contracts/workbook-contract.md`](contracts/workbook-contract.md) and `src/publisher/contracts/workbook_contract.py` |
| Replay evidence | [`contracts/evidence-format.md`](contracts/evidence-format.md) |
| Every numeric default | [`reference/thresholds.md`](reference/thresholds.md) and `Thresholds` in `src/publisher/governor/thresholds.py` |
| Every blocker and unavailability reason code: meaning, when it fires, gating parameter, resulting status | [`reference/blocker-codes.md`](reference/blocker-codes.md) |
| Technique IDs | [`reference/technique-catalog.md`](reference/technique-catalog.md) |
| Source-to-field mapping | [`reference/source-mapping.md`](reference/source-mapping.md) |
| AWS cost formulas | [`reference/aws-cost-model.md`](reference/aws-cost-model.md) |
| Known limitations | [`reference/known-limitations.md`](reference/known-limitations.md) |
| Quickstart, install, output tree | [`../README.md`](../README.md) |
| Precedence and documentation rules | this file |

## Precedence

For behavior, the code and its tests are authoritative, then the locked contracts (`docs/contracts/`, `schema/`, `src/publisher/contracts/*.py`). For product scope and requirements, the PRD is authoritative. Every other document owns the topics in the table above and explains the rest by pointer. When two documents disagree, fix the one that does not own the topic; a disagreement is a documentation bug, not a tie to break.

## Writing rules

- State a topic once, in its home, and point to it from everywhere else with one sentence.
- Move a block into its home before replacing it with a pointer.
- Never delete or renumber a numbered heading that other files cite; replace its body with a pointer.
- Numbers come from `Thresholds` in `src/publisher/governor/thresholds.py` and live only in `reference/thresholds.md`; prose names the parameter.
- Describe the current state in generic wording; keep finding and decision IDs out of these documents.
