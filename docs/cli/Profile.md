# `narwhal-profile`

`narwhal-profile --fleet PATH` measures live engines into the fleet's `profiles.path`, refits TTFT from retained samples, or merges separately measured role mixes. Every mode writes a profile store and a sidecar at the same path with its suffix replaced by `.samples.json`.

Live sweeps bind each fit to the [verified engine attestation](../configuration/01-Fleet-Schema.md#33-attestation) when `engine_contract` is configured, or to the live process identity otherwise, and retain that evidence with the raw observations in the `.samples.json` sidecar.

Live sweeps replace existing outputs when `--overwrite` is supplied. Refits and merges require fresh output paths, and every mode rejects symlink destinations.

## Selection, refitting, and output

| Option                 | Default     | Contract                                                                                                                                                                |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--version`            |             | Print the installed distribution version. |
| `--fleet PATH`         | required    | Fleet config JSON; defines engine membership for all three modes. |
| `--only IID`           | all engines | Repeatable engine selector for live sweeps. |
| `--refit-samples PATH` | omitted     | Refit TTFT from saved generation-bound samples while retaining decode fits. Requires `--out` and samples covering every fleet engine; exclusive with `--only` and `--merge`. |
| `--merge PATH`         | omitted     | Repeat at least twice to combine measured profile stores and their matching sidecars. Requires `--out`; exclusive with `--refit-samples`, `--only` and `--overwrite`. |
| `--out PATH`           | omitted     | Fresh profile destination required for `--refit-samples` and `--merge`, with a matching `.samples.json` sidecar. Live sweeps use `profiles.path`. |
| `--limits PATH`        | requested concurrency points | Generated per-engine `max_num_seqs` limits applied to live decode cohorts. |
| `--overwrite`          | false       | Replace live profile and sample files when the first engine completes; with `--only`, the new store contains the selected profiles. Refits always require fresh outputs. |

Refits retain the raw samples and process-generation evidence in the new sidecar. Merges require matching measurement evidence for every input profile and coverage of every configured engine; each engine, GPU group, role split and target-role variant must occur once. The merged sidecar records the source profile and sample paths with their SHA-256 hashes, so retain those source files.

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

## Prefill and decode sweeps

These options apply to live measurement. The command validates supplied sweep values before selecting a mode; refits use the retained samples and merges use the source stores.

| Option                      | Default                                   | Contract                                                                                                                                                                                                     |
| --------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate lengths. The profiler keeps points within each engine's live `max_model_len` and requires at least three distinct usable values.                                                   |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep. Requires at least two distinct values.                                                                                                                  |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Candidate stream counts. With `--limits`, the profiler keeps points within each engine's limit and adds that limit as a point when a candidate exceeds it. At least two distinct usable values are required. |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream. Minimum 3. Larger cohorts may require more tokens to overlap.                                                                                                                      |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length. The fit uses each length's median and retains every raw timing. Minimum 3.                                                                                                   |
| `--decode-repeats N`        | `1` | Repetitions per decode input-length/concurrency point. Minimum 1. |

## Shared-GPU neighbour traffic

`--colocated` loads the other engines in each target's `shared_device.group` according to their configured roles. Supply all five neighbour options with finite positive rates and positive integer token counts. Each neighbour's tokenised input plus output must fit its live `max_model_len`.

| Option | Default | Contract |
| --- | --- | --- |
| `--colocated` | false | Measure each target with traffic on peers in its shared GPU group; requires all five neighbour options. |
| `--neighbour-prefill-rps RATE` | required with `--colocated` | Offered requests per second per prefill neighbour. |
| `--neighbour-decode-rps RATE` | required with `--colocated` | Offered requests per second per decode neighbour. |
| `--neighbour-prefill-tokens N` | required with `--colocated` | Input tokens per prefill neighbour request; each requests one output token. |
| `--neighbour-decode-input-tokens N` | required with `--colocated` | Input tokens per decode neighbour request. |
| `--neighbour-decode-output-tokens N` | required with `--colocated` | Output tokens per decode neighbour request. |

```bash
narwhal-profile --fleet fleet.json --colocated \
  --neighbour-prefill-rps 0.5 --neighbour-decode-rps 0.25 \
  --neighbour-prefill-tokens 512 --neighbour-decode-input-tokens 128 \
  --neighbour-decode-output-tokens 32
```

## Profiling sweep stop conditions

Before each completion, the profiler verifies that the actual tokenised input plus the requested output fits the engine.

With `--colocated`, every neighbour must complete requests during the target's
measurement interval. The sample sidecar records each neighbour's role,
completion count, achieved rate and errors. A stalled or failed neighbour
rejects that profile's measured role mix.

A profiling run aborts when any of these conditions occurs:

- the `--only` selection matches zero configured engines;
- `/health` fails its HTTP 200 check;
- the `/tokenize` response fails `max_model_len` validation;
- engine limits leave fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels;
- the representative prefill fit exceeds 20% mean error;
- the representative prefill fit exceeds 50% worst-point error.

A completed sweep prints `wrote N profile(s) to PATH`; a refit prints `refitted N profile(s) to PATH`; a merge prints `merged N measured profiles to PATH`.
