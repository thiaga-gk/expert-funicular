---
name: databricks-source-contract
description: Design and validate the authoritative data-source contract for a Databricks optimization publisher. Use this skill whenever the publisher needs system-table/API/billing/event source mapping, collector SQL design, field provenance, freshness/retention/regional-scope rules, source readiness checks, current-regime evidence, compatibility-only fallbacks, or a decision about whether a metric is AUTHORITATIVE, OBSERVED, DERIVED, COMPATIBILITY_ONLY, or UNAVAILABLE. Trigger it before collector implementation and whenever source coverage changes, especially when causal hypotheses or optimization constraints require new evidence.
---

# Databricks Source Contract

Act as a source/provenance advisor and contract builder. Do not implement optimization policy.

Read the sibling orchestrator's artifact protocol and `references/optimization-thinking-framework.md`. Read `references/source-authority.md` and `references/collector-design.md`.

## Procedure

1. Load the approved `discovery/service-profile.yaml` and `discovery/system-model.yaml`.
2. For every resource identity field, current configuration field, causal variable, metric, constraint metric, attribution field, compatibility check, and cost input required by proposed techniques/economics, identify the source and grain.
3. Classify each field as `AUTHORITATIVE`, `OBSERVED`, `DERIVED`, `COMPATIBILITY_ONLY`, or `UNAVAILABLE`.
4. Record whether each field supports `IDENTITY`, `REGIME`, `CAUSAL_SIGNAL`, `CONSTRAINT`, `COUNTERFACTUAL`, `ECONOMICS`, `VALIDATION`, or multiple consumers.
5. Record source scope: regional/account/global, retention, freshness, permissions, expected nullability, corrections/restatements, and temporal join requirements.
6. Define configuration-regime/epoch construction inputs. Only authoritative configuration changes may define the current decision regime unless the service contract explicitly says otherwise.
7. Design set-based collector SQL that aggregates near the source. Do not pull raw high-volume events/queries when a deterministic aggregate is sufficient.
8. Define read-only API fallbacks only for compatibility/target resolution when system tables cannot answer the question. Never let a compatibility API silently override authoritative optimizer configuration.
9. Create readiness checks for missing table access, retention, region coverage, attribution, pricing, permissions, **hard-constraint evidence**, and validation metrics.
10. Write/update `sources/source-contract.yaml`, `sources/source-readiness.md`, and `sources/collector-sql/*`.
11. Run `scripts/validate_source_contract.py` before handoff.

## Constraints-first source rule

A technique cannot be promoted to active if the publisher cannot observe the evidence required to judge its hard post-change constraints, unless the approved optimization policy defines a safe empirical canary path. Record this limitation explicitly; do not substitute a weak proxy without approval.

## Fail closed

Return a blocker instead of inventing values when a required authoritative source or hard-constraint signal is unavailable or ambiguous. Unknown is not zero and unknown is not satisfied.

## Source contract field minimum

Each field entry must include:
`field`, `semantic`, `source`, `source_field`, `classification`, `grain`, `required`, `nullable`, `scope`, `freshness`, `retention`, `epoch_role`, `correction_semantics`, `fallback`, `consumers`.
