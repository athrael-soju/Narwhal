---
description: Define the production boundary and start a Narwhal primary and standby router pair.
---

# Production boundary and router pair

## Production boundary

A production deployment divides the work between Narwhal and the site's components:

| Component | Responsibility |
| --- | --- |
| Site automation | Provisions GPU hosts and engine processes |
| Public ingress | TLS, client authentication, rate limits, model routing, streaming proxy settings |
| Load balancer | Polls `/ready`, routes to the router that returns HTTP 200, and retries requests through a takeover |
| Narwhal | Admission, queueing, prefill and decode placement on the running fleet, retry, health ejection, role control, drain, readmission |
| Engine supervisor | Engine and attestation sidecar starts and stops, resource limits, restart policy, log retention |
| Shared storage | One lease domain shared by both router hosts |
| Monitoring | Metric scraping, journal retention, and paging per site policy |

## Keeping one deployment set

Install on both router hosts, with the same release identifier:

- the Narwhal release
- the fleet configuration
- the profile store
- the first-token calibration artifact, when `engine.first_token_calibration_path` is set, readable from each router's working directory
- the [deployment evidence set](../measure/02-Targets-and-Freeze.md#freezing-the-deployment-under-test)

When preflight's `calibration` gate rejects the first-token calibration artifact:

1. [Recalibrate](../deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration).
2. Distribute the new artifact and fleet configuration.
3. Restart the routers.

Inspect state handoff contracts before a router change:

```bash
narwhal-check --print-contract-versions
```

When the handoff contract versions match on both routers, follow the [rolling upgrade](04-Upgrade-and-Validate.md#rolling-upgrade-with-compatible-handoff-versions). When they differ, follow [Upgrading across a handoff-version change](04-Upgrade-and-Validate.md#upgrading-across-a-handoff-version-change).

## Configuring the client path

Public ingress carries the completion routes clients use. The private network carries `/narwhal/*`, `/metrics`, `/health`, `/ready`, the engine APIs, and the attestation endpoints.

Ingress must:

- remove client-supplied internal credentials and request IDs
- insert trusted replacements
- authenticate clients
- apply identity policy and rate limits
- route each model to its router pair
- forward streaming chunks as they arrive
- enforce connect and idle timeouts set from the service budget

Each engine leg receives:

- a request ID per attempt and phase
- the engine credential identified by `engine.engine_api_key_env`

Configure the load balancer from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg). Every 200 milliseconds, HAProxy checks each router with `GET /narwhal/targets` and then `GET /ready`. A router is in rotation while two conditions hold:

- `/narwhal/targets` returns HTTP 200. It does so while the router holds the lease and is not shutting down.
- `/ready` does not report `engine identity validation pending`. A router reports this while it validates engine identities after it starts or takes over.

HAProxy takes a router out of rotation at the first check that fails, and returns it at the first check that passes. A check also fails when the router does not accept its connection within 200 milliseconds or does not answer within one second.

A standby, a fenced router, a router that is shutting down and a stopped router are therefore out of rotation. While no router is in rotation, HAProxy holds each new request and tries again every second, up to 10 times, then answers HTTP 503 itself. It retries a request whose connection to a router fails in the same way. A new request therefore waits for the standby when the standby takes over within 10 seconds of the request, as it does at the default `--lease-ttl`. The held requests reach the new router together, and admission can refuse some of them with HTTP 429. A request in flight on a crashed router fails.

When the supervisor restarts a stopped router at once, HAProxy has already taken the stopped router out of rotation. The restarted process is a standby, so HAProxy sends it no requests, and new requests wait for the new holder.

The lease holder stays in rotation while it is not ready for another reason. HAProxy passes it each request, and the client receives the router's own HTTP 503 with `Retry-After: 1`. The router counts it as `rejected` with reason `not_ready`. The error type is `backend_unavailable` when no engine is live, and `not_ready` during a whole-wave hold or degraded engine monitoring. The body's `reason` names the hold. [Admission and responses](../http-api/02-Admission-and-Responses.md) lists each refusal.

When a router answers HTTP 503 while both routers are in rotation, HAProxy retries the request, alternating between them up to 10 times. Both are in rotation only during a takeover, after the new holder passes its checks and before HAProxy's next check of the old router. One client request can reach one router up to 6 times. Each router journals every attempt that it answers. It records a `standby` or `not_ready` refusal as `rejected` with reason `not_ready`. It records a `no_schedulable_engines` failure, or a `backend_unavailable` failure after prefill, as `failed` with reason `no_engine`. A request that a router fails after prefill can therefore appear in up to 6 journal rows on that router. Each row's `attempts` field lists up to `serving.max_attempts` attempts. The 503 retry applies to requests up to about 127 KiB, headers included.

A router that does not hold the lease answers the fleet-state reads, such as `GET /narwhal/engines` and `GET /narwhal/state`, with an HTTP 503 [fleet-state refusal](../http-api/04-Inspection.md#fleet-state-reads-on-a-router-without-the-lease). Through HAProxy, a read during a takeover waits with other requests for the new holder, for up to 10 seconds. A read that reaches the old router before HAProxy's next check of it gets that 503, which names the reason.

## Starting a router pair

Run the final [preflight](../deploy/06-Profile-and-Preflight.md#running-preflight) against the deployment set.

The shared lease path must support POSIX `flock`, coherent reads, and atomic rename. The host clock offset must stay below `--lease-safety-margin`. `--lease-ttl` must exceed the sum of `--lease-renew-interval` and `--lease-safety-margin`.

Start the first router, with its private listen address as `--host`:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Start the standby on the second host:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

Configure the supervisor to restart each router with `--standby-of` set to the other router's URL.

Admission sequence:

1. Wait for the active router's `/ready` to return HTTP 200.
2. Send the deployment workload through the intended ingress path.
3. Open client admission.

### Lease behavior

A router that holds a valid lease and admits traffic returns HTTP 200 from `/ready`. During a network partition, the active lease holder fences itself before its local lease deadline, and the standby claims the lease at expiry. When shared storage becomes unavailable, both routers withdraw readiness.

At shutdown, the `/ready` and fleet-state reads of the active lease holder return HTTP 503 with reason `shutting down`. Thus its load balancer checks and the probes of the standby fail. For `--shutdown-drain` seconds, 0.5 by default, it still accepts connections and serves every request, while the load balancer takes it out of rotation. Set `--shutdown-drain` longer than the load balancer's check interval. The shipped HAProxy configuration checks every 0.2 seconds. If the router stopped listening or closed its idle connections earlier, it would reset a connection that the load balancer just opened or reused. The client would then receive HTTP 502. After the drain, the router stops accepting connections and closes its idle ones. For 0.1 seconds, it still admits the requests that it already received. Then it writes its handoff, releases the lease and finishes the requests that it dispatched to prefill. A request that still needs a prefill placement, for example one waiting for an admission or prefill seat, then gets HTTP 503 `standby`. The standby takes over during that drain and counts the drained streams as offered but not served. A standby or fenced router retains its saved primary state handoff when it shuts down.

When the supervisor restarts the stopped router at once with `--standby-of` set to the standby, both routers are standbys until one claims the lease. Each counts the other's `/ready` answer, HTTP 503 with reason `shadowing`, as a failed poll. The first router to reach its `--standby-takeover-after` failed polls while the lease is free claims it. The standby resumes from the handoff it received from the stopped router, and the restarted router resumes from the handoff the stopped router last saved. The other router then shadows the new primary. After a `SIGTERM`, which frees the lease, the standby claims first when both routers run with the same options, because it starts counting failed polls before the restarted process starts. After a `SIGKILL`, both routers wait for the lease to expire, and either can claim it.

When both routers restart at once, each with `--standby-of` set to the other, the same rule elects one holder. Each router counts the `shadowing` answers of the other as failed polls. The first router that finds the lease free claims it and resumes from the handoff at its state path. That handoff must be at most `--standby-max-handoff-age` seconds old. The other router then shadows the new holder. HAProxy holds new requests until the holder is ready. When a SIGKILL stopped the old holder, the new holder is ready only after the lease expires.

## Monitoring the router pair

To scrape both routers and discover the engines through the load balancer, start the monitoring stack as in [Monitoring a router pair](../observability/01-Start-and-Verify.md#monitoring-a-router-pair).

On the dashboard, **Router roles** shows which router holds the lease, and a marker on every chart shows each takeover. [Selectors](../observability/05-Dashboard.md#selectors) lists the panels that read the active router and the panels that add both routers.
