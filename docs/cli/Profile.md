# `narwhal-profile`

`narwhal-profile --version` prints the distribution name and version from the executable's Python environment, then exits with status 0. [Installation](../Install-from-PyPI.md) covers version reporting from a source checkout.

`narwhal-profile` measures the selected engines and writes their profiles to `profiles.path` from the fleet config.

When `engine_contract` is configured, each fit is bound to the [verified engine attestation](../configuration/01-Fleet-Schema.md#33-attestation). Otherwise, it is bound to the live process identity. The `.samples.json` sidecar retains that evidence and the raw observations.

The profiler rejects symlink destinations and requires `--overwrite` to replace existing files. Give each run a new output path to retain prior profiles and samples.

## Selection, refitting, and output

| Option                 | Default     | Contract                                                                                                                                                                |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`         | required    | Fleet config JSON                                                                                                                                                       |
| `--only IID`           | all engines | Repeatable engine selector                                                                                                                                              |
| `--refit-samples PATH` | omitted     | Refit TTFT from saved generation-bound samples while retaining their decode measurements. Requires `--out` and includes every engine in the fleet; cannot be used with `--only`. |
| `--out PATH`           | omitted     | Fresh profile path for `--refit-samples`; the command writes a matching `.samples.json` sidecar.                                                                        |
| `--limits PATH`        | omitted     | Generated per-engine `max_num_seqs` limits from deployment preparation. The profiler bounds each decode cohort before probing.                                          |
| `--overwrite`          | false       | Replaces existing profile and sample files when the first engine completes; with `--only`, the new store contains the selected profiles.                              |

## Prefill and decode sweeps

| Option                      | Default                                   | Contract                                                                                                                                                                                                     |
| --------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate lengths. The profiler keeps points within each engine's live `max_model_len` and requires at least three distinct usable values.                                                   |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep. Requires at least two distinct values.                                                                                                                  |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Candidate stream counts. With `--limits`, the profiler keeps points within each engine's limit and adds that limit as a point when a candidate exceeds it. At least two distinct usable values are required. |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream. Minimum 3. Larger cohorts may require more tokens to overlap.                                                                                                                      |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length. The fit uses each length's median and retains every raw timing. Minimum 3.                                                                                                   |

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

A completed sweep prints `wrote N profile(s) to PATH`; a refit prints `refitted N profile(s) to PATH`.
