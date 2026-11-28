---
description: Define the production boundary and start a Narwhal primary and standby router pair.
---

# Production boundary and router pair

## Production boundary

On SSH hosts, `narwhal deploy` starts and supervises the fleet's processes, and the site keeps the hosts and everything around the fleet:

| Component | Owner | Responsibility |
| --- | --- | --- |
| Engines | Narwhal | `narwhal deploy run` starts each engine container from a checked launch plan and checks it on every run. The container has no restart policy, so an engine that stops stays down until the next `run` restarts it, as [Engine restart and process replacement](03-Restart-Engines.md) describes |
| Attestation sidecars and routers | Narwhal | Transient systemd user units that systemd restarts on failure. `run` and `status` check each restarted unit before they record its new identity |
| Load balancer | Narwhal | HAProxy from the shipped configuration, in a container that Docker restarts on failure. It polls `/ready`, routes to the router that returns HTTP 200, and retries requests through a takeover |
| Prometheus and Grafana | Narwhal | The monitoring stack on the primary router's host, in containers that Docker restarts |
| Router | Narwhal | Admission, queueing, prefill and decode placement on the running fleet, retry, health ejection, role control, drain, readmission |
| Hosts | Site | Provisioning, the operating system, drivers, the container runtime, Docker, systemd lingering for the SSH user, SSH access and host keys, firewalls, clocks, and the staged checkpoint and engine image |
| Lease store | Site | One shared mount both router hosts reach at the lease path, which `lease-check` and `router-start` check |
| Recovery | Site | Running `narwhal deploy run` again after a host reboot, a stopped engine or a new release, as [After a host reboot](../Deploy.md#after-a-host-reboot) and [Deploying a new release](../Deploy.md#deploying-a-new-release) describe |
| Public ingress | Site | TLS, client authentication, rate limits, model routing, streaming proxy settings |
| Alerting and logs | Site | The Alertmanager and its paging, the dead man's switch for `NarwhalWatchdog`, and retention of the unit logs, container logs and request journals, which Narwhal does not rotate |

On Kubernetes, the [fleet controller](../deploy/Kubernetes.md) runs the stages for the namespace's `NarwhalFleet` and builds the fleet's workloads, the kubelet restarts their containers, and the site keeps the cluster:

| Component | Owner | Responsibility |
| --- | --- | --- |
| Engines and attestation sidecars | Narwhal | The controller builds one StatefulSet of engine pods, each with its engine container and its sidecar, from checked launch plans, and pins each pod to the node discovery measured. The kubelet restarts a failed container, and the controller checks the new identity on its next pass, as [Engine restarts](../deploy/Kubernetes.md#engine-restarts) describes |
| Routers | Narwhal | The controller builds the router pair's StatefulSet, and the kubelet restarts a failed router. The routers elect the primary through a `Lease`, as [Electing the primary on Kubernetes](#electing-the-primary-on-kubernetes) describes |
| Load balancer | Narwhal | The controller builds HAProxy's Deployment and Service from the shipped configuration |
| Gate Jobs | Narwhal | The controller runs every check as a Job, adopts the Jobs a restarted controller left running, and deletes them in the background so their pods go too |
| Prometheus and Grafana | Narwhal or site | The chart runs both, or leaves them to the cluster's Prometheus Operator and Grafana, as [Monitoring](../deploy/Kubernetes.md#monitoring) describes |
| Cluster | Site | Kubernetes 1.30 or later, the nodes, the GPU and RDMA device plugins, the storage classes, a network plugin that enforces NetworkPolicy, and the images in a registry the nodes reach |
| Charts and access | Site | Installing and upgrading both charts, the namespace of each fleet, the RBAC bindings of whoever writes the fleet, and the Secrets that hold the engine key and the routers' control token, as [Security and access](../deploy/Kubernetes.md#security-and-access) describes |
| Staged inputs | Site | The staged checkpoint on the model volume, and the deployment inputs in the `NarwhalFleet` |
| Recovery | Site | Asking a stalled fleet to run again, stopping it before a change that reruns discovery, replacing a lost controller node, and deleting what a deleted fleet leaves, as [Recovery](../deploy/Kubernetes.md#recovery) describes |
| Cluster health, alerting and logs | Site | Node GPU health, volume usage and Job outcomes in the cluster's own monitoring, the Alertmanager, and retention of the container logs, which the kubelet rotates |
| Public ingress | Site | TLS, client authentication, rate limits, model routing, streaming proxy settings |

## Keeping one deployment set

`narwhal deploy run` installs one release on every host, and its `fleet-limits` stage copies one fleet, profile store and calibration to both router hosts. A pair you start yourself needs the same on both router hosts, with the same release identifier:

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

`narwhal deploy` and the Kubernetes fleet controller forward only `POST /v1/completions`, `POST /v1/chat/completions`, `GET /v1/models`, `GET /health`, `GET /ready` and `GET /narwhal/targets` through HAProxy, matched by method and exact path. Every other request answers HTTP 404 there, so a client of the load balancer cannot reach `/narwhal/state`, `/metrics`, membership or lifecycle routes. Stages, the other router and monitoring read those at a router's own address. The shipped file itself forwards every path; `narwhal deploy` adds the filter when it binds the file to the pair.

A standby, a fenced router, a router that is shutting down and a stopped router are therefore out of rotation. While no router is in rotation, HAProxy holds each new request and tries again every second, up to 10 times, then answers HTTP 503 itself. It retries a request whose connection to a router fails in the same way. A new request therefore waits for the standby when the standby takes over within 10 seconds of the request, as it does at the default `--lease-ttl`. The held requests reach the new router together, and admission can refuse some of them with HTTP 429. A request in flight on a crashed router fails.

When the supervisor restarts a stopped router at once, HAProxy has already taken the stopped router out of rotation. The restarted process is a standby, so HAProxy sends it no requests, and new requests wait for the new holder.

The lease holder stays in rotation while it is not ready for another reason. HAProxy passes it each request, and the client receives the router's own HTTP 503 with `Retry-After: 1`. The router counts it as `rejected` with reason `not_ready`. The error type is `backend_unavailable` when no engine is live, and `not_ready` during a whole-wave hold or degraded engine monitoring. The body's `reason` names the hold. [Admission and responses](../http-api/02-Admission-and-Responses.md) lists each refusal.

When a router answers HTTP 503 while both routers are in rotation, HAProxy retries the request, alternating between them up to 10 times. Both are in rotation only during a takeover, after the new holder passes its checks and before HAProxy's next check of the old router. One client request can reach one router up to 6 times. Each router journals every attempt that it answers. It records a `standby` or `not_ready` refusal as `rejected` with reason `not_ready`. It records a `no_schedulable_engines` failure, or a `backend_unavailable` failure after prefill, as `failed` with reason `no_engine`. A request that a router fails after prefill can therefore appear in up to 6 journal rows on that router. Each row's `attempts` field lists up to `serving.max_attempts` attempts. The 503 retry applies to requests up to about 127 KiB, headers included.

A router that does not hold the lease answers the fleet-state reads, such as `GET /narwhal/engines` and `GET /narwhal/state`, with an HTTP 503 [fleet-state refusal](../http-api/04-Inspection.md#fleet-state-reads-on-a-router-without-the-lease). Through HAProxy, a read during a takeover waits with other requests for the new holder, for up to 10 seconds. A read that reaches the old router before HAProxy's next check of it gets that 503, which names the reason.

## Starting a router pair

`narwhal deploy run` starts the pair in its [`router-start` stage](../deploy/07-Serve-and-Measure.md#starting-the-router-pair), with the options below. To run a pair under your own supervisor instead, run the final [preflight](../deploy/06-Profile-and-Preflight.md#running-preflight) against the deployment set first.

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

### Electing the primary on Kubernetes

On Kubernetes the routers elect the primary through a `coordination.k8s.io` Lease instead of a file, so no shared volume holds the lease. Start both routers with `--kubernetes-lease` set to the same Lease name in place of `--lease-path`. The [fleet controller](../deploy/Kubernetes.md#starting-the-router-pair) starts them on the Lease `narwhal-router`. The router's ServiceAccount, which the narwhal chart makes, needs `get` and `update` on that Lease, and `create` on Leases so the first router can create it. RBAC cannot limit `create` to one name, since the name is in the request body.

Each router fences itself on its own clock, `CLOCK_BOOTTIME` on Linux, which keeps counting while the host is suspended, so a holder that resumes finds its deadline gone. No decision compares one router's clock with the other's or with the API server's:

- The holder controls the fleet until `--lease-ttl` less `--lease-safety-margin` after it sent its last renewal that the API server accepted: 4 seconds with the defaults. When the API server does not answer, the holder keeps serving until that deadline and then fences itself.
- A standby takes the Lease only after it has seen one version of it, by `resourceVersion`, stay unchanged on its own clock for the longer of the Lease's `leaseDurationSeconds` and its own `--lease-ttl`, and only with an update that fails if the Lease changed since. Every renewal that lands is a new version, so the holder's deadline passes at least `--lease-safety-margin` seconds before the standby can take over.
- A standby that saw the holder release the Lease claims it at once. A release keeps the epoch and carries the annotation `narwhal.io/released-by`, naming the holder and epoch. A Lease patched free by hand has no such mark and counts as a free Lease the standby never saw released.
- A router that creates the Lease, or claims a free Lease it never saw released, holds it at once and serves one lease duration later, so any holder it never saw has fenced by then. This happens on a fleet's first start and after someone deletes the Lease.

Both routers must run with the same `--lease-ttl`. A router refuses to claim a Lease that lasts longer than its own `--lease-ttl`, and reports `lease unavailable`. A router that creates the Lease after a deletion waits only the longest duration it has seen, so a peer on a longer `--lease-ttl` that it never saw could still be inside its deadline.

A hypervisor that pauses a holder's virtual machine and stops its guest clock also stops the holder's deadline. When the machine resumes, the holder keeps serving until its next renewal, at most `--lease-renew-interval` later, meets the standby's update as a conflict and fences it. Keep such pauses from router hosts, or count that interval as a window with two routers.

A standby reads the Lease after every poll that reaches the primary, and on each takeover attempt once `--standby-takeover-after` polls have failed. With the defaults it therefore takes over 5 to about 7 seconds after the primary's last renewal, inside the 10 seconds HAProxy holds a request. A conflict or a write whose reply was lost never counts as a lost or won Lease: the router reads the Lease again and checks the holder, the epoch and the renewal time it wrote.

While the API server is unreachable, no router can renew or claim the Lease. An outage shorter than the holder's deadline changes nothing. A longer one fences the primary, and no router serves until the API server answers and the standby claims the Lease. The standby receives no handoff while it cannot read the Lease, so after an outage longer than `--standby-max-handoff-age` it reports `no fresh handoff`. Once the Lease has stayed expired for that long, the standby claims it as a [cold start](../cli/Serve.md#standby-takeover-and-lease-fencing) and serves without a handoff.

The Lease outlives the routers. A router that stops cleanly frees it, so the next first start claims it. Delete it with `kubectl delete lease narwhal-router` only while no router runs.

Admission sequence:

1. Wait for the active router's `/ready` to return HTTP 200.
2. Send the deployment workload through the intended ingress path.
3. Open client admission.

### Lease behavior

A router that holds a valid lease and admits traffic returns HTTP 200 from `/ready`. During a network partition, the active lease holder fences itself before its local lease deadline, and the standby claims the lease at expiry. When shared storage becomes unavailable, both routers withdraw readiness.

With `--lease-path`, a router takes the lease's coordination lock with non-blocking attempts every 10 ms, so a standby on a network file system claims the lease at expiry rather than when the file system next retries a blocked lock. A renewal that cannot take the lock before the holder's local lease deadline fences the holder. A claim waits one lease period at most, `--lease-ttl` less `--lease-safety-margin`, and reports the held lock as `lease unavailable`.

At shutdown, the `/ready` and fleet-state reads of the active lease holder return HTTP 503 with reason `shutting down`. Thus its load balancer checks and the probes of the standby fail. For `--shutdown-drain` seconds, 0.5 by default, it still accepts connections and serves every request, while the load balancer takes it out of rotation. Set `--shutdown-drain` longer than the load balancer's check interval. The shipped HAProxy configuration checks every 0.2 seconds. If the router stopped listening or closed its idle connections earlier, it would reset a connection that the load balancer just opened or reused. The client would then receive HTTP 502. After the drain, the router stops accepting connections and closes its idle ones. For 0.1 seconds, it still admits the requests that it already received. Then it writes its handoff, releases the lease and finishes the requests that it dispatched to prefill. A request that still needs a prefill placement, for example one waiting for an admission or prefill seat, then gets HTTP 503 `standby`. The standby takes over during that drain and counts the drained streams as offered but not served. A standby or fenced router retains its saved primary state handoff when it shuts down.

When the supervisor restarts the stopped router at once with `--standby-of` set to the standby, both routers are standbys until one claims the lease. Each counts the other's `/ready` answer, HTTP 503 with reason `shadowing`, as a failed poll. The first router to reach its `--standby-takeover-after` failed polls while the lease is free claims it. The standby resumes from the handoff it received from the stopped router, and the restarted router resumes from the handoff the stopped router last saved. The other router then shadows the new primary. After a `SIGTERM`, which frees the lease, the standby claims first when both routers run with the same options, because it starts counting failed polls before the restarted process starts. After a `SIGKILL`, both routers wait for the lease to expire, and either can claim it.

When both routers restart at once, each with `--standby-of` set to the other, the same rule elects one holder. Each router counts the `shadowing` answers of the other as failed polls. The first router that finds the lease free claims it and resumes from the handoff at its state path. That handoff must be at most `--standby-max-handoff-age` seconds old. On a Kubernetes Lease, when neither router holds one that fresh, as after both stopped for longer, the first router that finds the lease absent, or expired for longer than `--standby-max-handoff-age`, claims it as a [cold start](../cli/Serve.md#standby-takeover-and-lease-fencing). The other router then shadows the new holder. HAProxy holds new requests until the holder is ready. When a SIGKILL stopped the old holder, the new holder is ready only after the lease expires.

## Monitoring the router pair

To scrape both routers and discover the engines through the load balancer, start the monitoring stack as in [Monitoring a router pair](../observability/01-Start-and-Verify.md#monitoring-a-router-pair).

On the dashboard, **Router roles** shows which router holds the lease, and a marker on every chart shows each takeover. [Selectors](../observability/05-Dashboard.md#selectors) lists the panels that read the active router and the panels that add both routers.
