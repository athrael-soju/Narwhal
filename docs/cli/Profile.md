# `narwhal-profile`

`narwhal-profile` measures every selected engine and writes the resulting profile store to `profiles.path` from the fleet config. Raw observations are written beside the store with the profile path suffix replaced by `.samples.json`.

Use a different output path for each run when previous profiling evidence must remain available. Existing destinations require `--overwrite`, and symlink destinations are rejected.

## Selection, refitting, and output

| Option                 | Default     | Contract                                                                                                                                                                |
| ---------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`         | required    | Fleet config JSON                                                                                                                                                       |
| `--only IID`           | all engines | Repeatable engine selector                                                                                                                                              |
| `--refit-samples PATH` | omitted     | Refit TTFT from a saved `.samples.json` file while retaining its decode measurements. Requires `--out` and a complete fleet selection.                                  |
| `--out PATH`           | omitted     | Fresh profile path for `--refit-samples`; the command writes a matching `.samples.json` sidecar.                                                                        |
| `--limits PATH`        | omitted     | Generated per-engine `max_num_seqs` limits from deployment preparation. The profiler bounds each decode cohort before probing.                                          |
| `--overwrite`          | false       | Starts a new profile/sample pair. Existing destinations are replaced when the first engine completes. With `--only`, the new store contains only the selected profiles. |

## Prefill and decode sweeps

| Option                      | Default                                   | Contract                                                                                                                                                                                                     |
| --------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Comma-separated candidate lengths. The profiler keeps points within each engine's live `max_model_len` and requires at least three distinct usable values.                                                   |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Comma-separated prompt lengths for the decode sweep. Requires at least two distinct values.                                                                                                                  |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Candidate stream counts. With `--limits`, the profiler keeps points within each engine's limit and adds that limit as a point when a candidate exceeds it. At least two distinct usable values are required. |
| `--decode-tokens N`         | `64`                                      | Tokens per decode stream. Minimum 3. Larger cohorts may require more tokens to overlap.                                                                                                                      |
| `--prefill-repeats N`       | `3`                                       | Repetitions per prefill length. The fit uses each length's median and retains every raw timing. Minimum 3.                                                                                                   |

## Profiling acceptance conditions

Before each completion, the profiler verifies that the actual tokenised input plus the requested output fits the engine.

A profiling run aborts when any of these conditions occurs:

- every `--only` value is absent from the configured engines;
- an engine fails `/health`;
- `/tokenize` omits a valid `max_model_len`;
- the live engine limit leaves too few sweep points;
- the representative prefill fit exceeds 20% mean error;
- the representative prefill fit exceeds 50% worst-point error.

A successful run ends with:

`wrote N profile(s) to PATH`
