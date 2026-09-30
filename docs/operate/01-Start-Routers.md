# Production boundary and router pair

## 1. Production boundary

| Component         | Responsibility                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| Site automation   | Provisions GPU hosts and engine processes |
| Public ingress    | TLS, client authentication, rate limits, model routing, streaming proxy settings |
| Load balancer     | Polls `/ready` and routes to the router that returns HTTP 200 |
| Narwhal           | Admission, queueing, prefill and decode placement on the running fleet, retry, health ejection, role control, drain, readmission |
| Engine supervisor | Starts and stops engines and attestation sidecars. Sets resource limits, restart policy, and log retention |
| Shared storage    | One lease domain shared by both router hosts                                                                  |
| Monitoring        | Metric scraping, journal retention, and paging per site policy                                                       |

## 2. Keep one deployment set

Give both routers the same release identifier and these five items:

- the Narwhal release;
- the fleet configuration;
- the profile store;
- the first-token calibration artifact, when `engine.first_token_calibration_path` is set;
- the corresponding [deployment evidence set](../measure/02-Targets-and-Freeze.md#6-freeze-the-deployment-under-test).

Install that set on both router hosts. The configured calibration path must be readable from each router's working directory.

The calibration artifact's process generation must match the live engine. After an engine replacement:

1. [Recalibrate](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
2. Distribute the new artifact and fleet configuration.
3. Restart the routers.

Inspect state handoff contracts before a router change:

```bash
narwhal-check --print-contract-versions
```

Rolling transitions need matching handoff contract versions on both routers. If they differ, follow [Upgrade across a handoff-version change](04-Upgrade-and-Validate.md#102-upgrade-across-a-handoff-version-change).

## 3. Configure the client path

Expose only the completion routes clients use. Keep these interfaces on the private network:

- `/narwhal/*`
- `/metrics`
- engine APIs
- attestation endpoints
- `/health`
- `/ready`

Ingress must:

- remove client-supplied internal credentials and request IDs;
- insert trusted replacements;
- authenticate clients;
- apply identity policy and rate limits;
- route each model to its router pair;
- forward streaming chunks as they arrive;
- enforce connect and idle timeouts derived from the service budget.

Narwhal propagates the trusted request ID. Each engine leg receives:

- its own attempt-specific and phase-specific request ID;
- the engine credential identified by `engine.engine_api_key_env`.

Configure the load balancer from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg). It routes by `/ready` status. `/ready` reports the admitting router, backend availability, and lifecycle state.

## 4. Start a router pair

Run the final [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) against the deployment set. Start both routers from that set.

| Item | Requirement |
| --- | --- |
| Shared lease path | POSIX `flock`, coherent reads, and atomic rename |
| Host clock offset | Below `--lease-safety-margin` |
| `--lease-ttl` | `narwhal-serve` requires a value above the sum of `--lease-renew-interval` and `--lease-safety-margin` |

Start the first router on its host, with its private listen address as `--host`:

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
| The network partitions | The active lease holder fences itself before its local lease deadline. The standby claims the lease after it expires. |
| Shared storage becomes unavailable | Both routers withdraw readiness. |
| A standby or fenced router shuts down | It retains its saved primary state handoff. |
| The active lease holder shuts down | It persists its latest counters before releasing control. |
