# Workbook Projection

The workbook is a human projection only. It must never calculate optimization decisions, IDs, confidence, scenario selection, or counterfactual economics.

Default visible sheets:
1. Executive Summary
2. Resource Portfolio
3. Scenario Plans
4. Recommendation Registry

Detailed sheet row grains must be exact and machine-testable. Store exact ordered columns/groups in the contract module; the renderer consumes that declaration rather than maintaining a second list.

Financial values: numeric when available; signed impact; explicit currency and impact type; unavailable displayed as an explicit presentation state, never zero. No macros/hidden business formulas/hidden sheets.
