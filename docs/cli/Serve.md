# `narwhal-serve`

`narwhal-serve` runs one Narwhal router from a fleet config.

```bash
narwhal-serve --fleet fleet.json
```

Before it reads the fleet config, the command checks that it can bind `--host` and `--port`, following Uvicorn's rules for IPv4, IPv6, and wildcard addresses. If the bind fails, it exits with status 1. Uvicorn binds again when it starts, so a process that takes the address in between is still caught.

For how command-line options interact with values in the config, see [CLI precedence](../configuration/06-Fabric-and-Operations.md#18-cli-precedence).

## Options

| Option                       | Default                                 | Description                                                                                   |
| ---------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------- |
| `--fleet PATH`               | required                                | Fleet config file (JSON).                                                                     |
| `--host HOST`                | `127.0.0.1`                             | Address to listen on.                                                                         |
| `--port PORT`                | `8000`                                  | Port to listen on.                                                                            |
| `--log-level LEVEL`          | `info`                                  | One of `critical`, `error`, `warning`, `info`, `debug`, or `trace`.                           |
| `--journal PATH`             | `journal.jsonl` next to `profiles.path` | Request journal. New entries are appended to the file.                                        |
| `--max-concurrent N`         | `serving.max_connections`               | Most requests the router will admit at once. Must be between 1 and `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s`            | How long Uvicorn waits for in-flight requests at shutdown. A whole number, 0 or more.         |
| `--resume`                   | `recovery.resume`                       | Restore roles, breaker holds, and counters from the last state handoff.                       |
| `--version`                  |                                         | Print the installed version.                                                                  |

## Standby routers and lease fencing

You can run a second router as a standby. Give it the primary's URL with `--standby-of` and it will poll the primary. It takes over once enough polls in a row have failed and the shared lease has expired. A standby doesn't apply `--resume` at startup; it applies the primary's handoff when it takes over.

Both routers read and write the lease file, so `--lease-path` must point to storage they share. [Start a router pair](../operate/01-Start-Routers.md#4-start-a-router-pair) covers the filesystem requirements, what happens during a network partition, and how to set up load-balancer checks.

Durations are in seconds and must be finite.

| Option                              | Default                           | Description                                                                                                                                         |
| ----------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--standby-of URL`                  | `""` (start as the active router) | URL of the primary. Setting this starts the router as a standby that can take over with fencing.                                                    |
| `--standby-probe-interval SECONDS`  | `0.25`                            | How often to poll the primary. Must be positive.                                                                                                    |
| `--standby-takeover-after N`        | `4`                               | Failed polls in a row before the standby tries to take over. At least 1.                                                                            |
| `--standby-max-handoff-age SECONDS` | `30.0`                            | The standby won't take over from saved state older than this. Must be positive.                                                                     |
| `--lease-path PATH`                 | `""` (local control only)         | Lease file on shared storage. Required with `--standby-of`.                                                                                         |
| `--router-id NAME`                  | host and port                     | A stable name for this router, used as the prefix of its lease-holder token. The router generates a new token each time it starts.                  |
| `--lease-ttl SECONDS`               | `5.0`                             | How long a lease lasts. Must be longer than the renewal interval plus the safety margin.                                                            |
| `--lease-renew-interval SECONDS`    | `1.0`                             | How often the holder renews the lease. Must be positive.                                                                                            |
| `--lease-safety-margin SECONDS`     | `1.0`                             | The most clock skew between the routers that the lease allows for. This much time is set aside before the lease expires locally. Must be 0 or more. |

If a lease record's `expires_at` or `updated_at` isn't a finite timestamp, no router can acquire or renew the lease. The current holder keeps counting down to its last good deadline on its monotonic clock, so its lease still runs out on time.
