# Reference Architecture

```mermaid
flowchart LR
  S[Databricks sources] --> C[Collector]
  C --> N[Normalizer / current regime]
  N --> G{Supported-type gate}
  G -->|supported| A[Analyzer]
  G -->|unsupported| X[Fail closed]
  A --> CE[Constraint evaluation]
  CE --> O[Deterministic optimizer]
  O --> E[Candidate estimator]
  E --> R1[Reviewer 1]
  R1 --> P[Scenario planner]
  P --> B[Stateful bundle estimator]
  B --> SCE[Scenario constraint evaluation]
  SCE --> R2[Reviewer 2]
  R2 --> J[RecommendationPackage]
  J --> V[Semantic + JSON Schema validation]
  V --> F[packages/*.json]
  J --> W[Workbook projection]
```

The optimization policy is lexicographic: hard constraints/feasibility first, then the approved primary objective, then secondary objectives. Economics never overrides a failed hard constraint.

Constraint evaluation may be implemented as a dedicated class or deterministic pure functions. Keep the single-`publisher.py` core during fast iteration unless there is an approved reason to split it.

LLMs may help research/explain/challenge outside this runtime path; they must not be the authoritative decision engine.
