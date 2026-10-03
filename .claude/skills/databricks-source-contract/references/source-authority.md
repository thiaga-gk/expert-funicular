# Source Authority

- **AUTHORITATIVE**: canonical current configuration/identity source that may drive decisions directly.
- **OBSERVED**: measured behavior/attribution; may support inference but must not be mislabeled as config.
- **DERIVED**: deterministic calculation from declared inputs.
- **COMPATIBILITY_ONLY**: read-only evidence used to validate feasibility; never overrides authoritative config.
- **UNAVAILABLE**: cannot be defensibly sourced. Emit null/unknown/blocker rather than a guess.

Preserve source timestamps and current-regime boundaries. Historical evidence can calibrate/backtest but must not silently satisfy current-regime gates.
