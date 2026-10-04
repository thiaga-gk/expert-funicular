# Documentation index

Contract versions are declared in code (`contracts/reporter_contract.py`, `contracts/workbook_contract.py`) and in each contract document's header. File names carry no version.

| Folder | Document | Read it when |
|---|---|---|
| `architecture/` | [`context.md`](architecture/context.md) | You are new: domain language, invariants and concepts. Read first. |
| | [`hld.md`](architecture/hld.md) | You need the component architecture and its traceability to requirements. |
| | [`lld.md`](architecture/lld.md) | You are changing code: module responsibilities, CLI, internal contracts. |
| `product/` | [`prd.md`](product/prd.md) | You need what the product must do, scope and acceptance criteria. |
| `contracts/` | [`reporter-json-contract.md`](contracts/reporter-json-contract.md) | You consume or change the `RecommendationPackage` JSON. |
| | [`workbook-contract.md`](contracts/workbook-contract.md) | You consume or change the workbook projection. |
| `reference/` | [`source-mapping.md`](reference/source-mapping.md) | You need which system table or API feeds which field. |
| | [`thresholds.md`](reference/thresholds.md) | You are tuning or explaining policy thresholds. |
| | [`technique-catalog.md`](reference/technique-catalog.md) | You need stable technique IDs and the algorithm per knob. |
| | [`aws-cost-model.md`](reference/aws-cost-model.md) | You are working on AWS (non-DBU) economics. |
| | [`known-limitations.md`](reference/known-limitations.md) | You need the open limitations of the current release. |
| (root) | [`skills.md`](skills.md) | You are building a publisher for another Databricks service. |
| `proposals/` | [`remediation-runbook.md`](proposals/remediation-runbook.md), [`golden-scenarios.md`](proposals/golden-scenarios.md), [`gap-review-2.8.0.md`](proposals/gap-review-2.8.0.md) | You are resolving open findings: the process, the scenario table and the design record. |
| `archive/` | Historical designs and audits | You need the rationale behind a shipped design. Not maintained. |

Precedence (PRD section 27): the PRD and the locked contract documents take precedence over the HLD, LLD and reference documents. Treat any disagreement as a documentation bug and fix the lower-level document.
