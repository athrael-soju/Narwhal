# Troubleshoot a fleet

## Capture router and engine state

Collect one bundle per incident router, with:

- a fresh output path
- that router's fleet configuration
- that router's run directory

Collect a bundle:

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://router:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/router-incident-001
```

On exit status `3`, inspect the source rows in the partial bundle's [manifest](Diagnostic-Bundles.md).

| Option | Adds |
| --- | --- |
| `--artifact PATH` | Ingress and supervisor status, engine boot logs, profiles, or deployment load results from outside the selected run |
| `--include-request-content` | Journal and completion content |

Manual capture for releases that predate `narwhal diagnostics collect`:

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

Local artifacts:

1. Filter them with site tooling per the [Content policy](Diagnostic-Bundles.md#content-policy).
2. Place them beside these snapshots.

Before stopping an engine, capture evidence per the [planned restart or unplanned-failure procedure](troubleshoot/01-Engine-Recovery.md).

## Router, admission, and lifecycle signals

| Symptom | Next check or action | Follow-up |
| --- | --- | --- |
| Request to `/health` fails | Query peer `/ready` to identify the lease holder. | Inspect the router process, host, and network path. |
| `/health` reports `standby` | Send traffic and lifecycle actions to the active lease holder. | |
| `/health` reports `fenced` | Identify the current lease holder. | Remove the fenced router from the load balancer. |
| `/health` reports `maintenance` | Follow `/narwhal/lifecycle` through the engine wave until readiness returns. | |
| Both routers return HTTP 503 from `/ready` | Compare refusal reasons. | Inspect backend health, lifecycle holds, engine monitoring, the lease holder, and state handoff freshness. |
| HTTP 429 increases | Separate `rejected`, `refused`, and queue-shed reasons before changing capacity. | |
| HTTP 502 increases | Inspect engine failures, ejection, quarantine, and in-flight work. | |
| HTTP 504 increases | Separate queue and request expiry from engine timeouts with the response error and terminal journal row. | |
| A stream ends with an error frame after the HTTP 200 response starts | Inspect failed attempts, final outcome, and participating engines. | |
| Lifecycle state is `blocked` | Repair the failed drain identity capture or readmission check. | Retry that operation. |

Procedures by path:

<div class="grid cards" markdown>

-   [Fleet overload with healthy engines](#fleet-overload-with-healthy-engines)

    ---

    HTTP 429 or 504 increases.

-   [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md)

    ---

    An engine failed unexpectedly, or the fleet needs a whole-wave restart.

-   [Router failover and rollback](troubleshoot/02-Router-Recovery.md)

    ---

    The primary router failed, or a standby reports a stale or incompatible state handoff.

</div>

## Fleet overload with healthy engines

1. Classify the overload from `admission`, `serving`, `resident`, and pool load in `/narwhal/state`:
    - immediate concurrency rejection
    - queue-full shedding
    - queue expiry
    - predictive refusal
2. Keep the request mix, `serving.max_connections`, queue depth, and timeouts fixed.
3. Test two offered rates under those conditions.
4. Compare completed throughput and the share of requests meeting the service-level objective.
5. Reduce ingress traffic, or add a fleet that passed deployment validation, before raising a limit.

| Ratio | Numerator | Denominator |
| --- | --- | --- |
| Load-test router outcome | Router completions | Admitted requests |
| [Deployment attainment](measure/04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal) | Client completions that meet the service-level objective | All scheduled offers, including cancellations, predictive refusals, and unsent scheduling misses |

## After recovery

Run the [release validation drills](operate/05-Release-Drills.md#11-validate-every-release).
