# Production boundary and router pair

## 1. Production boundary

Site automation provisions GPU hosts and engine processes; Narwhal admits requests and places them on the running fleet.

| Component         | Responsibility                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| Public ingress    | TLS, client authentication, WAF policy, body limits, public rate limits, model routing, and streaming proxy settings |
| Load balancer     | Poll `/ready` and route traffic to the router returning HTTP 200                                                     |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, and readmission      |
| Engine supervisor | Engine and attestation-sidecar start/stop, resource limits, restart policy, and log retention                        |
| Shared storage    | Provide one coherent lease domain to both router hosts                                                               |
| Monitoring        | Scrape metrics, retain journals, and page according to site policy                                                   |

## 2. Keep one deployment set

A router pair must run one coherent deployment set under one release identifier:

- the Narwhal release;
- the fleet configuration;
- the profile store;
- the corresponding [deployment evidence set](../measure/03-Load-Trial.md#7-run-the-synthetic-deployment-trial).

Install that set on both router hosts.

Before replacing or upgrading either router, inspect the installed build's handoff contracts:

```bash
narwhal-check --print-contract-versions
```

Both routers must implement compatible handoff contracts for a rolling transition. A handoff-version mismatch requires the maintenance procedure in [Upgrade and rollback](04-Upgrade-and-Validate.md#10-upgrade-and-rollback).

## 3. Configure the client path

Expose the completion routes required by clients.

Keep these interfaces on the private network:

- `/narwhal/*`
- `/metrics`
- engine APIs
- attestation endpoints
- `/health`
- `/ready`

Ingress performs:

- client authentication and public rate limiting;
- model-to-router-pair routing;
- streaming chunk forwarding as chunks arrive;
- connect and idle timeout enforcement derived from the service budget;
- removal of client-supplied internal credentials and request IDs before trusted replacements are inserted.

Ingress terminates client credentials and applies identity policy. Narwhal propagates the trusted request ID for correlation.

Each engine leg receives:

- its own attempt-specific and phase-specific request ID;
- the engine credential identified by `engine.engine_api_key_env`.

The load balancer routes against `/ready`. HTTP status on that endpoint represents admission ownership, backend availability, and lifecycle state.

## 4. Start a router pair

Run the final preflight against the deployment set before either router begins serving production traffic.

Start both routers from the same release, fleet configuration, and profile store.

Start the first router:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Start its peer:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

The router hosts require one shared lease domain with:

- POSIX `flock`;
- coherent reads;
- atomic rename.

Keep host clock offset below `--lease-safety-margin`.

Set the lease TTL above:

```text
renewal interval + lease safety margin
```

Once the active router reports ready, send the deployment workload through the intended ingress path before opening client admission.

The shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg) is the reference load-balancer configuration.

### Lease behaviour

A router returns HTTP 200 from `/ready` while it owns a valid lease and admits traffic.

During a partition, the active lease holder fences itself before its local lease deadline. The standby may claim the lease after expiry.

Loss of shared storage causes both routers to withdraw readiness.

Shutdown preserves role-dependent handoff state:

- a standby or fenced router retains its saved primary handoff;
- an active lease holder persists its latest counters before releasing control.
