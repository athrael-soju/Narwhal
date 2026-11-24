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

Configure the load balancer from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg). It keeps a router in rotation for 8 seconds after the router's `/ready` check starts failing. When a connection to a router fails, HAProxy retries the request up to 10 times, waiting one second before each retry to the same router. A new request therefore waits through a takeover after a router crash, provided the standby passes `/ready` within those 8 seconds, as it does with the default `--lease-ttl`. The held requests reach the new router together, and admission can refuse some of them with HTTP 429. A request in flight on the crashed router fails. While both routers pass or are within their 8 seconds, HAProxy also retries an HTTP 503, alternating between the routers up to 10 times. Each router journals every attempt it answers: a `standby` or not-ready refusal as `rejected` with reason `not_ready`, and a `no_schedulable_engines` failure, or a `backend_unavailable` failure after prefill, as `failed` with reason `no_engine`. One client request can therefore produce up to 6 rows on a router, and run up to 6 times on a router that fails it after prefill. This happens only during a takeover, after the standby passes `/ready` and while the old router, or a process restarted in its place, still answers within its 8 seconds. The 503 retry applies to requests up to about 127 KiB, headers included.

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

At shutdown, the active lease holder writes its handoff and releases the lease, then finishes its open streams. Its `/ready` returns HTTP 503 with reason `shutting down` during the drain. The standby takes over during that drain and counts the drained streams as offered but not served. A standby or fenced router retains its saved primary state handoff when it shuts down.
