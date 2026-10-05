# Reporter Contracts

The repository has two locked external reporting contracts:

- **Reporter JSON Contract v2.0.0** — authoritative semantic/machine contract. `reporter_contract.py` contains dataclasses, enums, content-addressed IDs, and semantic validators; `reporter_schema.py` supports the committed Draft 2020-12 JSON Schema.
- **Workbook Reporter Contract v1.4.0** — human presentation projection. `workbook_contract.py` maps validated `RecommendationPackage` objects into the four normalized workbook row grains using numeric, currency-explicit economics. It contains no optimization or estimation logic.

`src/publisher/reporter/workbook_reporter.py` renders the projection to XLSX. JSON packages remain the source of truth.

The same package holds the internal typed boundaries between pipeline stages, which are not public contracts: `source.py` (normalized evidence), `analysis.py` (findings), `candidate.py` (decisions and candidates), `scenario.py` (plan steps and scenarios) and `review.py` (the reviewer-gated orchestration result).
