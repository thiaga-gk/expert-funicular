# Adversarial Challenge Catalog

## F1 — First Principles
- Is the optimizer changing a true cost/performance driver or merely a correlated knob?
- Is the objective explicit and service-appropriate?
- Are demand, resource state, pressure, performance/reliability, consumption, and cost linked coherently?

## F2 — Causal Mechanism
- Can the evidence support the claimed mechanism?
- What evidence would falsify the causal hypothesis, and is it tested?
- Could another mechanism explain the same observed signal?

## F3 — Constraints First
- Can an economically attractive change bypass a known hard constraint?
- Can unknown required constraint evidence be treated as satisfied?
- Are constraints evaluated on the proposed state rather than only current state?
- Can a safe canary path be distinguished from insufficient evidence?

## F4 — Counterfactual
- Does the candidate cost/performance model change exactly the state variables declared by the intervention?
- Can null/unavailable cost become zero?
- Can signed cost increase be mislabeled as savings?
- Does the model omit an expected runtime/resource effect that favors savings?

## F5 — Second Order
- Can candidate savings be added/double-counted?
- Can an upstream change leave a downstream candidate `READY` instead of `PENDING_REEVALUATION`?
- Are migration branches mutually exclusive with in-place tuning?
- Can an intervention create a new bottleneck that is never remeasured?
- Can stale/historical evidence satisfy a current-regime decision?

## F6 — Validation / Learning
- Are exact success metrics/thresholds defined where possible?
- Are rollback triggers and rollback action explicit?
- Does realization preserve original package identity?
- Can a failed canary still be marked successful or realized?

## Cross-cutting
- Can scenario confidence exceed the weakest material step without an explicit method?
- Can workbook calculations diverge from JSON?
- Can volatile timestamps change IDs?
- Can identical inputs produce different output ordering/IDs?
- Can compatibility APIs override authoritative config?
- Can unsupported/future resource types fall through supported algorithms?
- Can secrets/raw SQL appear in logs/evidence?
- Can an absent source silently reduce a blocker to NO_CHANGE?
