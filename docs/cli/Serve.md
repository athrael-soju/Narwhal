# `narwhal-serve`

Run `narwhal-serve --fleet PATH` to start a router from a fleet configuration.

When `--host` and `--port` fail to bind, `narwhal-serve` exits with status 1.

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

A standby router started with `--standby-of URL` takes over from the primary when both conditions hold:

- `--standby-takeover-after` consecutive polls have failed.
- The shared lease has expired.

Set `--lease-path` on both routers to the same file in the pair's lease domain.

Lease filesystem requirements and load-balancer checks: [Operate Narwhal](../operate/01-Start-Routers.md#4-start-a-router-pair).

| Option                              | Default       | Description                                                          | Valid values                                                  |
| ----------------------------------- | ------------- | -------------------------------------------------------------------- | ------------------------------------------------------------- |
| `--standby-of URL`                  | `""`          | Primary router URL for standby mode and fenced takeover.             | URL, or empty for an active router                            |
| `--standby-probe-interval SECONDS`  | `0.25`        | Interval between polls of the primary.                               | Finite, positive                                              |
| `--standby-takeover-after N`        | `4`           | Consecutive failed polls required for takeover.                      | At least 1                                                    |
| `--standby-max-handoff-age SECONDS` | `30.0`        | Maximum age of a state handoff eligible for takeover.                | Finite, positive                                              |
| `--lease-path PATH`                 | `""`          | Shared lease file, required with `--standby-of`.                     | Path, or empty for local control                              |
| `--router-id NAME`                  | host and port | Stable router name, the prefix of each start's lease-holder token.   |                                                               |
| `--lease-ttl SECONDS`               | `5.0`         | Lease lifetime.                                                      | Finite, greater than the renewal interval plus the safety margin |
| `--lease-renew-interval SECONDS`    | `1.0`         | Interval between lease renewals.                                     | Finite, positive                                              |
| `--lease-safety-margin SECONDS`     | `1.0`         | Allowed clock skew between routers.                                  | Finite, zero or greater                                       |

Lease acquisition and renewal require finite `expires_at` and `updated_at` timestamps in the lease record.
