# Generic Resource-Oriented Reporter Contract

## Top level
- `recommendation_package_id`
- `contract_version`
- `resource`
- `current_config`
- `workload_profile`
- `cost_baseline`
- `assessment`
- `evidence`
- `candidates`
- `scenarios`
- `recommended_plan`
- `package_economics`
- `realization`
- `provenance`

## Resource
Use service-neutral identity keys plus service-specific attributes:
```json
{
  "resource": {
    "resource_type": "JOB_CLUSTER",
    "workspace_id": "...",
    "resource_id": "...",
    "display_name": "...",
    "attributes": {}
  }
}
```
Do not copy `warehouse_id`, `warehouse_size`, etc. into the reusable base contract. A service adapter may expose those under `attributes` or a service extension.

## Evidence
Define once, reference many. Candidates/scenarios carry evidence IDs, not duplicated evidence payloads.

## Candidate vs technique
`technique_id` is stable catalog identity. `candidate_id` is the content-addressed resource/regime-specific decision instance.
