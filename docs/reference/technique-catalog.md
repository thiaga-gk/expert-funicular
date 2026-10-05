# Compute optimization technique catalog

Stable `technique_id` values are catalog identities. They do not change across warehouses/runs; `candidate_id` identifies the warehouse/epoch-specific decision instance.

| Knob | Stable technique IDs | Algorithm | Primary blocker / validation |
|---|---|---|---|
| ~~channel~~ (retired) | ~~`WH-CHANNEL-CURRENT-001`~~, ~~`WH-CHANNEL-STABILITY-001`~~ reserved, not emitted | observation only: the channel is reported in `current_config.warehouse_channel` and a `PREVIEW` warehouse carries an informational evidence note; no recommendation, because a channel move has no priced effect and Preview-only dependencies are not visible in system tables | n/a |
| ~~Photon~~ (retired in v2.7.1) | ~~`WH-PHOTON-ENABLE-001`~~ reserved, not emitted | observation only: ENABLED/DISABLED/MIXED/UNKNOWN is still inferred from current-epoch signed DBUs and reported in `current_config.photon`; no recommendation is made because Databricks publishes no Photon on/off price for SQL warehouses | n/a |
| size | `WH-SIZE-DOWNSIZE-001`, `WH-SIZE-UPSIZE-001`, `WH-SIZE-RIGHTSIZE-001` | spill/latency/queue right-sizing (a batch warehouse is judged on run days; spill concentrated in a few statements is a rewrite, not an upsize); type-correct DBU/AWS model | A/B performance validation for downsize and upsize |
| max clusters | `WH-MAX-CLUSTER-REDUCE-001`, `WH-MAX-CLUSTER-INCREASE-001`, `WH-MAX-CLUSTER-ASSESS-001` | concurrency ceiling tuning: Pro/Classic ceiling from peak query concurrency at the documented queries per cluster (one move up or down, a seasonal peak and an older epoch's higher peak hold a cut) | max limit / insufficient pressure / insufficient concurrency evidence |
| min clusters | `WH-MIN-CLUSTER-REDUCE-001`, `WH-MIN-CLUSTER-INCREASE-001`, `WH-MIN-CLUSTER-ASSESS-001` | floor from the demand busy periods start with (scale-up lag); lowering only | queue/scale pressure / insufficient busy periods |
| auto-stop | `WH-AUTOSTOP-REDUCE-001`, `WH-AUTOSTOP-ASSESS-001` | cost-minimizing auto-stop value on running idle time (idle gaps intersected with running time, net of restarts added or avoided); a raise is a canary | cold-start budget / restart billing unverified / insufficient running-idle evidence |
| warehouse type | `WH-SERVERLESS-MIGRATE-001`, `WH-PRO-MIGRATE-001`, `WH-WAREHOUSE-TYPE-ASSESS-001` | bidirectional type migration canary, target priced at its own best auto-stop; held when it never saves | compatibility unknowns + canary required |

Every candidate references one catalog technique, content-addressed evidence, deterministic confidence and independent economics. Candidate savings are never added to form package savings; only stateful scenario economics feed the package opportunity envelope.
