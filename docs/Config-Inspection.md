# `narwhal config`

`narwhal config validate` runs the serving schema, field, endpoint-reference, and cross-field checks on a fleet file. It reads only the fleet file and endpoint environment variables; profiles and reachable engines are optional.

Validate and inspect a fleet file:

```bash
narwhal config validate --fleet config/fleet.json
narwhal config inspect --fleet config/fleet.json --format json
```

| Option | Default | Description |
| --- | --- | --- |
| `--fleet PATH` | required | Fleet file to validate or inspect. |
| `--format text\|json` | `text` | `text`, or `json` for a versioned command result wrapping the effective configuration. |

| Invocation | Output |
| --- | --- |
| `inspect` in text mode | A `narwhal.effective-config` version 1 document. |
| `inspect --format json` | That document in the `data` field of the [command result](Command-Results.md). |
| `validate --format json` | The same `data` after successful validation. |

Output is identical across runs with the same file, environment, working directory, and filesystem path bindings.

## Fleet-file values and serving defaults

| Field | Content |
| --- | --- |
| `scope: "fleet_file"` | Values from the fleet file and the loader's defaults. |
| `settings` | `narwhal.fleet` layout with every default populated, including optional engine fields. Optional contract, hardware, and shared-device declarations are `null`. |
| Endpoint fields | Resolved `${VARIABLE}` values. |
| `engine.engine_api_key_env` | The credential variable's name. Inspection succeeds while the variable is unset. |
| `derived` | Engine count, connection-pool and keepalive limits, default admission concurrency, retained HTTP request limit, and integer Uvicorn shutdown timeout. |

Derivations:

| Value | Derivation |
| --- | --- |
| `engine.control_connections` | `max(4, 2 * engine_count)` when configured as zero. A positive value applies as set. |
| Default admission concurrency | `serving.max_connections` |
| Retained HTTP request limit | `serving.max_connections` plus `serving.queue_capacity` |

`narwhal config` reports only the fleet-file layer. `narwhal-serve` flags such as `--max-concurrent`, `--journal`, `--graceful-timeout`, and `--resume` apply at router construction.

## Artifact paths

- `source` and `artifact_paths` hold absolute paths with existing symlinks resolved.
- Relative paths resolve from `working_directory`, the serving process working directory.
- Path strings inside the file keep literal `~` and environment-variable text.

| Path | Source |
| --- | --- |
| `artifact_paths.profiles` | `profiles.path` |
| `artifact_paths.state` | `recovery.state_path` |
| Default journal | `journal.jsonl` beside the profiles |

Inspection only resolves these paths. The commands that use the files read and create them.

With engines running and profiles present, run [`narwhal-check --fleet config/fleet.json`](cli/Check.md). It checks live reachability, engine contracts, profile bindings, transfer paths, and service-level objective (SLO) gates.
