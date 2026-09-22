# CLI reference

Installing `narwhal-inference` adds five commands. Relative paths are resolved from the process working directory. All commands accept `-h` and `--help`. :chatgpt-content-reference{index="0"}

Use the installed commands in deployment scripts. `python -m narwhal.cli` can also start the serving process. Package ownership is documented in the [source responsibilities guide](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities).

## `narwhal-attest`

Runs an HTTP attestation sidecar for one engine. Start it after vLLM. Attestation stops if the engine version or process start time changes.

| Option                | Default     | Contract                                                |
| --------------------- | ----------- | ------------------------------------------------------- |
| `--document PATH`     | required    | Contract values plus a source for every populated field |
| `--engine-base URL`   | required    | vLLM base URL queried at `/version` and `/metrics`      |
| `--host HOST`         | `127.0.0.1` | Sidecar bind address                                    |
| `--port PORT`         | `8010`      | Sidecar port                                            |
| `--timeout-s SECONDS` | `5.0`       | Time budget for reading engine identity                 |

## `narwhal-serve`

Runs one router process. `--fleet` is required.

| Option                              | Default                                | Contract                                                                                                                 |
| ----------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `--fleet PATH`                      | required                               | Fleet config JSON                                                                                                        |
| `--host HOST`                       | `127.0.0.1`                            | Uvicorn bind address                                                                                                     |
| `--port PORT`                       | `8000`                                 | Uvicorn bind port                                                                                                        |
| `--log-level LEVEL`                 | `info`                                 | `critical`, `error`, `warning`, `info`, `debug`, or `trace`                                                              |
| `--journal PATH`                    | `journal.jsonl` beside `profiles.path` | Request journal, opened in append mode                                                                                   |
| `--max-concurrent N`                | Config `serving.max_connections`       | Router admission limit. Must be between 1 and `serving.max_connections`, inclusive.                                      |
| `--graceful-timeout SECONDS`        | Config `serving.graceful_timeout_s`    | Uvicorn shutdown drain time in nonnegative integer seconds                                                               |
| `--standby-of URL`                  | `""`                                   | `""` starts active. A URL starts in shadow mode, polls that primary, and begins takeover after consecutive failed polls. |
| `--standby-probe-interval SECONDS`  | `0.25`                                 | Primary poll interval. Must be positive.                                                                                 |
| `--standby-takeover-after N`        | `4`                                    | Failed polls required before takeover. Must be at least 1.                                                               |
| `--standby-max-handoff-age SECONDS` | `30.0`                                 | Oldest state eligible for takeover. Must be positive.                                                                    |
| `--lease-path PATH`                 | `""`                                   | `""` uses local control. A path enables shared lease fencing and is required with `--standby-of`.                        |
| `--router-id NAME`                  | host and port                          | Stable operator name prefixed to a unique boot holder                                                                    |
| `--lease-ttl SECONDS`               | `5.0`                                  | Lease lifetime. Must exceed the renewal interval plus the safety margin.                                                 |
| `--lease-renew-interval SECONDS`    | `1.0`                                  | Lease renewal interval. Must be positive.                                                                                |
| `--lease-safety-margin SECONDS`     | `1.0`                                  | Reserved maximum relative clock skew before local expiry. Must be nonnegative.                                           |
| `--resume`                          | Config `recovery.resume`               | Forces state resume on                                                                                                   |

Before reading the fleet config, `narwhal-serve` checks the selected port. If the port is already in use, the command exits with status 2. [Configuration](Configuration.md#paths-and-cli-precedence) documents CLI and configuration precedence.

Automatic standby takeover requires shared lease storage. [Operate Narwhal](Operate.md#start-the-production-routers) specifies the filesystem contract, partition behaviour, and load-balancer checks.

## `narwhal-profile`

Profiles every selected engine and writes the resulting store to `profiles.path` from the fleet config. The raw observations are written beside it using the same path with the suffix replaced by `.samples.json`.

Existing destinations require `--overwrite`. Symlink destinations are rejected. Use a different output path for each run if previous profiling evidence must be retained.

| Option                      | Default                                   | Contract                                                                                                                                                                |
| --------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required                                  | Fleet config JSON                                                                                                                                                       |
| `--only IID`                | all engines                               | Repeatable engine selector                                                                                                                                              |
| `--overwrite`               | false                                     | Starts a new profile/sample pair. Existing destinations are replaced when the first engine completes. With `--only`, the new store contains only the selected profiles. |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate lengths. The profiler keeps points within each engine's live `max_model_len` and requires at least three distinct usable values.                                                                                                |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep. Requires at least two distinct values.                                                                             |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Comma-separated stream counts. Requires at least two distinct values.                                                                                                   |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream. Minimum 3. Larger cohorts may require more tokens to overlap.                                                                                 |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length. Minimum 1.                                                                                                                              |

The run aborts if every `--only` value is absent from the configured engines, an engine fails `/health`, `/tokenize` omits a valid `max_model_len`, or the live limit leaves too few sweep points. Actual tokenised input plus requested output must fit before each completion. A successful run ends with `wrote N profile(s) to PATH`.

## `narwhal-check`

Runs these gates in order: `reach`, `contract`, `model`, `pace`, `tokenize`, `produce`, `consume`, `profile`, `slo`.

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required for preflight | Native fleet config JSON                                                                    |
| `--ring`                    | mesh                   | Uses ring coverage for `consume`. Without it, every eligible ordered pair is tested.        |
| `--repeats N`               | `1`                    | Transfer probes per pair. Values below 1 still run one probe.                               |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `model`, `pace`, `tokenize`, `profile`, and `slo`.                |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits.         |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

Preflight requires `--fleet`. Exit status 1 means a gate failed or the config could not be read or validated. Exit status 2 means the arguments are invalid.

Gate output and tables are intended for operator diagnosis. Automation should consume the contract registry and versioned artifacts.
