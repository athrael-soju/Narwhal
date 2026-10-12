---
description: Measure the prefill and decode cost model of each engine with narwhal-profile sweeps.
---

# `narwhal-profile`

`narwhal-profile --fleet PATH` runs in one of three modes:

| Mode | Operation | Output |
| --- | --- | --- |
| Live sweep | Measures live engines. | The fleet's `profiles.path` |
| Refit | Recomputes cold and warm time to first token (TTFT) fits from retained samples, with the decode fits kept. | `--out` |
| Merge | Combines separately measured role mixes. | `--out` |

Every mode writes a profile store, plus a sample sidecar at the store's path with its suffix replaced by `.samples.json`, and rejects symlink destinations.

A live sweep binds each fit to the engine it measured. When the fleet configuration sets `engine_contract`, the binding is the `launch_digest` of the [verified engine attestation](../configuration/01-Fleet-Schema.md#attestation) when the attestation carries launch evidence, and its `attestation_digest` otherwise. Without `engine_contract`, the binding is the live process identity.

A live sweep measures engines on separate devices at the same time. Engines in one `shared_device.group`, and all engines under `--colocated`, take turns.

### SGLang pairs

On an SGLang fleet, the sweep measures both roles of each engine, with a peer engine serving the other leg of each request. It needs `engine.connector` `mooncake` and at least two engines in the fleet, and rejects `--colocated`. Before each leg, the sweep switches both engines into their roles and empties their caches with `POST /flush_cache`. The evidence names each engine's peer in `peer`.

The sweep pairs the selected engines in fleet order and measures the pairs at the same time, each engine serving as the other's peer. With an odd count, the last three engines form one group in which each engine's peer is the next one. A single selected engine takes the next engine in the fleet as its peer. When any selected engine sets `shared_device`, the sweep measures the selected engines one at a time, and each engine's peer is the next selected engine.

## Selection, refitting, and output

These options select engines, choose the mode, and set the output files:

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--fleet PATH` | required | Fleet configuration file that defines engine membership for every mode. |
| `--only IID` | every engine | Repeatable engine `iid` to include in a live sweep. |
| `--refit-samples PATH` | optional | Sample sidecar for a refit of cold and warm prefill. |
| `--merge PATH` | optional | Measured profile store to combine with its matching sample sidecar, repeated at least twice. |
| `--out PATH` | optional | Fresh profile destination for `--refit-samples` and `--merge`, with a matching `.samples.json` sample sidecar. |
| `--limits PATH` | requested concurrency points | Generated per-engine `max_num_seqs` limits applied to live decode cohorts. |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout | Positive diagnostic HTTP timeout for every live probe. |
| `--overwrite` | `false` | Replace live profile and sample files when the first engine completes. |

The mode options depend on each other:

| Option | Requires | Mutually exclusive with |
| --- | --- | --- |
| `--refit-samples` | `--out` and samples covering every fleet engine | `--only`, `--merge` |
| `--merge` | `--out` and at least two profile stores | `--only`, `--overwrite`, `--refit-samples` |
| `--out` | `--refit-samples` or `--merge` | |
| `--observation-timeout-s` | A live sweep | `--refit-samples`, `--merge` |

With `--only`, `--overwrite` writes a store that holds the selected profiles.

A refit keeps the raw samples and process-generation evidence in its new sample sidecar.

A merge needs matching measurement evidence for every input profile and must cover every configured engine, with each engine, GPU group, role split and target-role variant occurring once. The merged sample sidecar records each source file's path and SHA-256 hash, so keep the source files.

Run each mode with:

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

## Prefill and decode sweeps

All modes validate these options:

| Option | Default | Description | Valid values |
| --- | :---: | --- | --- |
| `--prefill-lens LIST` | `256,700,1024,1300,2300,4096,4300,8300,12300,16300` | Comma-separated candidate prefill lengths, filtered to each engine's live `max_model_len`. | At least three distinct usable values |
| `--decode-input-lens LIST` | `512,4096,8192` | Comma-separated prompt lengths for the decode sweep. | At least two distinct values |
| `--decode-concurrency LIST` | `1,4,16,48` | Candidate stream counts, with candidates above an engine's `--limits` value replaced by that value. | At least two distinct usable values |
| `--decode-tokens N` | `64` | Tokens per decode stream. | At least 3 |
| `--cached-prefix-lens LIST` | `2048,4096,8192` | Comma-separated cached prefix lengths for the warm prefill sweep. | At least two distinct values and five cases with `--cached-suffix-lens` |
| `--cached-suffix-lens LIST` | `700,1300,2600` | Comma-separated uncached suffix lengths for the warm prefill sweep. | At least two distinct values |
| `--prefill-repeats N` | `3` | Repetitions per prefill length. | At least 3 |
| `--decode-repeats N` | `1` | Repetitions per decode input-length and concurrency point. | At least 1 |

Each engine's [decode sweep](../measure/01-Profile.md#decode-sweep) warms each concurrency with one unmeasured cohort. A decode cohort whose first stream finishes before its last stream joins reruns once with more tokens per stream.

## Shared-GPU neighbour traffic

With `--colocated`, each target is measured under traffic from its neighbours. Each neighbour's tokenized input plus output must fit its live `max_model_len`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--colocated` | `false` | Measure each target under traffic that peers in its `shared_device.group` send by their configured roles. | |
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

## Sweep validation and completion

With `--colocated`, the sample sidecar records each neighbour's role, completion count, achieved rate, and errors. A profile's measured role mix is rejected when a neighbour completes zero requests during the target's measurement interval.

A profiling run aborts when any of these conditions occurs:

- the `--only` selection matches zero configured engines
- `/health` fails its HTTP 200 check
- the context-limit response fails `max_model_len` validation: `/tokenize` on vLLM, `/server_info` on SGLang
- engine limits leave fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels
- the representative prefill fit exceeds 20% mean error or 50% worst-point error
- the decode fit error exceeds `profiles.max_decode_fit_mape`, or its leave-one-cell-out error exceeds `profiles.max_decode_cv_mape`
- an engine serves prompt tokens from its prefix cache during the prefill sweep, the decode sweep, or a cold control in the warm prefill sweep
- on SGLang, `POST /flush_cache` fails or returns a status other than 200

On success, each mode prints:

| Mode | Success output |
| --- | --- |
| Live sweep | `wrote N profile(s) to PATH` |
| Refit | `refitted N profile(s) to PATH` |
| Merge | `merged N measured profiles to PATH` |
