# Troubleshoot a Fleet

## Capture router and engine state

Before changing router or engine state, create a separate ignored evidence directory for each router in the incident and capture its control endpoints.

```bash
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)

curl -sS http://router:8000/health > "$incident_dir/health.json"
curl -sS -o "$incident_dir/ready.json" -w '%{http_code}\n' http://router:8000/ready
curl -sS http://router:8000/narwhal/state > "$incident_dir/state.json"
curl -sS http://router:8000/narwhal/lifecycle > "$incident_dir/lifecycle.json"
curl -sS http://router:8000/metrics > "$incident_dir/metrics.txt"
```

Store the router journal, ingress and supervisor status, engine boot logs, fleet configuration, profiles, and deployment load results beside the endpoint snapshots. Before stopping an engine, capture the evidence required by its planned lifecycle restart or unplanned-failure procedure.

## Router, admission, and lifecycle signals

| Signal                                     | Next check or action                                                                                                       |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Request to `/health` fails                 | Query peer `/ready` to identify the lease holder; inspect the router process, host, and network path.                      |
| `/health` reports `standby`                | Send traffic and lifecycle actions to the active lease holder.                                                             |
| `/health` reports `fenced`                 | Identify the current lease holder and remove the fenced router from the load balancer.                                     |
| `/health` reports `maintenance`            | Follow `/narwhal/lifecycle` through the engine wave until readiness returns.                                               |
| Both routers return HTTP 503 from `/ready` | Compare refusal reasons, then inspect backend health, lifecycle holds, monitoring, lease ownership, and handoff freshness. |
| HTTP 429 increases                         | Separate `rejected`, `refused`, and queue-shed reasons before changing capacity.                                           |
| HTTP 502 increases                         | Inspect engine failures, ejection, quarantine, and in-flight work.                                                         |
| HTTP 504 increases                         | Separate queue and request expiry from engine timeouts using the response error and terminal journal row.                  |
| A stream terminates with an error frame    | Inspect failed attempts, final outcome, and participating engines after the HTTP 200 response has started.                 |
| Lifecycle state is `blocked`               | Repair the failed drain identity capture or readmission check, then retry that operation.                                  |

Continue with the procedure for the affected path:

- [Fleet overload with healthy engines](#fleet-overload-with-healthy-engines)
- [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md)
- [Router failover and rollback](troubleshoot/02-Router-Recovery.md)

## Fleet overload with healthy engines

Read `admission`, `serving`, `resident`, and pool load from `/narwhal/state` to trace overload to immediate concurrency rejection, queue-full shedding, queue expiry, or predictive refusal.

When reconciling load-test attainment, divide router completions by admitted requests and client successes by all offered requests, including predictive refusals.

Reduce offered traffic at ingress or add a fleet whose deployment passed validation.

Run the same request mix at two offered rates with `serving.max_connections`, queue depth, and timeouts fixed, comparing completed throughput and SLO-qualified requests before raising a limit that could hold work past its TTFT budget.

## After recovery

Run the [release and production drills](operate/04-Upgrade-and-Validate.md#11-validate-every-release) after the repair.
