# Operate Narwhal

Each model fleet is controlled by one router at a time. A shared lease selects the active router and fences its HA peer as standby.

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Ownership

| Component         | Responsibility                                                                                                          |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Public ingress    | TLS, client authentication, WAF policy, body limits, public rate limits, model routing, and streaming proxy settings.   |
| Load balancer     | Poll `/ready` and send traffic to the router returning HTTP 200.                                                        |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, and readmission checks. |
| Engine supervisor | Engine and attestation-sidecar start/stop, resource limits, restart policy, and log retention.                          |
| Shared storage    | Provide one coherent lease domain to both router hosts.                                                                 |
| Monitoring        | Scrape metrics, retain journals, and page according to site policy.                                                     |

The deployment platform provisions GPUs and containers.

## Keep one deployment set

Keep the [deployment evidence set](Measure.md#2-validate-the-deployment-under-load) under one release identifier. Install the corresponding Narwhal release, fleet configuration, and profile store on both routers in the HA pair.

Both routers must implement compatible handoff contracts. Check the installed build before upgrading:

```bash
narwhal-check --print-contract-versions
```

## Configure ingress

Expose only the completion routes clients require. Keep `/narwhal/*`, `/metrics`, engine APIs, attestation endpoints, `/health`, and `/ready` on the private network.

Ingress is responsible for:

- client authentication and public rate limits;
- model-to-router-pair routing;
- forwarding streaming chunks as they arrive;
- connect and idle timeouts derived from the service budget;
- removing client-supplied internal credentials and request IDs before inserting trusted replacements.

Ingress terminates client credentials and applies identity policy. Narwhal forwards the trusted request ID for correlation. Each engine leg receives its own attempt- and phase-specific ID plus the credential named by `engine.engine_api_key_env`.

## Start the production routers

Run the final preflight first. Start both routers from the same fleet configuration and profile store. Once the active router reports ready, send the deployment workload through the intended ingress before opening client admission.

Start the first router:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Start its peer from the same release, configuration, and profiles:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

Both hosts must share a lease domain that supports POSIX `flock`, reads, and atomic rename. Keep the clock offset between hosts below `--lease-safety-margin`. Set the lease TTL above the renewal interval plus that margin.

The load balancer should route from `/ready`. A router returns HTTP 200 only while it owns a valid lease and is admitting traffic. The shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg) is the reference configuration.

During a partition, the lease holder fences itself before its local lease deadline. The standby may claim the lease after expiry. If shared storage becomes inaccessible, both routers withdraw readiness.

Shutdown state depends on role. A standby or fenced router keeps its saved primary handoff. An active lease holder persists its latest counters before releasing control.

## Monitor the fleet

Use [Set up observability](Observability.md) to generate engine targets, start Prometheus and Grafana, verify collection, and open the dashboard through an SSH tunnel.

`/health` reports router-process health through backend outages. It returns HTTP 200, with the configured fleet size in `instances` and the number of placement-eligible engines in `available_instances` after ejection, drain, and quarantine exclusions.

If no engine remains eligible for placement, an active router reports `status: degraded`. `/ready` returns HTTP 503 with `reason: no available engines`, and new completion requests receive a retryable `backend_unavailable` refusal.

A whole-wave lifecycle hold overrides that state. The router reports `status: maintenance` with the lifecycle reason and returns completion code `standby` at HTTP 503.

Performance-drift and temporary-quarantine holds keep the last eligible engine available. Failed health or inference probes may still exclude it.

During backend outages and managed maintenance, the router keeps control. A whole-wave hold pauses background monitoring until readmission. Standbys use `control_ready` to copy current handoffs. Client load balancers route according to HTTP status.

Run both routers on the same release. A replacement must apply the same lifecycle rules and preserve saved ejections until recovery succeeds.

Choose the router and engine scopes from the [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard), then inspect these groups:

| Signal                                         | Read                                                 |
| ---------------------------------------------- | ---------------------------------------------------- |
| Readiness and lease epoch                      | Which router owns admission.                         |
| Pool size and normalized load                  | Whether either phase has reached its measured limit. |
| Queue depth, pressure, and sheds               | Whether overload is waiting, protected, or refused.  |
| Ejection, probation, and role floors           | Remaining healthy capacity.                          |
| TTFT, TPOT, queue wait, and seat time          | Where client latency is spent.                       |
| Retries, failures, refusals, and rejections    | Which protection path is active.                     |
| Role changes, reversals, and blocked decisions | Whether the controller is stable.                    |

Set router-down, engine-down, error-burst, unserved, ejection, and role-floor thresholds in `tools/prometheus-alerts.yml` for the deployment before enabling paging.

Use request journals for per-request placement and timing. Use metrics for process-level summaries, subject to counter resets and state-retention windows.

## Restart one engine

With `recovery.engine_restart_policy: individual`, remove the engine from new placement before its supervisor changes the process.

Drain it:

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}'
```

Poll `/narwhal/lifecycle` until `engines.e0.ready_to_stop` becomes `true`. If the deadline expires first, the engine remains excluded and its resident work is preserved.

Restart the engine and attestation sidecar through their configured process manager. Check the engine HTTP endpoints and verify sidecar attestation against the new process identity. Then request readmission:

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}'
```

Readmission runs these gates before returning the engine to placement:

- health;
- current process-bound attestation;
- a process identity newer than the recorded drain identity;
- configured model identity;
- direct generation;
- role-permitted KV transfer;
- final health.

A failed gate leaves the engine blocked. The API returns HTTP 409 and names the failed gate.

Recovery after an unplanned breaker ejection uses the same validation, except the current process identity is accepted. A passing engine returns automatically. A failed gate holds it for operator repair and explicit readmission.

If a contracted fleet loses every placement peer, automatic recovery waits for the complete cohort and validates the cohort atomically. Any failed member keeps the cohort held. Repair the failing check, then request:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

If an individual lifecycle hold removes the last available peer, promote the hold to a wave:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

Then use the [complete-wave procedure](#restart-an-engine-wave).

## Restart an engine wave

Use `recovery.engine_restart_policy: whole_wave` when the engine build shares peer state across the fleet. Under this policy, drains and readmission operate on the complete wave.

A confirmed ejection, changed process identity, or failed identity verification places the whole fleet on hold and withdraws readiness until an operator completes wave readmission.

Start the drain:

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}'
```

Wait for the router to withdraw readiness and for `wave.ready_to_stop` to become `true`. Restart every engine and attestation sidecar from one immutable build, then request readmission:

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"wave":true}'
```

Narwhal returns the fleet in one state transition after all recorded drain identities have been superseded, every validation gate passes, and the role-permitted KV ring completes. One failed member keeps the entire wave excluded.

An unplanned whole-wave hold needs an explicit drain before restart so Narwhal can record current process identities. If an engine is already stopped and identity collection fails, start that engine under the fleet hold and retry the drain. Once `wave.ready_to_stop` is `true`, restart every engine and sidecar, including any process started only for identity collection, then request whole-wave readmission.

After each successful liveness sample, contracted fleets compare `/version`, `process_start_time_seconds`, and process-bound attestation with the accepted identity. A difference triggers recovery.

The handoff stores accepted process starts and restart policy. Resume and standby takeover use that state to check each running engine before admission. A version mismatch during contracted resume either holds the fleet for a managed wave or fails startup. Automatic takeover requires handoff schema version 1.

A changed process start is detected on the next successful liveness sample, normally after:

```text
recovery.liveness_every * controller.monitor_interval_s
```

plus probe and monitoring time.

Whole-wave policy relies on those sweeps to detect process replacement. Planned restarts therefore need to use the drain procedure, with accurate process-start metrics and attestation.

## Upgrade and rollback

For compatible handoff versions:

1. Stop the standby.
2. Install the new release and its matching deployment set on that host.
3. Start it as standby. `/health` must return 200 and `/ready` must return 503.
4. Stop the old active router cleanly.
5. Confirm that the upgraded router owns a higher lease epoch and is the only ready backend.
6. Upgrade the stopped router and return it as standby.

A handoff-version mismatch requires a maintenance window. Stop ingress and both routers, install one coherent deployment set, then start the active router and its standby.

To roll back, stop the new process before the old build can claim the fleet. Restore code, configuration, profiles, and a state version supported by that build as one unit.

## Required drills

Run the three CPU process drills against every release artifact:

```bash
make integration
```

`make integration` starts local stub engines and routers for individual and whole-wave lifecycle recovery. Its HA drill kills the primary during a stream, verifies standby takeover, and checks that the recovered primary remains fenced.

Before production, repeat the permitted engine-restart procedure and router failover on an idle fleet using the real supervisor and load balancer.

| Drill                     | Pass condition                                                                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | Placement stops after drain, resident work reaches zero, and every readmission gate passes.                                                                                           |
| Whole-wave restart        | Every action follows the wave policy, readiness is withdrawn before stop, and the fleet is readmitted only after every restarted engine passes attestation and the KV ring completes. |
| Router failover           | The load balancer selects one lease owner; roles and cumulative counters survive; the previous primary remains fenced.                                                                |

Use the [troubleshooting guide](Troubleshoot.md) for failure procedures. [Measure a fleet](Measure.md) defines the production evidence.