---
description: Capture diagnostic bundles and recover a Narwhal fleet from router, admission and overload failures.
---

# Troubleshooting a fleet

## Capturing router and engine state

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

Add `--artifact PATH` to include ingress and supervisor status, engine boot logs, profiles, or deployment load results from outside the selected run. Add `--include-request-content` to include journal and completion content.

Manual capture for releases before 0.3.0:

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

Start from the router's health and readiness:

- If a request to `/health` fails, query the peer's `/ready` to identify the lease holder. Inspect the router process, host, and network path.
- If `/health` reports `standby`, send traffic and lifecycle actions to the active lease holder.
- If `/health` reports `fenced`, identify the current lease holder and remove the fenced router from the load balancer.
- If `/health` reports `maintenance`, follow `/narwhal/lifecycle` through the engine wave until readiness returns.
- If both routers return HTTP 503 from `/ready`, compare their refusal reasons. Inspect backend health, lifecycle holds, engine monitoring, the lease holder, and state handoff freshness.

For rising client errors:

- If HTTP 429 responses increase, separate `rejected`, `refused`, and queue-shed reasons before changing capacity.
- If HTTP 502 responses increase, inspect engine failures, ejection, quarantine, and in-flight work.
- If HTTP 504 responses increase, separate queue and request expiry from engine timeouts with the response error and terminal journal row.
- If a stream ends with an error frame after the HTTP 200 response starts, inspect the failed attempts, final outcome, and participating engines.

If the lifecycle state is `blocked`, repair the failed drain identity capture or readmission check, and retry that operation.

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

The load-test router outcome ratio divides router completions by admitted requests. [Deployment attainment](measure/04-Reconcile-and-Accept.md#10-joining-client-offers-to-the-router-journal) divides the client completions that meet the service-level objective by all scheduled offers, including cancellations, predictive refusals, and unsent scheduling misses.

## Validating recovery

Run the [release validation drills](operate/05-Release-Drills.md#11-validating-every-release).
