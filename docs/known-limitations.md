# Known Limitations — v2.7.0

This file records limitations that remain true in the sealed testing baseline. Resolved implementation gaps are intentionally not retained here.

1. **Advisory/read-only:** the publisher does not apply SQL warehouse configuration changes. HITL execution remains external.
2. **Realization ingestion:** the public JSON contract supports realization, but Phase 1 emits `NOT_APPLIED`; automated post-change measurement ingestion is not implemented.
3. **Supported warehouse types:** active optimization is limited to `CLASSIC`, `PRO`, and standard `SERVERLESS`. Lakehouse Real-Time and unknown/future types fail closed at the supported-type gate.
4. **Historical AWS actual cost:** without CUR/effective-rate integration, customer AWS infrastructure actuals are not authoritative. Forward Pro/Classic AWS cost uses the versioned regional on-demand registry and is modeled.
5. **AWS pricing scope:** modeled AWS forward cost does not include organization-specific discounts, RI/Savings Plans, support, network, tax, or other charges unless explicitly represented in a future pricing source contract.
6. **Migration economics:** Serverless/Pro migration economics are pre-canary modeled ranges and require compatibility review plus canary validation.
7. **Photon:** Photon is an observed current-epoch billing state (`product_features.is_photon`), not a direct authoritative warehouse-config field.
8. **Compatibility evidence:** read-only compatibility API evidence is optional/non-authoritative; unavailable evidence falls back to HITL/unknown rather than guessed compatibility.
9. **Query-source attribution:** query source is multi-label where applicable; category percentages are not assumed to form a mutually exclusive distribution.
10. **Out-of-scope knobs:** query rewrite, storage optimization, scheduling optimization, Spot policy tuning, and statement-timeout tuning are outside the current active technique set.
11. **Workbook:** `workbook.xlsx` is a presentation projection only. Numeric annual-impact columns preserve signed economics; positive values are savings, negative values are cost increases, and unavailable values are not coerced to zero.
