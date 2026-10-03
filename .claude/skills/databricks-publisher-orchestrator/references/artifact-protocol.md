# Publisher Artifact Protocol

## Purpose
Use artifacts as the stable API between advisor, builder, verifier, and release skills. Chat prose is not an authoritative handoff.

## Root
`artifacts/databricks/<service-id>/`

## Required structure
```text
artifacts/databricks/<service-id>/
├── registry.yaml
├── discovery/
│   ├── service-profile.yaml
│   ├── system-model.yaml
│   ├── service-context.md
│   └── open-decisions.yaml
├── sources/
│   ├── source-contract.yaml
│   ├── source-readiness.md
│   └── collector-sql/
├── catalog/
│   ├── optimization-policy.yaml
│   ├── technique-catalog.yaml
│   └── dependency-graph.yaml
├── economics/
│   └── economics-contract.yaml
├── contracts/
│   ├── domain-contract.yaml
│   ├── reporter-contract.yaml
│   └── workbook-contract.yaml
├── tests/
│   ├── golden-scenarios.yaml
│   └── acceptance-criteria.yaml
└── release/
    └── release-evidence.yaml
```

## Artifact states
`DRAFT`, `REVIEW_REQUIRED`, `APPROVED`, `SUPERSEDED`.

## Ownership
- discovery skill owns `discovery/*`
- source skill owns `sources/*`
- technique skill owns `catalog/*`, including optimization policy and F1-F6 technique records
- economics skill owns `economics/*`
- contract skill owns `contracts/*`
- validator owns test evidence and may propose changes but does not silently modify approved design artifacts
- release sealer owns `release/*`

## Framework traceability
Each active technique must be traceable through:

`system-model causal driver -> source evidence -> technique F1/F2/F3 -> candidate counterfactual F4 -> dependency graph F5 -> validation/golden/realization F6`.

The artifact registry should record the content hashes/versions of upstream artifacts that affect runtime decisions.

## Registry fields
Each artifact entry records path, version, status, content hash, owner skill, dependencies, approved_by/approved_at when applicable, and supersedes when relevant.

## Change rule
If an approved upstream artifact changes, mark dependent artifacts `REVIEW_REQUIRED` and invalidate affected implementation/release evidence. A change to hard constraints, scenario selection, causal mechanism, or counterfactual semantics is decision-affecting and must not leave downstream artifacts marked approved.

## Generic resource vocabulary
Use `resource` in reusable contracts. Service-specific adapters may expose `warehouse`, `job_cluster`, `pipeline`, etc. Do not copy SQL Warehouse names into generic publisher contracts.
