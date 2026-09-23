# Troubleshoot a Fleet

Use this procedure to diagnose router, engine, lifecycle, overload, failover, and rollback failures without destroying the state needed to explain them.

## Capture the control state first

Create one ignored evidence directory for each router and incident before changing router or engine state.

```bash
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)

curl -sS http://router:8000/health > "$incident_dir/health.json"
curl -sS -o "$incident_dir/ready.json" -w '%{http_code}\n' http://router:8000/ready
curl -sS http://router:8000/narwhal/state > "$incident_dir/state.json"
curl -sS http://router:8000/narwhal/lifecycle > "$incident_dir/lifecycle.json"
curl -sS http://router:8000/metrics > "$incident_dir/metrics.txt"
```

Add the router journal, ingress and supervisor status, engine boot logs, fleet configuration, profiles, and deployment load results to the same directory.

An engine restart must follow either its lifecycle contract or the unplanned-failure procedure. Preserve the evidence required by that path before stopping the process.

## Classify the failure from the control plane

Use the first available signal to choose the recovery path.

| Signal                                     | Control-plane state                                     | Action                                                                                                                      |
| ------------------------------------------ | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `/health` is unreachable                   | The router or its host is unavailable                   | Query `/ready` on the peer router and identify the lease holder                                                             |
| `/health` reports `standby`                | The router is alive without fleet control               | Keep traffic and lifecycle actions on the active router                                                                     |
| `/health` reports `fenced`                 | Lease ownership has moved to another router             | Find the current lease holder and keep the fenced router out of the load balancer                                           |
| `/health` reports `maintenance`            | An engine-wide maintenance wave is active               | Follow `/narwhal/lifecycle` until readiness returns                                                                         |
| Both routers return HTTP 503 from `/ready` | Neither router currently accepts traffic                | Read both refusal reasons, then inspect backend health, lifecycle holds, monitoring, lease ownership, and handoff freshness |
| HTTP 429 increases                         | Admission control is rejecting or shedding more work    | Split failures by `rejected`, `refused`, and queue-shed reason before changing capacity                                     |
| HTTP 502 or 504 increases                  | An engine failed or exceeded its timeout                | Inspect engine health, ejection, quarantine, and in-flight work                                                             |
| A stream terminates with an error frame    | Failure occurred after the HTTP 200 response started    | Inspect failed attempts, final outcome, and participating engines                                                           |
| Lifecycle state is `blocked`               | Drain identity capture or readmission validation failed | Repair the failed check, then retry the failed drain or readmission operation                                               |

Follow the matching procedure:

- [Fleet overload with healthy engines](#fleet-overload-with-healthy-engines)
- [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md)
- [Router failover and rollback](troubleshoot/02-Router-Recovery.md)

## Fleet overload with healthy engines

Read `admission`, `serving`, `resident`, and pool load from `/narwhal/state`.

Separate the overload mode before changing limits:

- immediate concurrency rejection;
- queue-full shedding;
- queue expiry;
- predictive refusal.

These modes fail at different points in admission and scheduling, so a single increase in capacity limits can move the failure later without improving attainment.

The router journal and the client report attainment over different populations. The router divides completions by admitted requests. The client divides successful requests by all offered requests, including predictively rejected requests. Use both denominators when reconciling load-test output.

Reduce offered traffic at ingress, or add a fleet whose deployment has already been validated.

Keep `serving.max_connections`, queue depth, and timeouts at their current values until a controlled two-point measurement establishes the scaling direction. Longer queues or timeouts can turn an immediate refusal into a late SLO miss.

## After recovery

Run the [release and production drills](operate/04-Upgrade-and-Validate.md#11-validate-every-release) after the repair.

Those drills exercise the deployment's lifecycle and failover paths under the repaired configuration.
