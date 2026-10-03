# Agent Patterns

## Advisor
Read-only. Research, challenge, compare evidence, and update design artifacts only when explicitly assigned artifact ownership. Never edit implementation code.

## Builder
May edit code/tests owned by the implementation stage. Build only from approved artifacts. Must not invent missing source, threshold, compatibility, or economic semantics.

## Verifier
Independent of the builder when possible. Challenge correctness, determinism, safety, economics, contracts, and E2E behavior. May create tests/evidence; findings return to builder.

## Orchestrator
Coordinates gates and artifacts; does not become the domain expert or silently resolve disagreements.

## Model routing
If model routing is available, use capable lower-cost models for parallel research/build loops and the strongest available model for final architecture/algorithm/code challenge. Keep model names configurable, not embedded into semantic artifacts.
