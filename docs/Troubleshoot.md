# Troubleshoot a fleet

Before changing a router or engine, capture the current control state. Keep one ignored evidence directory per router and incident, and collect the router’s private endpoints there:

```bash
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)

curl -sS http://router:8000/health > "$incident_dir/health.json"
curl -sS -o "$incident_dir/ready.json" -w '%{http_code}\n' http://router:8000/ready
curl -sS http://router:8000/narwhal/state > "$incident_dir/state.json"
curl -sS http://router:8000/narwhal/lifecycle > "$incident_dir/lifecycle.json"
curl -sS http://router:8000/metrics > "$incident_dir/metrics.txt"
```

Put the router journal, ingress and supervisor status, engine boot logs, fleet config, profiles, and deployment load results in the same incident directory. Do not restart an engine until you have followed either its lifecycle contract or the unplanned-failure procedure.

## Start with the first useful signal

| Symptom                                    | What it tells you                                       | Next action                                                                                                                |
| ------------------------------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `/health` is unreachable                   | The router or its host is unavailable                   | Check `/ready` on the other router and identify the lease holder                                                           |
| `/health` reports `standby`                | The router is alive but does not control the fleet      | Do not send traffic or lifecycle actions to it                                                                             |
| `/health` reports `fenced`                 | Lease ownership has moved elsewhere                     | Find the current lease holder and keep this router out of the load balancer                                                |
| `/health` reports `maintenance`            | An engine-wide maintenance wave is active               | Follow `/narwhal/lifecycle` until readiness returns                                                                        |
| Both routers return HTTP 503 from `/ready` | Neither router is accepting traffic                     | Read the refusal reasons, then inspect backend health, lifecycle holds, monitoring, lease ownership, and handoff freshness |
| HTTP 429 increases                         | Admission control is rejecting or shedding more work    | Split the failures by `rejected`, `refused`, and queue-shed reason before changing capacity                                |
| HTTP 502 or 504 increases                  | An engine failed or exceeded its timeout                | Check engine health, ejection, quarantine, and in-flight work                                                              |
| A stream finishes with an error frame      | Failure occurred after the HTTP 200 response started    | Inspect failed attempts, the final outcome, and the engines involved                                                       |
| Lifecycle state is `blocked`               | Drain identity capture or readmission validation failed | Repair the failed check, then retry the failed drain or readmission operation                                              |

## Healthy engines, overloaded fleet

Read `admission`, `serving`, `resident`, and pool load from `/narwhal/state`. Classify overload before changing limits: immediate concurrency rejection, queue-full shedding, queue expiry, and predictive refusal are different failure modes.

The router and client report attainment over different populations. The router journal divides completions by admitted requests. The client divides successful requests by all offered requests, including requests rejected predictively. Compare both denominators when reconciling load-test results.

Reduce offered traffic at ingress, or bring up another fleet that has already been validated. Do not change `serving.max_connections`, queue depth, or timeouts until a controlled two-point measurement establishes the scaling direction.

Longer queues and timeouts can convert an immediate refusal into a late SLO miss. Keep shedding offered traffic at the current settings until the measurement justifies a different configuration.

## Unplanned failure of one engine

Confirm that the failed engine is ejected or quarantined and that surviving engines are still receiving work. Preserve the engine boot log and the supervisor’s exit reason before restarting anything.

Read `recovery.engine_restart_policy`.

For `individual`, bring the engine back far enough to verify its HTTP endpoints, then restart its sidecar through the configured process manager so the sidecar binds the new process identity. Inspect the sidecar log and repair any named endpoint or contract failure.

For `whole_wave`, use the complete-wave recovery procedure.

Watch `/narwhal/lifecycle` during recovery. A contracted fleet runs the health, attestation, model, generation, role-permitted KV, and final-health gates automatically. When they pass, verify that the engine reports `accepts_new: true` and that its ejection has cleared.

If individual validation enters `blocked`, repair the engine and call `/narwhal/lifecycle/readmit`. The ejection clears only after every recovery gate passes. Fleets configured for `whole_wave` require complete-wave readmission.

## Planned restart of one engine

Use the [drain sequence](Operate.md#restart-one-engine). The external supervisor may stop the process only after `ready_to_stop: true`, which confirms that new placement has stopped and resident work has drained.

On readmission, require a newer process identity and rerun the recovery gates.

## NIXL peer failure or mixed engine generation

A stale-peer assertion, a transfer stall that kills a peer, or an engine-generation mismatch requires a whole-wave recovery.

Start a lifecycle whole-wave drain to stop new traffic. If drain identity capture fails for any engine, use the [unplanned whole-wave procedure](Operate.md#restart-an-engine-wave) to restore that endpoint, then retry the drain.

Wait until `wave.ready_to_stop: true` and the router returns HTTP 503 from `/ready`. Stop every engine process tree through the external supervisor. Before restarting, verify that accelerator memory belongs only to the current worker.

Launch every engine from the same immutable image and launch contract. Start a fresh attestation sidecar for each engine process, then submit whole-wave readmission.

Do not restore ingress until the KV ring and final-health checks pass and `/ready` returns HTTP 200.

## Primary router failure

Check the private `/ready` endpoint on both routers. Exactly one lifecycle document should report `router.controls_fleet: true`, and the load balancer should select only that router.

Verify that the active router’s lease epoch is greater than the failed primary’s epoch. Its roles and cumulative counters must also match the last handoff.

Restart the old primary as a standby with `--standby-of` pointing at the active router. The recovered process should return HTTP 503 from `/ready` and reject a direct completion request.

If both routers return 503, read their refusal reasons before attempting takeover. Repair lease storage or clock-bound failures where reported. Keep lease ownership and fencing intact while restoring a single active primary.

## Stale or incompatible handoff

The standby reports `no fresh handoff` when the previous lease epoch’s handoff has expired or when the previous router exited before persisting one. A contract-version mismatch or incorrect epoch also keeps `/ready` at HTTP 503 until a compatible handoff is available.

Keep client traffic stopped. Restore a compatible router release and state set. If that is not possible, start one router from its configured opening roles during a maintenance window.

Before admitting traffic, verify that the old router process is stopped or fenced.

## Router rollback

Remove the target router from the load balancer. Stop it cleanly where possible so it writes a complete handoff.

Check the rollback build’s contract manifest:

```bash
narwhal-check --print-contract-versions
```

Restore the config, profiles, and a handoff version that the rollback build can read. Verify that fleet control belongs either to this router or to its fenced HA peer.

Start the rollback build. Check health, readiness, roles, cumulative counters, and one completion before restoring service. Bring its standby back only after the active router is stable.

Older builds can load handoffs that declare a supported schema version. To start from configured opening roles and reset cumulative counters, set:

```yaml
recovery:
  resume: false
```

## Required drills

Run the [release and production drills](Operate.md#required-drills) after repair. They exercise the deployment’s lifecycle and failover paths.