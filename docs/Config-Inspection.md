# `narwhal config`

`narwhal config validate` runs these checks on a fleet file:

- Serving schema
- Fields
- Endpoint references
- Cross-field rules

| Input                                                    | Requirement |
| -------------------------------------------------------- | ----------- |
| Fleet file                                               | Required    |
| Endpoint environment variables                           | Required    |
| Credential variable named by `engine.engine_api_key_env` | Optional    |
| Profiles                                                 | Optional    |
| Reachable engines                                        | Optional    |

```bash
narwhal config validate --fleet config/fleet.json
narwhal config inspect --fleet config/fleet.json --format json
```

| Option                | Default  | Description                        |
| --------------------- | -------- | ---------------------------------- |
| `--fleet PATH`        | required | Fleet file to validate or inspect. |
| `--format text\|json` | `text`   | Output format.                     |

| Invocation               | Output                                                                         |
| ------------------------ | ------------------------------------------------------------------------------ |
| `inspect` in text mode   | A `narwhal.effective-config` version 1 document.                               |
| `inspect --format json`  | A `narwhal.effective-config` version 1 document in the `data` field of the [command result](Command-Results.md). |
| `validate --format json` | A `narwhal.effective-config` version 1 document in `data`.                       |

Output depends only on the fleet file, environment, working directory, and path bindings.

## Fleet-file values and serving defaults

| Field                                                                                   | Content                                                                                |
| --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `scope: "fleet_file"`                                                                   | Values from the fleet file and the loader's defaults.                                  |
| `settings`                                                                              | `narwhal.fleet` layout with every default populated.         |
| `settings.engine_contract`, `settings.hardware`, and `settings.engines[].shared_device` | `null` when omitted from the fleet file.                                               |
| Endpoint fields                                                                         | Resolved `${VARIABLE}` values.                                                         |
| `engine.engine_api_key_env`                                                             | The credential variable's name.                                                        |

`derived` fields:

| Field                           | Value                                                                |
| ------------------------------- | -------------------------------------------------------------------- |
| `engine_count`                  | Number of engines.                                                   |
| `control_connections`           | `engine.control_connections` when positive.                          |
| `control_connections`           | `max(4, 2 * engine_count)` when `engine.control_connections` is `0`. |
| `data_keepalive_connections`    | `max(1, serving.max_connections // 2)`                               |
| `control_keepalive_connections` | `max(1, control_connections // 2)`                                   |
| `max_concurrent`                | Default admission concurrency, `serving.max_connections`.            |
| `http_retained_limit`           | `serving.max_connections + serving.queue_capacity`                   |
| `uvicorn_graceful_timeout_s`    | `serving.graceful_timeout_s` as an integer.                          |

`narwhal-serve` flags applied outside the `narwhal config` output:

- `--max-concurrent`
- `--journal`
- `--graceful-timeout`
- `--resume`

## Artifact paths

- `source` and `artifact_paths` hold absolute paths with existing symlinks resolved.
- Relative paths resolve from the serving process `working_directory`.
- Path strings inside the file keep literal `~` and environment-variable text.

| Path                      | Source                              | Written by        | Read by                          |
| ------------------------- | ----------------------------------- | ----------------- | -------------------------------- |
| `artifact_paths.profiles` | `profiles.path`                     | `narwhal-profile` | `narwhal-serve`, `narwhal-check` |
| `artifact_paths.state`    | `recovery.state_path`               | `narwhal-serve`   | `narwhal-serve`                  |
| Default journal           | `journal.jsonl` beside the profiles | `narwhal-serve`   |                                  |

Live checks run against running engines and the profile store:

```bash
narwhal-check --fleet config/fleet.json
```

[`narwhal-check`](cli/Check.md) covers:

- Live reachability
- Engine contracts
- Profile bindings
- Transfer paths
- Service-level objective (SLO) gates
