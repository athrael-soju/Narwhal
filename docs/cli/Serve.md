# `narwhal-serve`

`narwhal-serve` starts one router process from a fleet configuration. `--fleet` is required.

Before reading the fleet config, the command checks the selected port. An occupied port terminates startup with exit status 2.

[Configuration](../configuration/06-Fabric-and-Operations.md#18-cli-precedence) defines CLI and configuration precedence.

## Serving options

| Option                       | Default                                | Contract                                                                            |
| ---------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------- |
| `--fleet PATH`               | required                               | Fleet config JSON                                                                   |
| `--host HOST`                | `127.0.0.1`                            | Uvicorn bind address                                                                |
| `--port PORT`                | `8000`                                 | Uvicorn bind port                                                                   |
| `--log-level LEVEL`          | `info`                                 | `critical`, `error`, `warning`, `info`, `debug`, or `trace`                         |
| `--journal PATH`             | `journal.jsonl` beside `profiles.path` | Request journal, opened in append mode                                              |
| `--max-concurrent N`         | Config `serving.max_connections`       | Router admission limit. Must be between 1 and `serving.max_connections`, inclusive. |
| `--graceful-timeout SECONDS` | Config `serving.graceful_timeout_s`    | Uvicorn shutdown drain time in nonnegative integer seconds                          |
| `--resume`                   | Config `recovery.resume`               | Forces state resume on                                                              |

## Standby takeover and lease fencing

Set `--standby-of URL` to start the router in shadow mode against a primary. The standby polls that primary and begins takeover after the configured number of consecutive failed polls.

Automatic takeover requires shared lease storage. `--lease-path` is therefore required when `--standby-of` is set. [Operate Narwhal](../operate/01-Start-Routers.md#4-start-a-router-pair) defines the shared-filesystem contract, partition behaviour, and load-balancer checks.

| Option                              | Default       | Contract                                                                                                                 |
| ----------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `--standby-of URL`                  | `""`          | `""` starts active. A URL starts in shadow mode, polls that primary, and begins takeover after consecutive failed polls. |
| `--standby-probe-interval SECONDS`  | `0.25`        | Primary poll interval. Must be positive.                                                                                 |
| `--standby-takeover-after N`        | `4`           | Failed polls required before takeover. Must be at least 1.                                                               |
| `--standby-max-handoff-age SECONDS` | `30.0`        | Oldest state eligible for takeover. Must be positive.                                                                    |
| `--lease-path PATH`                 | `""`          | `""` uses local control. A path enables shared lease fencing and is required with `--standby-of`.                        |
| `--router-id NAME`                  | host and port | Stable operator name prefixed to a unique boot holder                                                                    |
| `--lease-ttl SECONDS`               | `5.0`         | Lease lifetime. Must exceed the renewal interval plus the safety margin.                                                 |
| `--lease-renew-interval SECONDS`    | `1.0`         | Lease renewal interval. Must be positive.                                                                                |
| `--lease-safety-margin SECONDS`     | `1.0`         | Reserved maximum relative clock skew before local expiry. Must be nonnegative.                                           |
