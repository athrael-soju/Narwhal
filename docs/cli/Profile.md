---
description: Measure the prefill and decode cost model of each engine with narwhal-profile sweeps.
---

# `narwhal-profile`

`narwhal-profile --fleet PATH` measures the prefill and decode costs of vLLM and SGLang engines and writes them as router profiles. It runs in one of three modes:

| Mode | Operation | Output |
| --- | --- | --- |
| Live sweep | Measures the live engines. | The fleet's `profiles.path`, or `--out` |
| Refit | Recomputes the cold and warm time to first token (TTFT) fits from a sample sidecar and keeps the decode fits. | `--out` |
| Merge | Combines profile stores. | `--out` |

Every mode writes a profile store, and a sample sidecar at the store's path with the suffix `.samples.json`. Neither file may be a symlink.

A live sweep binds each fit to the engine process it measured. With `engine_contract` set in the fleet configuration, the binding is the `launch_digest` of the [verified engine attestation](../configuration/01-Fleet-Schema.md#attestation), or its `attestation_digest` when the attestation carries no launch evidence. Without `engine_contract`, the binding is the engine version and process start time.

A live sweep measures engines on separate devices at the same time. Engines in one `shared_device.group`, and all engines under `--colocated`, take turns.

## SGLang pairs

On an SGLang fleet, the sweep measures both roles of each engine and has a peer engine serve the other leg of each request. It needs `engine.connector` `mooncake`, at least two engines in the fleet and each engine's attestation sidecar, and it rejects `--colocated`. Before each engine's prefill, decode and warm prefill sweeps, the sweep switches the engine and its peer into opposite roles with `POST /pd_role_switch` and empties both caches with `POST /flush_cache`. The sample sidecar names each engine's peer in `peer`.

The sweep pairs the selected engines in fleet order and measures the pairs at the same time, each engine serving as the other's peer. With an odd count, the last three engines form one group in which each engine's peer is the next one. A single selected engine takes the next engine in the fleet as its peer. When any selected engine sets `shared_device`, the sweep measures the selected engines one at a time, and each engine's peer is the next selected engine.

## Selection, refitting, and output

These options select engines, choose the mode, and set the output files:

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--fleet PATH` | required | Fleet configuration file that defines engine membership for every mode. |
| `--only IID` | every engine | Engine `iid` to include in a live sweep, repeatable. |
| `--refit-samples PATH` | optional | Sample sidecar to refit. |
| `--merge PATH` | optional | Profile store to combine, read with its matching sample sidecar, repeatable. |
| `--out PATH` | optional | New profile store, written with a matching sample sidecar. Required for `--refit-samples` and `--merge`. A live sweep writes here in place of `profiles.path`. |
| `--limits PATH` | requested concurrency points | [`profiling-limits.json`](../deploy/06-Profile-and-Preflight.md) with each engine's `runtime.max_sequences`, above which the profiler drops decode concurrency points. |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout | Finite, positive HTTP timeout for every live probe. |
| `--overwrite` | `false` | Replace the live profile store and sample sidecar, starting when the first engine completes. |

The mode options depend on each other:

| Option | Requires | Mutually exclusive with |
| --- | --- | --- |
| `--refit-samples` | `--out`, and samples covering every fleet engine | `--only`, `--merge` |
| `--merge` | `--out` | `--only`, `--overwrite`, `--refit-samples` |
| `--out` | A store path and sidecar path that do not exist yet | `--overwrite` |
| `--observation-timeout-s` | A live sweep | `--refit-samples`, `--merge` |

With `--only`, `--overwrite` writes a store that holds only the selected engines' profiles.

A refit needs each engine's saved generation evidence, and writes the raw samples and that evidence to its new sample sidecar.

A merge needs measurement evidence in each source's sample sidecar that matches every profile in the source store. The merged stores must cover every configured engine. A later store replaces an earlier store's row for the same combination of engine, GPU group, role split and target role. The merge leaves out rows for engines outside the fleet. A merged store can be a source. The merged sample sidecar records each source file's path and SHA-256 hash, so keep the source files.

Run each mode with:

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

## Prefill and decode sweeps

Every mode validates these options:

| Option | Default | Description | Valid values |
| --- | :---: | --- | --- |
| `--prefill-lens LIST` | `256,700,1024,1300,2300,4096,4300,8300,12300,16300` | Comma-separated candidate prefill lengths. Each engine keeps the lengths that fit its live context limit with one output token. | At least three distinct positive values, and three that fit each engine |
| `--decode-input-lens LIST` | `512,4096,8192` | Comma-separated prompt lengths for the decode sweep. Each engine keeps the lengths that fit its live context limit with `--decode-tokens`. | At least two distinct positive values, and two that fit each engine |
| `--decode-concurrency LIST` | `1,4,16,48` | Candidate stream counts. With `--limits`, each engine drops the counts above its limit and adds the limit. | At least two distinct values of at least 1, and two that remain for each engine |
| `--decode-tokens N` | `64` | Tokens per decode stream. | At least 3 |
| `--cached-prefix-lens LIST` | `2048,4096,8192` | Comma-separated cached prefix lengths for the warm prefill sweep. | Positive, with at least two distinct values and five prefix and suffix cases |
| `--cached-suffix-lens LIST` | `700,1300,2600` | Comma-separated uncached suffix lengths for the warm prefill sweep. | Positive, with at least two distinct values |
| `--prefill-repeats N` | `3` | Repetitions per prefill length. | At least 3 |
| `--decode-repeats N` | `1` | Repetitions per decode input length and concurrency. | At least 1 |

The live context limit comes from `/tokenize` on vLLM. On SGLang, it is the lower of `context_length` and `max_req_input_len` on `/server_info`.

The [warm prefill sweep](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix) runs when the engine exports a prefix-cache hit counter, `vllm:prefix_cache_hits_total` on vLLM or `sglang:cached_tokens_total` on SGLang. When a [warm sweep condition](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix) fails, the profile keeps cold prefill only, and the sample sidecar records the reason in `cached_prefill.reason`.

## Shared-GPU neighbour traffic

With `--colocated`, the sweep measures each target while the other engines in its `shared_device.group` serve paced traffic for their configured roles. Every target needs a `shared_device` group with at least one neighbour. Each neighbour's tokenized input plus output must fit its live context limit. SGLang fleets reject `--colocated`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--colocated` | `false` | Measure each target under traffic from the peers in its `shared_device.group`. | |
| `--neighbour-prefill-rps RATE` | required with `--colocated` | Offered requests per second per prefill neighbour. | Finite, positive |
| `--neighbour-decode-rps RATE` | required with `--colocated` | Offered requests per second per decode neighbour. | Finite, positive |
| `--neighbour-prefill-tokens N` | required with `--colocated` | Input tokens per prefill neighbour request, each asking for one output token. | Positive integer |
| `--neighbour-decode-input-tokens N` | required with `--colocated` | Input tokens per decode neighbour request. | Positive integer |
| `--neighbour-decode-output-tokens N` | required with `--colocated` | Output tokens per decode neighbour request. | Positive integer |

```bash
narwhal-profile --fleet fleet.json --colocated \
  --neighbour-prefill-rps 0.5 --neighbour-decode-rps 0.25 \
  --neighbour-prefill-tokens 512 --neighbour-decode-input-tokens 128 \
  --neighbour-decode-output-tokens 32
```

The sample sidecar records each neighbour's role, completion count, achieved rate and error in `colocated_load`.

## Completion and exit codes

On success, each mode ends with this line and exits with code `0`:

| Mode | Success output |
| --- | --- |
| Live sweep | `wrote N profile(s) to PATH` |
| Refit | `refitted N profile(s) to PATH` |
| Merge | `merged N measured profiles to PATH` |

A live sweep writes the profile store and sample sidecar after each engine completes, so a failure keeps the profiles of engines that finished. The sidecar also records the failing engine's error. In text mode, the profiler stops with exit code `1` when:

- an engine's `/health` returns a status other than 200
- a probe, including the context-limit probe, fails or returns an incomplete response
- an engine serves prompt tokens from its prefix cache during the prefill sweep, the decode sweep, or a cold control of the warm prefill sweep
- the decode fit error exceeds `profiles.max_decode_fit_mape`, its leave-one-cell-out error exceeds `profiles.max_decode_cv_mape`, or its measured domain is a single point on either axis
- a neighbour fails a request or completes zero requests during the target's measurement
- a neighbour's input plus output exceeds the neighbour's context limit
- the prefill fit exceeds 20% mean error or 50% worst-point error
- an engine's attestation fails verification, or its process changes during its measurement
- on SGLang, `POST /pd_role_switch` fails or the engine refuses the switch, or `POST /flush_cache` fails or returns a status other than 200

It stops with exit code `2` when:

- the arguments or the fleet configuration fail validation
- `--only` matches zero configured engines
- an output exists without `--overwrite`, or a refit or merge output exists
- a fleet with `engine_contract` has a profiled engine without an attestation URL
- an SGLang fleet uses a connector other than `mooncake`, has one engine, lacks an attestation URL, or sets `--colocated`
- a `--colocated` target has no neighbour in its group
- an engine's context limit leaves fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels
- a refit or merge input fails its checks

With `--format json`, the exit code follows the [command result](../Command-Results.md) status.
