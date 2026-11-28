---
description: Serving options, standby takeover and lease fencing for the narwhal-serve router.
---

# `narwhal-serve`

Start a router with `narwhal-serve --fleet PATH`, with the router's [control token](../HTTP-API.md#control-token) in `NARWHAL_ROUTER_TOKEN`. Without the token the router serves completions and reads, refuses every membership and lifecycle change with HTTP `403`, and cannot run as a standby.

`narwhal-serve` checks `--host` and `--port` before it loads the fleet file. It exits with these statuses:

| Condition | Exit status |
| --- | :--: |
| `--host` and `--port` fail the bind check | `1` |
| An argument is invalid, including a `--port` outside 0 to 65535 and a `--max-concurrent` above `serving.max_connections` | `2` |
| The fleet configuration fails to load | `2` |
| The router configuration fails validation | `2` |
| `NARWHAL_ROUTER_TOKEN` holds fewer than 32 random bytes or equals the engine API key, or is unset on a router started with `--standby-of` | `2` |

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
| `--max-concurrent N` | Config `serving.max_connections` | Router [in-flight limit](../configuration/02-Serving-and-Role-Control.md#in-flight-limit), from 1 to `serving.max_connections` inclusive. |
| `--graceful-timeout SECONDS` | Config `serving.graceful_timeout_s` | Uvicorn shutdown drain time, in whole seconds, zero or greater. |
| `--shutdown-drain SECONDS` | `0.5` | Time a stopping lease holder keeps accepting connections and serving requests after its readiness fails, finite and zero or greater. Set it longer than the load balancer's health-check interval. See [Lease behavior](../operate/01-Start-Routers.md#lease-behavior). |
| `--resume` | Config `recovery.resume` | Restore engine membership, roles, breaker holds, and counters from the last state handoff. |

`--resume` only turns resume on. To keep resume off, set `recovery.resume` to `false` and omit `--resume`.

## Standby takeover and lease fencing

A standby router started with `--standby-of URL` polls the primary every `--standby-probe-interval` seconds. A poll counts as failed unless the primary returns a handoff [signed](../http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff) under the standby's own control token, so both routers need the same `NARWHAL_ROUTER_TOKEN`. It takes over when all of these hold:

- `--standby-takeover-after` consecutive polls have failed.
- The last handoff the standby received is at most `--standby-max-handoff-age` seconds old.
- The shared lease has expired.

Set `--lease-path` on both routers to the same file in the pair's [lease domain](../operate/01-Start-Routers.md#starting-a-router-pair).

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--standby-of URL` | `""` | Primary router URL. | URL, or empty for an active router |
| `--standby-probe-interval SECONDS` | `0.25` | Interval between polls of the primary. | Finite, positive |
| `--standby-takeover-after N` | `4` | Consecutive failed polls required for takeover. | At least 1 |
| `--standby-max-handoff-age SECONDS` | `30.0` | Maximum age of a state handoff eligible for takeover. | Finite, positive |
| `--lease-path PATH` | `""` | Shared lease file. | Path, required with `--standby-of` |
| `--router-id NAME` | host and port | Stable router name, the prefix of each start's lease-holder token. | |
| `--lease-ttl SECONDS` | `5.0` | Lease lifetime. | Finite, greater than the renewal interval plus the safety margin |
| `--lease-renew-interval SECONDS` | `1.0` | Interval between lease renewals. | Finite, positive |
| `--lease-safety-margin SECONDS` | `1.0` | Allowed clock skew between routers. | Finite, zero or greater |
