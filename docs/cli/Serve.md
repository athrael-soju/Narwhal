---
description: Serving options, standby takeover and lease fencing for the narwhal-serve router.
---

# `narwhal-serve`

Start a router with `narwhal-serve --fleet PATH`.

`narwhal-serve` checks `--host` and `--port` before it loads the fleet file. It exits with these statuses:

| Condition | Exit status |
| --- | :--: |
| `--host` and `--port` fail the bind check | `1` |
| An argument is invalid, including a `--port` outside 0 to 65535 and a `--max-concurrent` above `serving.max_connections` | `2` |
| The fleet configuration fails to load | `2` |
| The router configuration fails validation | `2` |

## Serving options

A command-line option takes precedence over the matching fleet configuration field.

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--fleet PATH` | required | Fleet configuration JSON file. |
| `--host HOST` | `127.0.0.1` | Uvicorn bind address. |
| `--port PORT` | `8000` | Uvicorn bind port, from 0 to 65535. |
| `--log-level LEVEL` | `info` | Logging threshold: `critical`, `error`, `warning`, `info`, `debug`, or `trace`. |
| `--journal PATH` | `journal.jsonl` beside `profiles.path` | Request journal, append mode. |
| `--deployment-status PATH` | none | [Deployment status](../telemetry/06-Deployment-Runs.md#deployment-status) that `/metrics` exports as the [deployment metrics](../telemetry/03-Metrics-and-Control.md#deployment-and-restart-metrics). It is read on each scrape. |
| `--max-concurrent N` | Config `serving.max_connections` | Router [in-flight limit](../configuration/02-Serving-and-Role-Control.md#in-flight-limit), from 1 to `serving.max_connections` inclusive. |
| `--graceful-timeout SECONDS` | Config `serving.graceful_timeout_s` | Uvicorn shutdown drain time, in whole seconds, zero or greater. |
| `--shutdown-drain SECONDS` | `0.5` | Time a stopping lease holder keeps accepting connections and serving requests after its readiness fails, finite and zero or greater. Set it longer than the load balancer's health-check interval. See [Lease behavior](../operate/01-Start-Routers.md#lease-behavior). |
| `--resume` | Config `recovery.resume` | Restore engine membership, roles, breaker holds, and counters from the last state handoff. |

`--resume` only turns resume on. To keep resume off, set `recovery.resume` to `false` and omit `--resume`.

## Standby takeover and lease fencing

A standby router started with `--standby-of URL` polls the primary every `--standby-probe-interval` seconds. It takes over when all of these hold:

- `--standby-takeover-after` consecutive polls have failed.
- The last handoff the standby received is at most `--standby-max-handoff-age` seconds old.
- The lease has expired, or the standby saw its holder release it.

Give both routers the same lease: `--lease-path` set to one file in the pair's [lease domain](../operate/01-Start-Routers.md#starting-a-router-pair), or on Kubernetes `--kubernetes-lease` set to one Lease name, as in [Electing the primary on Kubernetes](../operate/01-Start-Routers.md#electing-the-primary-on-kubernetes).

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--standby-of URL` | `""` | Primary router URL. | URL, or empty for an active router |
| `--first-start-primary` | off | Start as the primary when no lease exists or the lease is free, as a Kubernetes Lease is once its holder released it, or when a lease file expired longer ago than `--standby-max-handoff-age`, so no standby can still take it over. Otherwise, or when another router claims the lease first, start as the standby of `--standby-of`, as also while the `--lease-witness`, when set, is missing, or while the router cannot read the Kubernetes Lease. Set it on one router of a pair, so that router claims the lease on the pair's first start and after both routers stopped, and a restart of it while its peer can take over rejoins as the standby. | Requires `--standby-of` |
| `--standby-probe-interval SECONDS` | `0.25` | Interval between polls of the primary. | Finite, positive |
| `--standby-takeover-after N` | `4` | Consecutive failed polls required for takeover. | At least 1 |
| `--standby-max-handoff-age SECONDS` | `30.0` | Maximum age of a state handoff eligible for takeover. | Finite, positive |
| `--lease-path PATH` | `""` | Shared lease file. Its directory must exist; the router never creates it, so a missing mount stops the router from taking a lease of its own. On start, the router takes the lease lock and removes the temporary files a lease writer left when it died mid-write: `.<lease file name>.<pid>.<32 hex digits>.tmp` and `.now` beside the lease, last modified more than 10 lease TTLs before the lease storage's own clock, which it reads from a file it creates there. It touches no other file. | Path. `--standby-of` requires it or `--kubernetes-lease`, not both |
| `--lease-witness PATH` | `""` | File on the shared lease storage that must exist before the router reads or claims the lease, such as the `.narwhal-lease-witness` that `narwhal deploy`'s [lease check](../deploy/07-Serve-and-Measure.md#checking-the-shared-lease-path) leaves. While it is missing, `--first-start-primary` starts the router as the standby. | Path, requires `--lease-path` |
| `--kubernetes-lease NAME` | `""` | Kubernetes Lease in the pod's namespace that elects the primary, in place of `--lease-path`. The router finds the API server through `KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`, and reads the token, CA and namespace from `/var/run/secrets/kubernetes.io/serviceaccount`. Its ServiceAccount needs `get` and `update` on the Lease, and `create` on Leases for the first start. | DNS subdomain name. Requires a whole-second `--lease-ttl` and a positive `--lease-safety-margin` |
| `--router-id NAME` | host and port | Stable router name, the prefix of each start's lease-holder token. | |
| `--lease-ttl SECONDS` | `5.0` | Lease lifetime, the Lease's `leaseDurationSeconds` with `--kubernetes-lease`. | Finite, greater than the renewal interval plus the safety margin. Whole seconds with `--kubernetes-lease` |
| `--lease-renew-interval SECONDS` | `1.0` | Interval between lease renewals. | Finite, positive |
| `--lease-safety-margin SECONDS` | `1.0` | How long before its lease expires the holder stops controlling the fleet. With `--lease-path` it covers the clock skew between the router hosts. With `--kubernetes-lease` it covers clock rate and scheduling delay, since no clock is compared with another. | Finite, zero or greater. Positive with `--kubernetes-lease` |
