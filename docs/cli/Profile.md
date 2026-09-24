# `narwhal-profile`

`narwhal-profile` measures the selected engines and writes their profiles to `profiles.path` from the fleet config, or to `--out` when supplied. It binds each fit to the verified attestation when `engine_contract` is configured, or to the live process identity otherwise, and retains that evidence with raw observations in the `.samples.json` sidecar.

The profiler rejects symlink destinations and requires `--overwrite` to replace existing files. Give each run a new output path to retain prior profiles and samples.

## Selection, refitting, and output

| Option                 | Default     | Contract                                                                                                                                                                |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`         | required    | Fleet config JSON                                                                                                                                                       |
| `--only IID`           | all engines | Repeatable engine selector. With `--reuse`, forces a fresh sweep for the named engine while checking the rest of the fleet.                                          |
| `--reuse PATH`         | omitted     | Read a saved profile and sample pair, check each row against its live generation and effective sweep, and measure missing or changed rows. Requires `--out`.       |
| `--refit-samples PATH` | omitted     | Refit TTFT from saved generation-bound samples while retaining their decode measurements. Requires `--out` and a complete fleet selection.                             |
| `--out PATH`           | omitted     | Write a profile and matching `.samples.json` sidecar to this path. Required for `--reuse` and `--refit-samples`.                                                     |
| `--limits PATH`        | omitted     | Generated per-engine `max_num_seqs` limits from deployment preparation. The profiler bounds each decode cohort before probing.                                          |
| `--overwrite`          | false       | Replaces existing profile and sample files when the first engine completes; with `--only`, the new store contains the selected profiles. Unavailable with `--reuse`. |

`--reuse` requires the same model and requested sweep as the saved pair. The profiler checks live health, context and sequence limits, and generation identity for every fleet engine before carrying its row forward. The output pair records SHA-256 digests of the source pair. Update `profiles.path` to the new output before preflight; the source files remain available for audit.

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

A profiling run aborts when any of these conditions occurs:

- the `--only` selection matches zero configured engines;
- `/health` fails its HTTP 200 check;
- the `/tokenize` response fails `max_model_len` validation;
- engine limits leave fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels;
- the representative prefill fit exceeds 20% mean error;
- the representative prefill fit exceeds 50% worst-point error.

A completed sweep prints `wrote N profile(s) to PATH`; a refit prints `refitted N profile(s) to PATH`.
