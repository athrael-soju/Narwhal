# Offline fleet configuration

`narwhal config validate` checks a fleet file without running engines. It runs the same checks the router runs at startup: schema, field values, endpoint references, and cross-field rules.

It reads only the fleet file and the environment variables that endpoints reference, so it works before profiles exist and does not check engine reachability.

```bash
narwhal config validate --fleet config/fleet.json
narwhal config inspect --fleet config/fleet.json --format json
```

| Option                | What it does                                                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`        | The fleet file to validate or inspect.                                                                           |
| `--format text\|json` | `text` prints human-readable output. `json` wraps the result in a versioned [command result](CLI-Reference.md). |

With `inspect --format json`, the command result's `data` field holds a `narwhal.effective-config` version 1 document. In text mode, `inspect` prints that document on its own. `validate --format json` emits the same data, but only when validation passes.

Output is deterministic: the same file, environment, working directory, and filesystem paths produce byte-identical JSON, so runs can be diffed.

## Fleet-file values and serving defaults

The `narwhal.effective-config` document has `scope: "fleet_file"`: it shows the fleet file as the loader sees it, with defaults filled in. `settings` follows the `narwhal.fleet` layout with every default populated. Optional engine fields left unset appear with their defaults; unset contract, hardware, and shared-device declarations are `null`. `${VARIABLE}` references in endpoint fields are shown with their values substituted.

The engine API key is the exception. `engine.engine_api_key_env` shows the name of the environment variable, never its value. The key is read only when engine clients are created, so `inspect` works before the credential exists.

`derived` holds values the loader computes: the engine count, connection-pool and keepalive limits, default admission concurrency, the retained HTTP request limit, and the Uvicorn shutdown timeout (in whole seconds). Two defaults:

- If `engine.control_connections` is zero, the loader sets it to `max(4, 2 * engine_count)`. Any positive value is used as written.
- Admission concurrency defaults to `serving.max_connections`. The retained HTTP request limit is that number plus `serving.queue_capacity`.

> **Note:** `inspect` reports fleet-file values. `narwhal-serve` flags (`--max-concurrent`, `--journal`, `--graceful-timeout`, `--resume`) are applied at startup and can override these values. To see what a live router uses, check its command line.

## Artifact paths

`source` and everything under `artifact_paths` are absolute paths, with existing symlinks resolved.

> **Warning:** Relative paths in the fleet file are resolved from the current working directory, which the output reports as `working_directory`. They are not resolved relative to the fleet file. `narwhal-serve` behaves the same way, so run `inspect` from the directory you serve from. From any other directory the reported paths differ from the ones the router opens.

`~` and environment variables in path strings stay literal text.

`artifact_paths.profiles` comes from `profiles.path`, and `artifact_paths.state` comes from `recovery.state_path`. The journal defaults to `journal.jsonl` in the profiles directory.

`inspect` only touches the filesystem to resolve these paths. It does not open the profiles or state file and does not create anything.

## Checking the running fleet

Offline validation checks that the configuration is well-formed and consistent. It does not test reachability or health. With engines running and profiles collected, run [`narwhal-check`](cli/Check.md) to test reachability, engine contracts, profile bindings, transfer paths, and SLO gates:

```bash
narwhal-check --fleet config/fleet.json
```
