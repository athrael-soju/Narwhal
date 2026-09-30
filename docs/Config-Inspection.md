# Offline fleet configuration

You can check a fleet file without any engines running. `narwhal config validate` runs the same checks the router runs when it starts serving: the schema, each individual field, endpoint references, and the rules that tie fields to each other.

It only reads the fleet file and the environment variables its endpoints refer to. That means it works before you've collected any profiles, and it doesn't care whether the engine addresses are reachable yet.

```bash
narwhal config validate --fleet config/fleet.json
narwhal config inspect --fleet config/fleet.json --format json
```

| Option                | What it does                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`        | The fleet file to validate or inspect.                                                                      |
| `--format text\|json` | `text` prints a readable result. `json` wraps the result in a versioned [command result](CLI-Reference.md). |

With `inspect --format json`, the command result's `data` field holds a `narwhal.effective-config` version 1 document. In text mode, `inspect` prints that same document on its own. `validate --format json` gives you the same data, but only when validation passes.

The output is deterministic. If you run `inspect` twice with the same file, environment, working directory, and filesystem paths, you'll get byte-for-byte identical JSON, so you can diff two runs to see exactly what changed.

## Fleet-file values and serving defaults

The document is marked `scope: "fleet_file"` because it shows the fleet file as the loader sees it, with defaults filled in. `settings` follows the `narwhal.fleet` layout with every default populated. You'll see optional engine fields you never set, and `null` for any optional contract, hardware, or shared-device declarations. Any `${VARIABLE}` references in endpoint fields are shown with their values substituted.

The engine API key is handled differently. `engine.engine_api_key_env` shows the name of the environment variable, never its value. Engine clients only read the key when they're created, so you can inspect a configuration before the credential even exists.

`derived` holds values the loader works out rather than reads directly: the engine count, connection-pool and keepalive limits, default admission concurrency, the retained HTTP request limit, and the Uvicorn shutdown timeout (as a whole number of seconds). Two of these are worth understanding:

- If you leave `engine.control_connections` at zero, the loader sets it to `max(4, 2 * engine_count)`. Any positive value you set is used as written.
- Admission concurrency defaults to `serving.max_connections`. The retained HTTP request limit is that number plus `serving.queue_capacity`.

> **Note:** `inspect` shows what's in the fleet file, which isn't necessarily what the running router uses. Flags passed to `narwhal-serve` (`--max-concurrent`, `--journal`, `--graceful-timeout`, and `--resume`) are applied when the router starts and can override these values. To see what a live router is actually using, look at the command line it was started with.

## Artifact paths

`source` and everything under `artifact_paths` are absolute paths, with any existing symlinks resolved.

> **Warning:** Relative paths in the fleet file are resolved from your current working directory, which the output reports as `working_directory`. They are *not* resolved relative to the fleet file's own location. `narwhal-serve` works the same way, so run `inspect` from the directory you'll serve from, or the paths it shows won't match what the router opens.

`~` and environment variables inside path strings are kept as literal text and aren't expanded.

`artifact_paths.profiles` comes from `profiles.path`, and `artifact_paths.state` comes from `recovery.state_path`. The journal defaults to `journal.jsonl` in the same directory as the profiles.

`inspect` only touches the filesystem to resolve these paths. It doesn't open the profiles or the state file, and it doesn't create anything. The commands that actually use those files take care of that.

## Checking the running fleet

Offline validation tells you the configuration is well-formed and consistent. It can't tell you whether the fleet actually works. Once the engines are up and you've collected profiles, run [`narwhal-check`](cli/Check.md) to test reachability, engine contracts, profile bindings, transfer paths, and SLO gates against the live fleet:

```bash
narwhal-check --fleet config/fleet.json
```
