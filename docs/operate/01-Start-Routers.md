# Production boundary and router pair

## 1. Production boundary

Narwhal doesn't provision anything. Site automation brings up the GPU hosts and engine processes, and Narwhal takes over once they're running: it admits requests and places them on the fleet. Everything around that is someone else's job, as the table shows.

| Component         | Responsibility                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------------- |
| Public ingress    | TLS, client authentication, WAF policy, body limits, public rate limits, model routing, and streaming proxy settings |
| Load balancer     | Poll `/ready` and route traffic to the router returning HTTP 200                                                     |
| Narwhal           | Admission, queueing, prefill and decode placement, retry, health ejection, role control, drain, and readmission      |
| Engine supervisor | Engine and attestation-sidecar start/stop, resource limits, restart policy, and log retention                        |
| Shared storage    | Provide one coherent lease domain to both router hosts                                                               |
| Monitoring        | Scrape metrics, retain journals, and page according to site policy                                                   |

## 2. Keep one deployment set

Both routers in a pair run the same deployment set under a single release identifier. The set is the Narwhal release, the fleet configuration, the profile store, the matching [deployment evidence set](../measure/03-Load-Trial.md#7-run-the-synthetic-deployment-trial), and, if `engine.first_token_calibration_path` is set, the first-token calibration artifact.

Install the whole set on both router hosts, and make sure the calibration path resolves from each router's working directory. A router won't start if the artifact is missing or was produced for a different engine generation. That means replacing an engine has a knock-on effect: you need to [recalibrate](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) and push the new artifact and fleet configuration to both hosts before restarting either router.

Before you replace or upgrade a router, check which handoff contracts the installed build implements:

```bash
narwhal-check --print-contract-versions
```

You can only do a rolling transition if both routers implement compatible handoff contracts. If the versions don't match, follow the maintenance procedure in [Upgrade and rollback](04-Upgrade-and-Validate.md#10-upgrade-and-rollback) instead.

## 3. Configure the client path

Expose only the completion routes your clients actually call. The rest stays on the private network: the `/narwhal/*` control API, `/metrics`, `/health`, `/ready`, the engine APIs, and the attestation endpoints.

Ingress handles the client-facing side of the request path. It authenticates clients, applies identity policy and public rate limits, and routes each model to its router pair. It should forward streaming chunks as they arrive rather than buffering them, and its connect and idle timeouts should come from the service budget. It also has to strip any internal credentials or request IDs that a client sends and insert trusted ones in their place. Narwhal carries that trusted request ID through for correlation, so it must be one that ingress assigned.

Narwhal then gives each engine leg its own request ID, specific to the attempt and the phase, and authenticates to the engine with the credential named by `engine.engine_api_key_env`.

The load balancer picks a router by polling `/ready`. The status code there folds together admission ownership, backend availability, and lifecycle state, so the load balancer doesn't need to know about any of them separately.

## 4. Start a router pair

Before either router serves production traffic, run the final [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) against the deployment set. Both routers must start from that same [deployment set](#2-keep-one-deployment-set).

Start the first router:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-a \
  --lease-path /shared/narwhal/router.lease
```

Then start its peer as a standby of the first:

```bash
narwhal-serve \
  --fleet config/fleet.production.json \
  --host <private-listen-address> --port 8000 \
  --router-id router-b \
  --lease-path /shared/narwhal/router.lease \
  --standby-of http://router-a:8000
```

Both hosts have to see one shared lease domain that supports POSIX `flock`, coherent reads, and atomic rename. Keep the clock offset between them below `--lease-safety-margin`, and set the lease TTL higher than the renewal interval plus the lease safety margin.

When the active router reports ready, send the deployment workload through the real ingress path before you let clients in. For the load balancer, start from the shipped [HAProxy configuration](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg); it's the reference setup.

### Lease behavior

A router returns HTTP 200 from `/ready` only while it holds a valid lease and is admitting traffic.

If the pair gets partitioned, the active router fences itself before its local lease deadline, and the standby can claim the lease once it has expired. The lease safety margin is the gap between those two events, which is why host clocks need to stay within it. If shared storage goes away entirely, both routers withdraw readiness.

What a router saves on shutdown depends on its role. A standby or fenced router keeps the primary handoff it last saved. An active lease holder writes out its latest counters before it gives up control.
