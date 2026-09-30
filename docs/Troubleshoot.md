# Troubleshoot a fleet

## Capture state first

Collect evidence before you change anything on a router or engine. Once a process restarts, the state that explains the failure is usually gone.

Collect one bundle per router involved in the incident. Give each bundle its own output path, and point `--fleet` and `--run` at that router's configuration and run directory:

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://router:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/router-incident-001
```

The bundle keeps each response and file it reads. Its [manifest](Diagnostic-Bundles.md) records, for each source, the path or URL, the outcome and any error, the HTTP status for router reads, and the exported file's hash.

Exit code `3` means the bundle is partial: at least one source returned an HTTP error, failed, timed out, hit the byte limit, or was excluded. That's common during an incident, because a router that isn't ready returns HTTP 503 from `/ready`, and that alone makes the bundle partial. Check each source's outcome in the manifest before you retry any reads.

Two options you may need:

- `--artifact PATH` adds files from outside the selected run, such as ingress and supervisor status, engine boot logs, profiles, or deployment load results.
- `--include-request-content` adds journal and completion content. Leave it off unless the incident actually needs request content.

### Installations without the collector

If your installation doesn't have `narwhal diagnostics collect` yet, capture the endpoints by hand. This loop caps each request at five seconds and writes to a directory only you can read:

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

You get a `.body` and a `.status` file per endpoint. Copy any local files you would have passed with `--artifact` into the same directory, filtered according to your site's rules.

If you're about to stop an engine, save what its recovery procedure asks for too. For an unexpected failure, that means the engine boot log and the supervisor's exit reason (see [Engine failure](troubleshoot/01-Engine-Recovery.md#engine-failure)).

## Reading the signals

| What you see                               | What to do                                                                                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `/health` doesn't respond                  | Ask the peer router's `/ready` who holds the lease. Then look at the failed router's process, host, and network path.                           |
| `/health` reports `standby`                | This router isn't serving. Send traffic and lifecycle actions to the lease holder.                                                              |
| `/health` reports `fenced`                 | Find the current lease holder and take the fenced router out of the load balancer.                                                              |
| `/health` reports `maintenance`            | An engine wave is in progress. Follow `/narwhal/lifecycle` until readiness comes back.                                                          |
| Both routers return HTTP 503 from `/ready` | Compare the refusal reasons first. Then check backend health, lifecycle holds, monitoring, lease ownership, and handoff freshness.              |
| More HTTP 429s                             | Break them down by reason (`rejected`, `refused`, queue shed) before touching capacity. Each one points to a different cause.                   |
| More HTTP 502s                             | Look at engine failures, ejections, quarantines, and in-flight work.                                                                            |
| More HTTP 504s                             | Use the response error and the terminal journal row to tell queue or request expiry apart from engine timeouts.                                 |
| A stream ends with an error frame          | The client already got HTTP 200, so the failure happened mid-stream. Check failed attempts, the final outcome, and which engines were involved. |
| Lifecycle state is `blocked`               | Either the drain couldn't capture an engine's identity or a readmission check failed. Fix whichever it was and retry that operation.            |

Where to go next:

- [Fleet overload with healthy engines](#fleet-overload-with-healthy-engines)
- [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md)
- [Router failover and rollback](troubleshoot/02-Router-Recovery.md)

## Fleet overload with healthy engines

If the engines are healthy and the fleet is still turning work away, start with `/narwhal/state`. The `admission`, `serving`, `resident`, and pool load fields show which of these is happening:

- requests rejected immediately at the concurrency limit
- requests shed because the queue is full
- requests expiring while they wait in the queue
- requests refused up front by predictive admission

Don't raise a limit until you know it helps. Run the same request mix at two offered rates, keeping `serving.max_connections`, queue depth, and timeouts fixed, and compare completed throughput and SLO-qualified requests. A higher limit can just let work sit in the queue past its TTFT budget. You'd admit more requests and complete fewer of them in time.

If the numbers don't support a higher limit, cut offered traffic at ingress or add a fleet whose deployment has passed validation.

When you reconcile load-test results, keep two ratios apart:

- **Router completion rate:** router completions divided by admitted requests.
- **[Deployment attainment](measure/04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal):** SLO-qualified client completions divided by every scheduled offer, including cancellations, predictive refusals, and scheduling misses that were never sent.

Attainment is almost always the lower number, because its denominator includes work the router never saw.

## After recovery

Once the fleet is serving again, run the [release and production drills](operate/04-Upgrade-and-Validate.md#11-validate-every-release).
