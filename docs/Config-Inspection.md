# Offline fleet configuration

`narwhal config validate` loads a fleet file through the same schema, field,
endpoint-reference and cross-field checks used by serving. It reads the file and
endpoint environment variables, so validation succeeds before profiles exist and
while engine addresses are unreachable.

```bash
narwhal config validate --fleet config/fleet.json
narwhal config inspect --fleet config/fleet.json --format json
```

| Option | Operation |
| --- | --- |
| `--fleet PATH` | Load the fleet document selected for validation or inspection. |
| `--format text\|json` | Print validation in text mode or wrap the effective configuration in a versioned JSON command result. |

`inspect --format json` puts a `narwhal.effective-config` version 1 document in the
`data` field of the [command result](CLI-Reference.md). The default text mode prints
that inspection document directly. `validate --format json` returns the same data
after successful validation. Repeated inspection with the same file, environment,
working directory and filesystem path bindings produces identical JSON.

## Fleet-file values and serving defaults

`scope: "fleet_file"` identifies values supplied by the fleet file and the loader's
defaults. `settings` uses the `narwhal.fleet` layout with every default populated,
including optional engine fields and `null` for optional contract, hardware and
shared-device declarations. Endpoint fields contain resolved `${VARIABLE}` values;
`engine.engine_api_key_env` retains the credential variable's name. Engine clients
resolve its value when constructed, so configuration inspection works before that
credential is supplied.

`derived` reports the engine count, connection-pool and keepalive limits, default
admission concurrency, retained HTTP request limit and integer Uvicorn shutdown
timeout. The loader derives `engine.control_connections` from
`max(4, 2 * engine_count)` when its configured value is zero. An explicit positive
value takes precedence. Admission concurrency defaults to `serving.max_connections`;
the retained HTTP request limit adds `serving.queue_capacity`.

Serving flags such as `--max-concurrent`, `--journal`, `--graceful-timeout` and
`--resume` apply when `narwhal-serve` constructs the router. This inspection command
accepts the fleet-file layer; use the serving invocation to identify its overrides.

## Artifact paths

`source` and `artifact_paths` contain absolute paths, resolving existing symlinks.
Relative paths use `working_directory`, matching serving's process working
directory. The fleet file's parent directory supplies its location only. Path
strings inside the file retain literal `~` and environment-variable text.

`artifact_paths.profiles` and `artifact_paths.state` come from the loader's
`profiles.path` and `recovery.state_path`. The default journal is `journal.jsonl`
beside the profiles. Inspection resolves these names through filesystem reads;
profile contents, state contents and artifact creation belong to their consuming
commands.

After engines are running and profiles have been collected, run
[`narwhal-check --fleet config/fleet.json`](cli/Check.md) to verify live reachability,
engine contracts, profile bindings, transfer paths and SLO gates. Offline validation
covers configuration syntax and relationships; deployment preflight measures the
running fleet.
