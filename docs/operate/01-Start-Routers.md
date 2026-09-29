# Production boundary and router pair

## 1. Production boundary

Site automation provisions GPU hosts and engine processes; Narwhal admits requests and places them on the running fleet.

| Component         | Responsibility                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| Public ingress    | TLS, client authentication, rate limits, model routing, streaming proxy settings |
| Load balancer     | Polls `/ready` and routes to the router that returns HTTP 200 |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, readmission |
| Engine supervisor | Starts and stops engines and attestation sidecars; sets resource limits, restart policy, log retention |
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

The router needs a calibration artifact whose process generation matches the live engine. After an engine replacement, [recalibrate](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) and distribute the new artifact and fleet configuration before restarting the routers.

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

Ingress must remove client-supplied internal credentials and request IDs and insert trusted replacements. It also authenticates clients, applies identity policy and rate limits, and routes each model to its router pair. It forwards streaming chunks as they arrive and enforces connect and idle timeouts derived from the service budget.

Narwhal propagates the trusted request ID. Each engine leg receives:

- its own attempt-specific and phase-specific request ID;
- the engine credential identified by `engine.engine_api_key_env`.

The load balancer routes by `/ready` status, which reports the admitting router, backend availability, and lifecycle state.

## 4. Start a router pair

Run the final [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) against the deployment set, then start both routers from it.

The shared lease path needs POSIX `flock`, coherent reads, and atomic rename.

Keep host clock offset below `--lease-safety-margin`.

Set `--lease-ttl` above the sum of `--lease-renew-interval` and `--lease-safety-margin`; `narwhal-serve` rejects anything lower.

Start the first router on its host, using its private listen address for `--host`:

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

When the active router's `/ready` returns HTTP 200, send the deployment workload through the intended ingress path before opening client admission.

The shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg) is the load balancer reference.

### Lease behavior

| Condition | Behavior |
| --- | --- |
| A router holds a valid lease and admits traffic | `/ready` returns HTTP 200. |
| The network partitions | The active lease holder fences itself before its local lease deadline. The standby claims the lease after it expires. |
| Shared storage becomes unavailable | Both routers withdraw readiness. |
| A standby or fenced router shuts down | It retains its saved primary state handoff. |
| The active lease holder shuts down | It persists its latest counters before releasing control. |
