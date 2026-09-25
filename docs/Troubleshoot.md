# Troubleshoot a Fleet

## Capture router and engine state

Before changing router or engine state, collect a separate bundle for each router in the incident. Choose a fresh output path and select the fleet configuration and run directory that belong to that router.

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://router:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/router-incident-001
```

The [diagnostic bundle manifest](Diagnostic-Bundles.md) records HTTP status, retained response bodies, collection errors, source paths and artifact hashes. Exit `3` identifies a partial bundle; inspect its source rows before retrying individual reads. Add `--artifact PATH` for ingress and supervisor status, engine boot logs, profiles or deployment load results outside the selected run. Use `--include-request-content` when the incident requires journal or completion content.

For an installation awaiting the collector command, capture the endpoints with bounded curl requests into a private directory:

```bash
umask 077
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)

for endpoint in health ready narwhal/state narwhal/lifecycle metrics; do
  name=${endpoint##*/}
  curl --connect-timeout 2 --max-time 5 -sS \
    -o "$incident_dir/$name.body" -w '%{http_code}\n' \
    "http://router:8000/$endpoint" > "$incident_dir/$name.status"
done
```

Place site-filtered local artifacts beside these manual snapshots. Before stopping an engine, capture the evidence required by its planned lifecycle restart or unplanned-failure procedure.

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

Run the same request mix at two offered rates. Keep `serving.max_connections`, queue depth, and timeouts fixed, then compare completed throughput and SLO-qualified requests. Use that comparison before raising a limit that could hold work past its TTFT budget.

## After recovery

Run the [release and production drills](operate/04-Upgrade-and-Validate.md#11-validate-every-release) after the repair.
