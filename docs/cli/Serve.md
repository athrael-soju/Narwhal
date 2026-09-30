# `narwhal-serve`

Run `narwhal-serve --fleet PATH` to start a router from a fleet configuration.

`narwhal-serve` checks that `--host` and `--port` can bind before it reads the fleet configuration. A failed bind check exits with status 1.

## Serving options

Command-line options take [precedence](../configuration/06-Fabric-and-Operations.md#18-cli-precedence) over the matching fleet configuration fields.

| Option                       | Default                                | Description                                                                         |
| ---------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------- |
| `--version`                  |                                        | Print the installed distribution version.                                           |
| `--fleet PATH`               | required                               | Fleet configuration JSON file.                                                      |
| `--host HOST`                | `127.0.0.1`                            | Uvicorn bind address.                                                               |
| `--port PORT`                | `8000`                                 | Uvicorn bind port.                                                                  |
| `--log-level LEVEL`          | `info`                                 | Logging threshold: `critical`, `error`, `warning`, `info`, `debug`, or `trace`.     |
| `--journal PATH`             | `journal.jsonl` beside `profiles.path` | Request journal, opened in append mode.                                             |
| `--max-concurrent N`         | Config `serving.max_connections`       | Router admission limit, from 1 to `serving.max_connections` inclusive.              |
| `--graceful-timeout SECONDS` | Config `serving.graceful_timeout_s`    | Uvicorn shutdown drain time, in whole seconds, zero or greater.                     |
| `--resume`                   | Config `recovery.resume`               | Restore roles, breaker holds, and counters from the last state handoff.             |

## Standby takeover and lease fencing

`--standby-of URL` starts a standby router that polls the primary. The standby takes over when both conditions hold:

- `--standby-takeover-after` consecutive polls have failed.
- The shared lease has expired.

Set `--lease-path` on both routers to the same file in the pair's lease domain.

Lease filesystem requirements and load-balancer checks: [Operate Narwhal](../operate/01-Start-Routers.md#4-start-a-router-pair).

| Option                              | Default       | Description                                                                                                                                                                                   |
| ----------------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--standby-of URL`                  | `""`          | Primary router URL for standby mode and fenced takeover. Empty starts an active router.                                                                                                       |
| `--standby-probe-interval SECONDS`  | `0.25`        | Interval between polls of the primary. Must be finite and positive.                                                                                                                           |
| `--standby-takeover-after N`        | `4`           | Consecutive failed polls required for takeover. Must be at least 1.                                                                                                                           |
| `--standby-max-handoff-age SECONDS` | `30.0`        | Maximum age of a state handoff eligible for takeover. Must be finite and positive.                                                                                                            |
| `--lease-path PATH`                 | `""`          | Shared lease file, required with `--standby-of`. Empty uses local control.                                                                                                                    |
| `--router-id NAME`                  | host and port | Stable router name. Each start generates a new lease-holder token prefixed with it.                                                                                                           |
| `--lease-ttl SECONDS`               | `5.0`         | Lease lifetime. Must be finite and exceed the renewal interval plus the safety margin.                                                                                                        |
| `--lease-renew-interval SECONDS`    | `1.0`         | Interval between lease renewals. Must be finite and positive.                                                                                                                                 |
| `--lease-safety-margin SECONDS`     | `1.0`         | Allowed clock skew between routers. The holder's monotonic deadline is the last acquisition or renewal plus the lease lifetime, minus this margin. Must be finite, zero or greater.            |

A lease record needs finite `expires_at` and `updated_at` timestamps. An invalid record blocks acquisition and renewal.
