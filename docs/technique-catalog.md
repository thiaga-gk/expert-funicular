# Compute optimization technique catalog — v2.7.0

Stable `technique_id` values are catalog identities. They do not change across warehouses/runs; `candidate_id` identifies the warehouse/epoch-specific decision instance.

| Knob | Stable technique IDs | Algorithm | Primary blocker / validation |
|---|---|---|---|
| channel | `WH-CHANNEL-CURRENT-001`, `WH-CHANNEL-STABILITY-001` | release-channel stability review | preview dependency unknown |
| Photon | `WH-PHOTON-ENABLE-001` | infer ENABLED/DISABLED/MIXED/UNKNOWN from current-epoch signed DBUs; canary enablement when reliably disabled | mixed/stale/sparse billing evidence; A/B validation |
| size | `WH-SIZE-DOWNSIZE-001`, `WH-SIZE-UPSIZE-001`, `WH-SIZE-RIGHTSIZE-001` | spill/latency/queue right-sizing; type-correct DBU/AWS model | A/B performance validation for downsize |
| max clusters | `WH-MAX-CLUSTER-REDUCE-001`, `WH-MAX-CLUSTER-INCREASE-001`, `WH-MAX-CLUSTER-ASSESS-001` | concurrency ceiling tuning | max limit / insufficient pressure |
| min clusters | `WH-MIN-CLUSTER-REDUCE-001`, `WH-MIN-CLUSTER-INCREASE-001`, `WH-MIN-CLUSTER-ASSESS-001` | remove unnecessary baseline capacity | queue/scale pressure |
| auto-stop | `WH-AUTOSTOP-REDUCE-001`, `WH-AUTOSTOP-ASSESS-001` | inter-query idle-gap model | cold-start sensitivity / insufficient gaps |
| warehouse type | `WH-SERVERLESS-MIGRATE-001`, `WH-PRO-MIGRATE-001`, `WH-WAREHOUSE-TYPE-ASSESS-001` | bidirectional type migration canary | compatibility unknowns + canary required |

Every candidate references one catalog technique, content-addressed evidence, deterministic confidence and independent economics. Candidate savings are never added to form package savings; only stateful scenario economics feed the package opportunity envelope.
