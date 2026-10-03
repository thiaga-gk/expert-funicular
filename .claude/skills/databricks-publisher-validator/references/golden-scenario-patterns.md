# Golden Scenario Patterns

Every scenario fixture should state:
- resource identity/type/current config regime
- source evidence and coverage/freshness
- first-principles driver / causal hypothesis being exercised
- expected findings
- expected hard/validation/soft constraint states
- expected candidate status/current/recommended value
- expected blockers/confidence band
- expected counterfactual economics availability/range semantics
- expected second-order dependencies/invalidation and remeasurement
- expected canary success criteria / rollback behavior when applicable
- expected scenario steps and feasibility
- expected recommended scenario policy
- expected realization semantics where relevant
- expected package/hash stability inputs

Minimum framework cases:
- hard constraint satisfied
- hard constraint violated
- required constraint evidence unknown
- safe-canary path for pending validation
- canary failure and rollback
- causal falsifier present
- second-order invalidation
- new bottleneck/re-analysis

Keep fixtures minimal enough to explain but complete enough to execute the real pipeline.
