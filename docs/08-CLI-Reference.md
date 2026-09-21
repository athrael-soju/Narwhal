# CLI reference

Installing `narwhal-inference` creates five commands whose relative paths resolve from the process working directory. Every command accepts `-h` or `--help`.

Use these installed commands in deployment scripts. `python -m narwhal.cli` is also supported for serving. The [source responsibilities guide](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities) defines internal package ownership.

## `narwhal-attest`

Starts a small HTTP sidecar for one engine. Start it after vLLM. The sidecar stops attesting if the engine's version or process start time changes.

| Option | Default | Contract |
| --- | --- | --- |
| `--document PATH` | required | Contract values and a source for every populated field |
| `--engine-base URL` | required | vLLM base URL used for `/version` and `/metrics` |
| `--host HOST` | `127.0.0.1` | Sidecar bind address |
| `--port PORT` | `8010` | Sidecar port |
| `--timeout-s SECONDS` | `5.0` | Budget for reading engine identity |

## `narwhal-serve`

Starts one router process. `--fleet` is required.

| Option | Default | Contract |
| --- | --- | --- |
| `--fleet PATH` | required | Fleet config JSON |
| `--host HOST` | `127.0.0.1` | Uvicorn bind address |
| `--port PORT` | `8000` | Uvicorn bind port |
| `--log-level LEVEL` | `info` | `critical`, `error`, `warning`, `info`, `debug`, or `trace` |
| `--journal PATH` | `journal.jsonl` beside `profiles.path` | Request journal opened in append mode |
| `--max-concurrent N` | Config `serving.max_connections` | Router admission limit. Must be at least 1 and at most `serving.max_connections`. |
| `--graceful-timeout SECONDS` | Config `serving.graceful_timeout_s` | Uvicorn shutdown drain time in nonnegative integer seconds. |
| `--standby-of URL` | `""` | `""` starts active; a URL polls that primary while shadowing and starts takeover after consecutive silence. |
| `--standby-probe-interval SECONDS` | `0.25` | Primary poll interval. Must be positive. |
| `--standby-takeover-after N` | `4` | Consecutive failed polls before takeover. Must be at least 1. |
| `--standby-max-handoff-age SECONDS` | `30.0` | Maximum state age eligible for takeover. Must be positive. |
| `--lease-path PATH` | `""` | `""` uses local control; a path enables shared lease fencing and is required with `--standby-of`. |
| `--router-id NAME` | host and port | Stable operator name prefixed to a unique boot holder. |
| `--lease-ttl SECONDS` | `5.0` | Lease lifetime. Must exceed the renewal interval plus safety margin. |
| `--lease-renew-interval SECONDS` | `1.0` | Renewal cadence. Must be positive. |
| `--lease-safety-margin SECONDS` | `1.0` | Maximum relative clock skew reserved before local expiry. Must be nonnegative. |
| `--resume` | Config `recovery.resume` | Forces state resume on. |

Before loading the fleet, `narwhal-serve` exits with status 2 if the selected port is already in use. [Configuration](07-Configuration.md#paths-and-cli-precedence) records option precedence.

Automatic standby takeover requires shared lease storage. [Operate Narwhal](04-Operate.md#start-production-routers) defines its filesystem contract, partition behaviour, and load-balancer checks.

## `narwhal-profile`

Measures every selected engine and writes `profiles.path` from the fleet config. Replacing its suffix with `.samples.json` gives the raw-observation sidecar path. Existing destinations require `--overwrite`, while symlink destinations fail the write. Use a new output path per run to retain previous evidence.

| Option | Default | Contract |
| --- | --- | --- |
| `--fleet PATH` | required | Fleet config JSON |
| `--only IID` | all engines | Repeatable engine selector |
| `--overwrite` | false | Starts a fresh profile/sample pair, replacing existing destinations when the first engine completes. With `--only`, the new store contains just the selected profiles. |
| `--prefill-lens LIST` | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated integer prompt lengths. At least three distinct values are required. |
| `--decode-input-lens LIST` | `512,4096,8192` | Comma-separated prompt lengths used by the decode sweep. At least two distinct values are required. |
| `--decode-concurrency LIST` | `1,4,16,48` | Comma-separated integer stream counts. At least two distinct values are required. |
| `--decode-tokens N` | `64` | Tokens per decode stream. Must be at least 3; larger cohorts may need more to overlap. |
| `--prefill-repeats N` | `3` | Repetitions per prefill length. Must be at least 1. |

The command aborts when every `--only` value misses the configured engines, an engine fails `/health`, or exact tokenization fails on a dialect that advertises a tokenization route. Success ends with `wrote N profile(s) to PATH`.

## `narwhal-check`

Runs the `reach`, `contract`, `model`, `pace`, `tokenize`, `produce`, `consume`, `profile`, and `slo` gates in that order.

| Option | Default | Contract |
| --- | --- | --- |
| `--fleet PATH` | required for preflight | Native fleet config JSON. |
| `--ring` | mesh | Tests ring coverage in `consume`. The default tests every eligible ordered pair. |
| `--repeats N` | `1` | Transfer probes per pair. Values below 1 still execute one probe. |
| `--no-kv` | false | Runs `reach`, `contract`, `model`, `pace`, `tokenize`, `profile` and `slo`. |
| `--print-example-config` | false | Prints the packaged annotated config before input-config resolution, then exits. |
| `--print-contract-versions` | false | Prints the versioned JSON interface registry before input-config resolution, then exits. |

Every preflight run requires `--fleet`. Status 1 reports a failed gate or an unreadable or invalid config. Status 2 reports invalid arguments. Gate lines and tables serve human diagnosis; automation consumes the contract registry and versioned artifacts.

## `narwhal-canary`

Runs deterministic correctness probes beside idle or live traffic and writes verdicts, timings, placements and nearby control events to the result stream.

| Option | Default | Contract |
| --- | --- | --- |
| `--base URL` | `http://127.0.0.1:8000` | Router base URL. |
| `--model NAME` | Case-file model | Served model override. |
| `--cases PATH` | required | Exact-output case JSON. |
| `--rate FLOAT` | `0.1` | Canary requests per second. Must be positive. |
| `--duration SECONDS` | required | Arrival duration. Must be positive. |
| `--timeout SECONDS` | `30.0` | Whole-request timeout. |
| `--state-poll SECONDS` | `1.0` | Router-state poll interval. Zero disables polling. |
| `--event-window SECONDS` | `15.0` | Time on either side of a control event included in event scoring. |
| `--markers PATH` | `""` | `""` scores router-observed events; a path also loads operator marker JSONL. |
| `--out PATH` | required | Result JSONL path. |
| `--digest` | false | Retains a run-keyed HMAC-SHA-256 completion digest. |

Copy [`config/canary-cases.example.json`](https://github.com/athrael-soju/Narwhal/blob/main/config/canary-cases.example.json) and replace its model-specific values. `narwhal-canary` compares each returned completion and token sequence with its case, then writes verdicts, timings, token counts, digests, observed control events and a terminal summary under the [canary artifact contracts](09-API-and-Data-Reference.md#canary-artifacts).
