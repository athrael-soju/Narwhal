# Operate Narwhal

A shared lease grants one router control of each model fleet and fences its HA peer as standby.

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
| Load balancer     | Poll `/ready` and send traffic to the HTTP 200 router.                                                                  |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, and readmission checks. |
| Engine supervisor | Start, stop, resource limits, restart policy, and log retention for engines and attestation sidecars.                   |
| Shared storage    | One coherent lease domain for both router hosts.                                                                        |
| Monitoring        | Scrape metrics, retain journals, and page according to site policy.                                                     |

The deployment platform provisions GPUs and containers.

## Keep a deployment set

Store the [deployment evidence set](Measure.md#2-validate-the-deployment-under-load) under one release identifier. Install its Narwhal release, fleet config and profile store on both routers in an HA pair.

Both routers in an HA pair must use compatible handoff contracts. Check each installed build before an upgrade.

```bash
narwhal-check --print-contract-versions
```

## Configure ingress

Expose the completion routes required by clients, and keep `/narwhal/*`, `/metrics`, engine APIs, attestation endpoints, `/health` and `/ready` on the private network.

Ingress must:

- authenticate clients and apply public rate limits;
- route each model to its dedicated router pair;
- forward each streaming response chunk as it arrives;
- set connect and idle timeouts from the service budget;
- strip client-supplied internal credentials and request IDs before setting trusted replacements.

Ingress terminates client credentials and applies identity policy, then Narwhal forwards the trusted request ID for correlation while assigning an attempt- and phase-specific ID plus the credential from `engine.engine_api_key_env` to each engine leg.

## Start production routers

Run final preflight, start both routers from the same fleet configuration and profile store, then pass the deployment workload through intended ingress after the active router becomes ready and before client admission opens.

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Start the standby with the same release, configuration and profiles.

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

Place both hosts in one lease domain supporting POSIX `flock`, reads and atomic rename, keep their clock offset below `--lease-safety-margin` and set TTL above the renewal interval plus that margin.

Route load-balancer traffic with `/ready`, which returns HTTP 200 while the process owns a valid lease and admits traffic, starting from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg).

During a partition, the current holder fences itself before its local lease deadline, after which the standby can claim the expired lease; loss of storage access withdraws readiness from both hosts.

On shutdown, a standby or fenced router retains its saved primary handoff, while an active lease holder persists its latest counters before releasing control.

## Monitor the fleet

Follow [Set up observability](Observability.md) to generate engine targets, start Prometheus and Grafana, verify collection and open the dashboard through an SSH tunnel.

`/health` probes the router process through backend outages, returning HTTP 200 with the configured fleet size in `instances` and placement-eligible engines in `available_instances` after ejection, drain and quarantine exclusions.

When every engine is excluded from placement, an active router reports `status: degraded`, `/ready` returns HTTP 503 with `reason: no available engines` and new completion requests receive a retryable `backend_unavailable` refusal. A whole-wave lifecycle hold takes precedence, reports `status: maintenance` with its lifecycle reason and returns completion code `standby` at HTTP 503.

Performance-drift and temporary-quarantine holds preserve the last eligible engine, while failed health or inference probes can exclude it.

During backend outages and managed maintenance, the router retains control while a whole-wave hold pauses background monitoring until readmission; standbys use `control_ready` to copy current handoffs, and client load balancers route from the HTTP status.

Run both routers on one release so a replacement applies the same lifecycle rules and preserves saved ejections until recovery succeeds.

Select the router and engine scopes defined by the [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard), then read these metric groups:

| Signal                                         | Read                                                |
| ---------------------------------------------- | --------------------------------------------------- |
| Readiness and lease epoch                      | Which router owns admission.                        |
| Pool size and normalized load                  | Whether either phase is at its measured limit.      |
| Queue depth, pressure, and sheds               | Whether overload is waiting, protected, or refused. |
| Ejection, probation, and role floors           | How much healthy capacity remains.                  |
| TTFT, TPOT, queue wait, and seat time          | Where client latency is spent.                      |
| Retries, failures, refusals, and rejections    | Which protection path is active.                    |
| Role changes, reversals, and blocked decisions | Whether the controller is stable.                   |

Tune the router-down, engine-down, error-burst, unserved, ejection and role-floor alerts in `tools/prometheus-alerts.yml` to the deployment before paging.

Use request journals for per-request timing and placement, and metrics for process summaries bounded by counter resets and state-retention windows.

## Restart one engine

During first deployment, [check attestation across one engine restart](Deploy.md#check-attestation-across-one-engine-restart) to capture sidecar rejection and recovery on an idle engine. The procedure below adds router draining and readmission once the router is serving the fleet.

With `recovery.engine_restart_policy: individual`, drain the engine from new placement before the supervisor touches its process.

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}'
```

Poll `/narwhal/lifecycle` until `engines.e0.ready_to_stop` is `true`; when the drain deadline expires, the engine stays excluded with its resident work preserved.

Restart the engine and attestation sidecar through the external supervisor, then request readmission.

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}'
```

Readmission returns the engine after health, current process-bound attestation, a newer process identity, the configured model, direct generation, role-permitted KV transfer and final health pass; a failed gate keeps the engine blocked and returns HTTP 409 with its name.

After an unplanned breaker ejection, the same validation accepts the current process identity and automatically returns a passing engine, while a failed gate holds the engine for operator repair and explicit readmission.

When a contracted fleet loses every placement peer, automatic recovery waits for the complete cohort and validates it atomically. A failed cohort remains held; repair its failed check and request `POST /narwhal/lifecycle/readmit` with `{"wave":true}`. If an individual lifecycle hold loses its last available peer, promote the hold with `POST /narwhal/lifecycle/drain` and `{"wave":true}`, then continue with the [complete-wave procedure](#restart-an-engine-wave).

## Restart an engine wave

Set `recovery.engine_restart_policy: whole_wave` when the engine build shares peer state across the fleet, directing every drain and readmission through a complete wave.

A confirmed ejection, changed process identity or failed identity verification holds the entire fleet and withdraws readiness until an operator completes whole-wave readmission.

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/drain \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}'
```

After the router withdraws readiness, wait for `wave.ready_to_stop`, restart every engine and sidecar from one immutable build and then readmit the wave.

```bash
curl -fsS -X POST http://router:8000/narwhal/lifecycle/readmit \
  -H 'content-type: application/json' \
  -d '{"wave":true}'
```

Narwhal returns the fleet in one state change after every process start exceeds its recorded drain identity, every validation passes and the role-permitted KV ring completes; one failed member keeps the complete wave excluded.

After an unplanned whole-wave hold, send the explicit drain request so Narwhal records current process identities for the restart check. If identity collection fails on a stopped engine, start it under the fleet hold and retry the drain; once `wave.ready_to_stop` is true, restart every engine and sidecar, including the process started for identity collection, then request whole-wave readmission.

After each successful liveness sample, contracted fleets compare `/version`, `process_start_time_seconds` and process-bound attestation with the accepted identity, triggering recovery when those values differ.

The handoff records accepted process starts and restart policy so resume and standby takeover can compare each running engine before admission. A version mismatch on contracted resume holds the fleet for a managed wave or fails startup, and automatic takeover requires handoff schema version 1.

Identity checks detect a changed process start on the next successful liveness sample, normally after `recovery.liveness_every * controller.monitor_interval_s` plus probe and monitoring time.

Whole-wave policy uses these sweeps to detect process replacement, so coordinate planned restarts through the drain procedure and keep process-start metrics plus attestation accurate.

## Upgrade and rollback

For compatible handoff versions:

1. Stop the standby.
2. Install the new release and matching deployment set on that host.
3. Start it as standby and confirm `/health` is 200 while `/ready` is 503.
4. Stop the old active router cleanly.
5. Confirm the upgraded router owns a higher lease epoch and is the sole ready backend.
6. Upgrade the stopped router and return it as standby.

A handoff version mismatch requires a maintenance window: stop ingress and both routers, install one coherent deployment set, then start an active router and its standby.

For rollback, stop the new process before the old build can claim the fleet, then restore code, configuration, profiles and a state version supported by that build together.

## Required drills

Run the three CPU process drills on every release artifact.

```bash
make integration
```

`make integration` starts local stub engines and routers for individual and whole-wave lifecycle recovery, then the HA drill kills a primary during a stream and checks standby takeover plus recovered-primary fencing.

Before production, repeat the permitted engine restart procedure and router failover through the real supervisor and load balancer on an idle fleet.

| Drill | Pass condition |
| --- | --- |
| Individual engine restart | Placement stops after drain, resident work reaches zero and all readmission gates pass. |
| Whole-wave restart | The policy routes every action through the wave, withdraws readiness before stop, and readmits after every restarted engine passes attestation and the KV ring. |
| Router failover | The load balancer selects one lease owner; roles and cumulative counters survive; the previous primary stays fenced. |

The [troubleshooting guide](Troubleshoot.md) gives the failure procedures. [Measure a fleet](Measure.md) defines the production evidence.
