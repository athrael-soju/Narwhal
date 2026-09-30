# `narwhal-serve`

`narwhal-serve` runs one Narwhal router from a fleet config.

```bash
narwhal-serve --fleet fleet.json
```

Before it reads the fleet config, the command tests that it can bind `--host` and `--port`, using the same IPv4, IPv6 and wildcard handling as Uvicorn. If the bind fails, it exits with status 1. Uvicorn binds again at startup, so a race on the address fails the start.

For how command-line options interact with values in the config, see [CLI precedence](../configuration/06-Fabric-and-Operations.md#18-cli-precedence).

## Options

| Option                       | Default                                 | Description                                                                                   |
| ---------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------- |
| `--fleet PATH`               | required                                | Fleet config file (JSON).                                                                     |
| `--host HOST`                | `127.0.0.1`                             | Address to listen on.                                                                         |
| `--port PORT`                | `8000`                                  | Port to listen on.                                                                            |
| `--log-level LEVEL`          | `info`                                  | One of `critical`, `error`, `warning`, `info`, `debug`, or `trace`.                           |
| `--journal PATH`             | `journal.jsonl` next to `profiles.path` | Request journal. New entries are appended to the file.                                        |
| `--max-concurrent N`         | `serving.max_connections`               | Maximum requests the router admits at once. Must be between 1 and `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s`            | How long Uvicorn waits for in-flight requests at shutdown. A whole number, 0 or more.         |
| `--resume`                   | `recovery.resume`                       | Restore roles, breaker holds and counters from the last state handoff.                        |
| `--version`                  |                                         | Print the installed version.                                                                  |

## Standby routers and lease fencing

You can run a second router as a standby of the first. A standby polls the primary with `--standby-of` and takes over after `--standby-takeover-after` consecutive failed polls, once the shared lease has expired. It skips `--resume` at startup and applies the primary's state handoff when it takes over.

Both routers read and write the lease file, so `--lease-path` must point to storage they share. See [Start a router pair](../operate/01-Start-Routers.md#4-start-a-router-pair) for filesystem requirements, partition behavior and load-balancer checks.

The duration options below take seconds and must be finite.

| Option                              | Default                           | Description                                                                                                                                         |
| ----------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--standby-of URL`                  | `""` (start as the active router) | URL of the primary. Starts this router as a standby that can take over with fencing.                                                   |
| `--standby-probe-interval SECONDS`  | `0.25`                            | How often to poll the primary. Must be positive.                                                                                                    |
| `--standby-takeover-after N`        | `4`                               | Failed polls in a row before the standby tries to take over. At least 1.                                                                            |
| `--standby-max-handoff-age SECONDS` | `30.0`                            | The standby refuses to take over from a state handoff older than this. Must be positive.                                                                     |
| `--lease-path PATH`                 | `""` (local control only)         | Lease file on shared storage. Required with `--standby-of`.                                                                                         |
| `--router-id NAME`                  | host and port                     | Name used as the prefix of the lease-holder token. The token is regenerated on every start.                  |
| `--lease-ttl SECONDS`               | `5.0`                             | How long a lease lasts. Must be longer than the renewal interval plus the safety margin.                                                            |
| `--lease-renew-interval SECONDS`    | `1.0`                             | How often the holder renews the lease. Must be positive.                                                                                            |
| `--lease-safety-margin SECONDS`     | `1.0`                             | Clock skew budget. The holder treats its lease as expired this long before the TTL ends. Must be 0 or more. |

If a lease record's `expires_at` or `updated_at` is not a finite timestamp, no router can acquire or renew the lease. The current holder keeps its last valid deadline, tracked on a monotonic clock, and the lease expires on schedule.
