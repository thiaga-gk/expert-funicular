# Implementation Rules

- Use stable dataclasses/enums at decision boundaries.
- Keep source adapters isolated from decision contracts.
- Preserve `(workspace_id, resource_id...)` identity exactly; IDs are strings.
- Normalize timestamps/timezones deterministically.
- Do not log secrets, raw query text, or sensitive payloads unnecessarily.
- Keep DBU/cloud cost components separate.
- Keep unknown/unavailable distinct from zero.
- Keep confidence evidence-quality semantics separate from probability-of-savings claims.
- Recommended scenario policy must be explicit and deterministic.
- Realization records append measurement outcomes; they do not mutate original package identity.
- Use canonical serialization for IDs/hashes.
