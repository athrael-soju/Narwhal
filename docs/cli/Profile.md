# `narwhal-profile`

`narwhal-profile --fleet PATH` runs in one of three modes:

| Mode | Operation | Output |
| --- | --- | --- |
| Live sweep | Measures live engines. | The fleet's `profiles.path` |
| Refit | Recomputes time to first token (TTFT) fits from retained samples. | `--out` |
| Merge | Combines separately measured role mixes. | `--out` |

Every mode writes a profile store plus a sample sidecar at the store's path with its suffix replaced by `.samples.json`.

| Fleet configuration     | A live sweep binds each fit to                                                          |
| ----------------------- | --------------------------------------------------------------------------------------- |
| Sets `engine_contract`  | The [verified engine attestation](../configuration/01-Fleet-Schema.md#33-attestation)   |
| Omits `engine_contract` | The live process identity                                                               |

The `.samples.json` sidecar stores that evidence with the raw observations.

Every mode rejects symlink destinations.

## Selection, refitting, and output

| Option                 | Default     | Description                                                                                                                                                             |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--version`            |             | Print the installed distribution version. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--fleet PATH`         | required    | Fleet configuration file that defines engine membership for every mode. |
| `--only IID`           | every engine | Repeatable engine `iid` to include in a live sweep. |
| `--refit-samples PATH` | omitted     | Sample sidecar for a refit, which recomputes TTFT and keeps the decode fits. |
| `--merge PATH`         | omitted     | Measured profile store to combine with its matching sample sidecar, repeated at least twice. |
| `--out PATH`           | omitted     | Fresh profile destination for `--refit-samples` and `--merge`, with a matching `.samples.json` sample sidecar. |
| `--limits PATH`        | requested concurrency points | Generated per-engine `max_num_seqs` limits applied to live decode cohorts. |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout | Positive diagnostic HTTP timeout for every live probe, recorded in the sample sidecar. |
| `--overwrite`          | `false`     | Replace live profile and sample files when the first engine completes. |

Mode rules:

- Live sweeps reject `--out`.
- `--refit-samples` requires `--out` and samples covering every fleet engine.
- `--refit-samples` is mutually exclusive with `--only` and `--merge`.
- `--merge` requires `--out`.
- `--merge` is mutually exclusive with `--only` and `--overwrite`.
- `--observation-timeout-s` applies only to live sweeps.
- With `--only`, `--overwrite` writes a store holding the selected profiles.

Refit and merge outputs:

- Refits retain the raw samples and process-generation evidence in the new sample sidecar.
- A merge requires matching measurement evidence for every input profile and coverage of every configured engine.
- Each engine, GPU group, role split, and target-role variant must occur once in a merge.
- The merged sample sidecar records each source file's path and SHA-256 hash.
- Keep the source files.

Examples for the three modes:

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

## Prefill and decode sweeps

Every mode validates these live-sweep options.

If a working engine exceeds a probe's built-in HTTP timeout, set `--observation-timeout-s` for a diagnostic sweep.

| Option                      | Default                                   | Description                                                                                          | Valid values                        |
| --------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate prefill lengths, filtered to each engine's live `max_model_len`.           | At least three distinct usable values |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep.                                                 | At least two distinct values        |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Candidate stream counts, with candidates above an engine's `--limits` value replaced by that value.  | At least two distinct usable values |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream.                                                                            | At least 3, higher for large cohorts |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length.                                                                      | At least 3                          |
| `--decode-repeats N`        | `1`                                       | Repetitions per decode input-length and concurrency point.                                           | At least 1                          |

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

Run a colocated sweep:

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

- the `--only` selection matches zero configured engines;
- `/health` fails its HTTP 200 check;
- the `/tokenize` response fails `max_model_len` validation;
- engine limits leave fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels;
- the representative prefill fit exceeds 20% mean error or 50% worst-point error.

| Mode | Success output |
| --- | --- |
| Live sweep | `wrote N profile(s) to PATH` |
| Refit | `refitted N profile(s) to PATH` |
| Merge | `merged N measured profiles to PATH` |
