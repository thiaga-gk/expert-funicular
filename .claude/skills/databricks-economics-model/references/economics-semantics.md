# Economics Semantics

## Actual
Observed historical spend in a declared window. It may span prior configurations and must not automatically become the decision baseline.

## Forward baseline
Expected future spend if the current configuration/regime remains unchanged. Must declare model/window/assumptions.

## Candidate counterfactual
Expected forward impact if exactly one candidate were applied to the baseline state. Non-additive.

## Scenario counterfactual
Ordered stateful bundle estimate. After an upstream change, downstream estimates must use the changed state or be invalidated/re-evaluated.

## Package envelope
Across economically assessable mutually exclusive scenarios: low=min scenario low; base=recommended scenario base; high=max scenario high. Keep source scenario for each bound.

## Realization
Measured post-change outcome. Track DBU/cloud/total separately; annualize with an explicit method; preserve rollback/validation result.

## Signed impact
Positive = savings; negative = cost increase; zero = known no impact; null = unavailable.
