# Production boundary and router pair

## 1. Production boundary

Narwhal starts after site automation brings up the GPU hosts and engine processes. It handles admission and placement. The table lists the owner of each responsibility.

| Component         | Responsibility                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| Public ingress    | TLS, client authentication, WAF policy, body limits, public rate limits, model routing, and streaming proxy settings |
| Load balancer     | Polls `/ready` and routes traffic to the router returning HTTP 200                                                   |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, and readmission      |
| Engine supervisor | Engine and attestation-sidecar start/stop, resource limits, restart policy, and log retention                        |
| Shared storage    | Provides one coherent lease domain to both router hosts                                                              |
| Monitoring        | Scrapes metrics, retains journals, and pages according to site policy                                                |

## 2. Keep one deployment set

Both routers in a pair run the same deployment set under a single release identifier. The set is the Narwhal release, the fleet configuration, the profile store, the matching [deployment evidence set](../measure/03-Load-Trial.md#7-run-the-synthetic-deployment-trial), and, if `engine.first_token_calibration_path` is set, the first-token calibration artifact.

Install the deployment set on both router hosts. The calibration path must resolve from each router's working directory. A router does not start if the artifact is missing or was produced for a different engine generation. After replacing an engine, [recalibrate](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline), then copy the new artifact and fleet configuration to both hosts before restarting either router.

Before replacing or upgrading a router, check the handoff contracts the installed build implements:

```bash
narwhal-check --print-contract-versions
```

A rolling upgrade requires compatible handoff contracts on both routers. If they differ, follow [Upgrade and rollback](04-Upgrade-and-Validate.md#10-upgrade-and-rollback).

## 3. Configure the client path

Expose only the completion routes clients call. Keep these on the private network: the `/narwhal/*` control API, `/metrics`, `/health`, `/ready`, the engine APIs, and the attestation endpoints.

Ingress must:

- Authenticate clients and apply identity policy and public rate limits.
- Route each model to its router pair.
- Forward streaming chunks as they arrive.
- Set connect and idle timeouts from the service budget.
- Strip internal credentials and request IDs sent by clients, and insert trusted ones. Narwhal propagates the request ID for correlation, so ingress must assign it.

Narwhal assigns a separate request ID to each engine request, per attempt and phase. It authenticates to engines with the credential named by `engine.engine_api_key_env`.

The load balancer picks a router by polling `/ready`. The status code combines admission ownership, backend availability, and lifecycle state, so the load balancer needs no separate signal for each. [Lease behavior](#lease-behavior) covers the ownership part.

## 4. Start a router pair

Run [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) against the [deployment set](#2-keep-one-deployment-set) before either router serves production traffic.

Start the first router:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Start the second router as a standby of the first:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

Lease requirements:

- Both hosts see one shared lease domain that supports POSIX `flock`, coherent reads, and atomic rename.
- The clock offset between hosts stays below `--lease-safety-margin`.
- `--lease-ttl` is greater than `--lease-renew-interval` plus `--lease-safety-margin`.

When the active router reports ready, run the deployment workload through the production ingress path before admitting clients. See the [synthetic deployment trial](../measure/03-Load-Trial.md#7-run-the-synthetic-deployment-trial).

A reference load-balancer configuration is [deploy/ha/haproxy.cfg](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg).

### Lease behavior

A router returns HTTP 200 from `/ready` only while it holds a valid lease and is admitting traffic.

During a partition, the active router fences itself before its local lease deadline. The standby claims the lease only after it expires. `--lease-safety-margin` is the gap between the two, so host clock offset must stay below it. If shared storage is unavailable, both routers stop reporting ready.

On shutdown, an active lease holder saves its latest counters before releasing the lease. A standby or fenced router keeps the primary handoff it last saved.
