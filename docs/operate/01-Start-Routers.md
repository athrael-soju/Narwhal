# Production boundary and router pair

## 1. Production boundary

| Component | Responsibility |
| --- | --- |
| Site automation | Provisions GPU hosts and engine processes |
| Public ingress | TLS, client authentication, rate limits, model routing, streaming proxy settings |
| Load balancer | Polls `/ready` and routes to the router that returns HTTP 200 |
| Narwhal | Admission, queueing, prefill and decode placement on the running fleet, retry, health ejection, role control, drain, readmission |
| Engine supervisor | Engine and attestation sidecar starts and stops, resource limits, restart policy, log retention |
| Shared storage | One lease domain shared by both router hosts |
| Monitoring | Metric scraping, journal retention, and paging per site policy |

## 2. Keep one deployment set

Install on both router hosts, with the same release identifier:

- the Narwhal release
- the fleet configuration
- the profile store
- the first-token calibration artifact, when `engine.first_token_calibration_path` is set, readable from each router's working directory
- the [deployment evidence set](../measure/02-Targets-and-Freeze.md#6-freeze-the-deployment-under-test)

When an engine process is replaced, match the calibration artifact to its process generation:

1. [Recalibrate](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
2. Distribute the new artifact and fleet configuration.
3. Restart the routers.

Inspect state handoff contracts before a router change:

```bash
narwhal-check --print-contract-versions
```

| Handoff contract versions on both routers | Procedure |
| --- | --- |
| Match | [Rolling upgrade](04-Upgrade-and-Validate.md#101-rolling-upgrade-with-compatible-handoff-versions) |
| Differ | [Upgrade across a handoff-version change](04-Upgrade-and-Validate.md#102-upgrade-across-a-handoff-version-change) |

## 3. Configure the client path

| Network | Interfaces |
| --- | --- |
| Public ingress | Completion routes clients use |
| Private | `/narwhal/*`, `/metrics`, `/health`, `/ready`, engine APIs, attestation endpoints |

Ingress must:

- remove client-supplied internal credentials and request IDs
- insert trusted replacements
- authenticate clients
- apply identity policy and rate limits
- route each model to its router pair
- forward streaming chunks as they arrive
- enforce connect and idle timeouts derived from the service budget

Each engine leg receives:

- a request ID per attempt and phase
- the engine credential identified by `engine.engine_api_key_env`

Configure the load balancer from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg).

## 4. Start a router pair

Run the final [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) against the deployment set.

Router pair requirements:

| Item | Requirement |
| --- | --- |
| Shared lease path | POSIX `flock`, coherent reads, and atomic rename |
| Host clock offset | Below `--lease-safety-margin` |
| `--lease-ttl` | Above the sum of `--lease-renew-interval` and `--lease-safety-margin` |

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

Admission sequence:

1. Wait for the active router's `/ready` to return HTTP 200.
2. Send the deployment workload through the intended ingress path.
3. Open client admission.

### Lease behavior

| Condition | Behavior |
| --- | --- |
| A router holds a valid lease and admits traffic | `/ready` returns HTTP 200. |
| The network partitions | The active lease holder fences itself before its local lease deadline. |
| The lease expires during a partition | The standby claims the lease. |
| Shared storage becomes unavailable | Both routers withdraw readiness. |
| A standby or fenced router shuts down | It retains its saved primary state handoff. |
| The active lease holder shuts down | It persists its latest counters before releasing control. |
