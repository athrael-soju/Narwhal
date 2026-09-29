# `narwhal-profile`

`narwhal-profile --fleet PATH` runs in one of three modes:

| Mode | Operation | Output |
| --- | --- | --- |
| Live sweep | Measures live engines. | The fleet's `profiles.path` |
| Refit | Recomputes time to first token (TTFT) fits from retained samples. | `--out` |
| Merge | Combines separately measured role mixes. | `--out` |

Every mode writes a profile store and a sample sidecar. The sidecar sits at the store's path with its suffix replaced by `.samples.json`.

A live sweep binds each fit to the [verified engine attestation](../configuration/01-Fleet-Schema.md#33-attestation) when the fleet configuration sets `engine_contract`, and to the live process identity if not. The `.samples.json` sidecar stores that evidence with the raw observations.

Refits and merges need fresh output paths. Symlink destinations are rejected in every mode.

## Selection, refitting, and output

| Option                 | Default     | Description                                                                                                                                                             |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--version`            |             | Print the installed distribution version. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--fleet PATH`         | required    | Fleet configuration file; defines engine membership for all three modes. |
| `--only IID`           | every engine | Instance ID (`iid`) of an engine to include in a live sweep; repeatable. |
| `--refit-samples PATH` | omitted     | Sample sidecar holding saved samples bound to process generations. A refit recomputes TTFT from it while retaining the decode fits. |
| `--merge PATH`         | omitted     | Measured profile store, with its matching sample sidecar, to combine; repeat at least twice. |
| `--out PATH`           | omitted     | Fresh profile destination for `--refit-samples` and `--merge`, with a matching `.samples.json` sample sidecar. |
| `--limits PATH`        | requested concurrency points | Generated per-engine `max_num_seqs` limits applied to live decode cohorts. |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout | Positive diagnostic HTTP timeout for every live probe. |
| `--overwrite`          | `false`     | Replace live profile and sample files when the first engine completes. With `--only`, the new store contains the selected profiles. |

Mode rules:

- Live sweeps write to `profiles.path` and reject `--out`.
- `--refit-samples` requires `--out` and samples covering every fleet engine. It cannot combine with `--only` or `--merge`.
- `--merge` requires `--out`. It cannot combine with `--refit-samples`, `--only`, or `--overwrite`.
- `--observation-timeout-s` applies only to live sweeps.

Refit and merge outputs:

- Refits retain the raw samples and process-generation evidence in the new sample sidecar.
- A merge requires matching measurement evidence for every input profile and coverage of every configured engine.
- Each engine, GPU group, role split, and target-role variant must occur once in a merge.
- Keep the source files; the merged sample sidecar records their paths and SHA-256 hashes.

Examples for the three modes:

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

## Prefill and decode sweeps

These options apply to live sweeps. The command validates every supplied sweep value before selecting a mode.

If a working engine exceeds a probe's built-in HTTP timeout, set `--observation-timeout-s` for a diagnostic sweep. The sample sidecar records the value. Serving requests continue to use the deadlines in the fleet configuration.

| Option                      | Default                                   | Description                                                                                                                                                                                                  |
| --------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate prefill lengths. The profiler keeps points within each engine's live `max_model_len`. At least three distinct usable values must remain.                                           |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep. Requires at least two distinct values.                                                                                                                  |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Candidate stream counts; at least two distinct usable values. With `--limits`, the profiler keeps points within each engine's limit. It adds that limit as a point when a candidate exceeds it. |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream; minimum 3. Raise it for large cohorts so the streams overlap.                                                                                                                      |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length; minimum 3. The fit uses each length's median.                                                                                                   |
| `--decode-repeats N`        | `1`                                       | Repetitions per decode input-length and concurrency point; minimum 1.                                                                                                                                        |

## Shared-GPU neighbour traffic

`--colocated` loads the other engines in each target's `shared_device.group` according to their configured roles. Neighbour rates must be finite and positive. Token counts must be positive integers.

Each neighbour's tokenized input plus output must fit its live `max_model_len`.

| Option | Default | Description |
| --- | --- | --- |
| `--colocated` | `false` | Measure each target with traffic on peers in its shared GPU group; requires all five neighbour options. |
| `--neighbour-prefill-rps RATE` | required with `--colocated` | Offered requests per second per prefill neighbour. |
| `--neighbour-decode-rps RATE` | required with `--colocated` | Offered requests per second per decode neighbour. |
| `--neighbour-prefill-tokens N` | required with `--colocated` | Input tokens per prefill neighbour request; each requests one output token. |
| `--neighbour-decode-input-tokens N` | required with `--colocated` | Input tokens per decode neighbour request. |
| `--neighbour-decode-output-tokens N` | required with `--colocated` | Output tokens per decode neighbour request. |

Run a colocated sweep:

```bash
narwhal-profile --fleet fleet.json --colocated \
  --neighbour-prefill-rps 0.5 --neighbour-decode-rps 0.25 \
  --neighbour-prefill-tokens 512 --neighbour-decode-input-tokens 128 \
  --neighbour-decode-output-tokens 32
```

## Sweep validation and completion

With `--colocated`, every neighbour must complete requests during the target's measurement interval. Otherwise the profile's measured role mix is rejected.

The sample sidecar records each neighbour's role, completion count, and achieved rate, plus errors.

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
