# Troubleshoot a fleet

Preserve the control state before changing a router or engine. Create a separate ignored evidence directory for each router and incident, and collect from its private address:

```bash
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)
curl -sS http://router:8000/health > "$incident_dir/health.json"
curl -sS -o "$incident_dir/ready.json" -w '%{http_code}\n' http://router:8000/ready
curl -sS http://router:8000/narwhal/state > "$incident_dir/state.json"
curl -sS http://router:8000/narwhal/lifecycle > "$incident_dir/lifecycle.json"
curl -sS http://router:8000/metrics > "$incident_dir/metrics.txt"
```

Retain the router journal, ingress status, supervisor status, engine boot logs, fleet config, profiles and deployment load results with those files. Follow the lifecycle contract or the unplanned-failure procedure below before restarting an engine.

## Read the first signal

| Symptom | Meaning | Action |
|---|---|---|
| `/health` is unreachable | The router or host is down | Check the other router's `/ready` and see who holds the lease |
| `/health` is `standby` | The router is up as a standby | Keep traffic and lifecycle actions off it |
| `/health` is `fenced` | Lease ownership moved to another router | Find the current lease holder and leave this router out of the load balancer |
| `/health` is `maintenance` | An engine-wide maintenance wave is running | Follow `/narwhal/lifecycle` and wait for readiness to return |
| `/ready` is 503 on both routers | Both routers are refusing traffic | Check the refusal reasons first, then backend health, lifecycle holds, monitoring, lease ownership, and handoff freshness |
| HTTP 429 rises | More requests are being refused or shed | Break down `rejected`, `refused`, and queue-shed reasons before adding capacity |
| HTTP 502 or 504 rises | An engine failed or timed out | Check engine health, ejection, quarantine, and work still in flight |
| Stream ends with an error frame | The stream failed after the 200 response had started | Check the failed attempts, final outcome, and engines involved |
| Lifecycle state is `blocked` | Drain identity capture or readmission validation failed | Fix the failed check, then retry the action that failed: drain or readmission |

## Overload with healthy engines

1. Read `admission`, `serving`, `resident`, and pool load from `/narwhal/state`.
2. Separate immediate concurrency rejection, queue-full shed, queue expiry, and predictive refusal.
3. Compare both attainment denominators: the router journal divides completions by admitted requests, while the client divides success by every offered request, including predictive refusals.
4. Reduce offered traffic at ingress or add a separately validated fleet.
5. Change `serving.max_connections`, queue depth, or timeouts after a controlled two-point measurement confirms the direction.

A longer queue or timeout can turn an immediate refusal into a late SLO miss, so shed offered traffic at the current settings until the two-point measurement confirms a new direction.

## One unplanned engine failure

1. Confirm the engine is ejected or quarantined and that other engines still receive work.
2. Preserve its boot log and supervisor exit reason.
3. Check `recovery.engine_restart_policy`. With `individual`, verify the recovered engine's HTTP endpoints, then restart its sidecar through the configured process manager to bind the new identity. Inspect the sidecar log and repair any named endpoint or contract failure. With `whole_wave`, use the complete-wave procedure below.
4. Watch `/narwhal/lifecycle`. A contracted fleet automatically runs health, attestation, model, generation, role-permitted KV, and final-health gates.
5. If validation passes, confirm the engine returns to `accepts_new: true` and the ejection clears.
6. If individual validation fails, repair the `blocked` engine and call `/narwhal/lifecycle/readmit`; readmission clears its ejection after every recovery gate passes. A `whole_wave` fleet requires complete-wave readmission.

## One planned engine restart

Use the [drain sequence](Operate.md#restart-one-engine), and let the external supervisor stop the process after `ready_to_stop: true` confirms that placement has stopped and resident work has drained; readmission then proves a newer process identity alongside the recovery gates.

## NIXL peer failure or mixed engine generation

Treat a stale-peer assertion, transfer stall that kills a peer, or generation mismatch as a whole-wave event.

1. Stop new traffic with a lifecycle whole-wave drain. If drain identity capture fails for an engine, follow the [unplanned whole-wave procedure](Operate.md#restart-an-engine-wave) to restore the endpoint and retry the drain.
2. Wait for `wave.ready_to_stop: true` and router `/ready` HTTP 503.
3. Stop the full engine process trees through the external supervisor.
4. Verify accelerator memory belongs only to the current worker.
5. Start every engine from one immutable image and launch contract.
6. Start a new attestation sidecar for every engine process.
7. Submit whole-wave readmission.
8. Return ingress traffic only after the KV ring and final health checks pass and `/ready` returns HTTP 200.

## Primary router failure

1. Check both private `/ready` routes.
2. Confirm one lifecycle document reports `router.controls_fleet: true`.
3. Confirm the load balancer selects only that router.
4. Verify its lease epoch is greater than the failed primary's epoch and that roles and cumulative counters match the last handoff.
5. Restart the old primary as standby, pointing `--standby-of` at the current active router.
6. Confirm the recovered process returns HTTP 503 from `/ready` and refuses a direct completion request.

If both routers return 503, inspect their refusal reasons before attempting takeover. Repair lease storage or clock bounds when those checks fail. Preserve the lease and fencing while restoring one active primary.

## Stale or incompatible handoff

The standby raises `no fresh handoff` when the preceding lease epoch's handoff has expired or that router ended before persisting one. A contract-version mismatch or wrong epoch holds `/ready` at HTTP 503 until a matching handoff arrives.

Keep client traffic stopped. Restore the compatible router release and state set, or start one router from the configured opening roles during a maintenance window. Confirm the old process is stopped or fenced before admitting traffic.

## Router rollback

1. Withdraw the target router from the load balancer.
2. Stop it cleanly when possible so its handoff is complete.
3. Check the rollback build's contract manifest with `narwhal-check --print-contract-versions`.
4. Restore the matching config, profiles and a handoff version that build reads.
5. Confirm that fleet control belongs to this router or its fenced HA peer.
6. Start the rollback build and verify health, readiness, roles, counters, and one completion.
7. Restore its standby only after the active router is stable.

Older builds load handoffs that declare a supported schema version. To start from configured opening roles and reset cumulative counters, set `recovery.resume: false`.

## Required drills

Use the [release and production drills](Operate.md#required-drills) to check the repaired deployment's lifecycle and failover paths.
