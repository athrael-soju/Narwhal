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

Every mode writes a profile store plus a sample sidecar at the store's path with its suffix replaced by `.samples.json`.

When the fleet configuration sets `engine_contract`, a live sweep binds each fit to the `launch_digest` of the [verified engine attestation](../configuration/01-Fleet-Schema.md#attestation) when the attestation carries launch evidence, otherwise to its `attestation_digest`. When the configuration omits `engine_contract`, a live sweep binds each fit to the live process identity.

A live sweep measures engines on separate devices at the same time. It measures engines in one `shared_device.group`, and all engines under `--colocated`, one after another.

On an SGLang fleet, a live sweep measures engines one after another, each with the next engine in the fleet as its peer. For each leg it switches the engine and its peer to prefill and decode. It requires `engine.connector` `mooncake` and at least two engines, and rejects `--colocated`. The engine evidence records the peer in `peer`.

Every mode rejects symlink destinations.

## Selection, refitting, and output

These options select engines, choose the mode, and set the output files.

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

Mode rules:

| Option | Requires | Mutually exclusive with |
| --- | --- | --- |
| `--refit-samples` | `--out` and samples covering every fleet engine | `--only`, `--merge` |
| `--merge` | `--out` and at least two profile stores | `--only`, `--overwrite`, `--refit-samples` |
| `--out` | `--refit-samples` or `--merge` | |
| `--observation-timeout-s` | A live sweep | `--refit-samples`, `--merge` |

With `--only`, `--overwrite` writes a store holding the selected profiles.

Refit and merge outputs:

- Refits retain the raw samples and process-generation evidence in the new sample sidecar.
- A merge requires matching measurement evidence for every input profile.
- A merge requires coverage of every configured engine.
- Each engine, GPU group, role split, and target-role variant must occur once in a merge.
- The merged sample sidecar records each source file's path and SHA-256 hash.
- Keep the source files.

Commands for each mode:

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

A decode cohort whose first stream finishes before its last stream joins reruns once with more tokens per stream.

## Shared-GPU neighbour traffic

Each neighbour's tokenized input plus output must fit its live `max_model_len`.

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

With `--colocated`, a profile's measured role mix is rejected when a neighbour completes zero requests during the target's measurement interval.

The sample sidecar records each neighbour's role, completion count, achieved rate, and errors.

A profiling run aborts when any of these conditions occurs:

- the `--only` selection matches zero configured engines
- `/health` fails its HTTP 200 check
- the `/tokenize` response fails `max_model_len` validation
- engine limits leave fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels
- the representative prefill fit exceeds 20% mean error or 50% worst-point error
- the decode fit error exceeds `profiles.max_decode_fit_mape`, or its leave-one-cell-out error exceeds `profiles.max_decode_cv_mape`
- an engine serves prompt tokens from its prefix cache during the prefill sweep, the decode sweep, or a cold control in the warm prefill sweep

On success, each mode prints:

| Mode | Success output |
| --- | --- |
| Live sweep | `wrote N profile(s) to PATH` |
| Refit | `refitted N profile(s) to PATH` |
| Merge | `merged N measured profiles to PATH` |
