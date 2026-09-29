# `narwhal-profile`

`narwhal-profile` measures how your engines perform and writes the profile store that the router uses. It works in one of three modes:

- A **live sweep**, the default, measures the running engines and writes to the fleet's `profiles.path`.
- A **refit** (`--refit-samples`) recalculates the TTFT fits from samples saved by an earlier sweep and keeps the existing decode fits.
- A **merge** (`--merge`) combines profile stores that were measured separately, for example one per role mix.

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
```

Every mode writes two files: the profile store, and a samples file beside it with the same name ending in `.samples.json`. For example, `profiles.json` gets `profiles.samples.json`.

A live sweep ties each fit to the engine it was measured on. If the fleet config has an `engine_contract`, the fit is tied to the engine's [verified attestation](../configuration/01-Fleet-Schema.md#33-attestation). Otherwise it's tied to the live process identity. The samples file keeps this evidence together with the raw observations.

A live sweep won't replace existing files unless you pass `--overwrite`. Refits and merges always need new output paths. No mode writes to a symlink.

## Options

| Option                            | Default                          | Description                                                                                                                                                                                    |
| --------------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`                    | required                         | Fleet config (JSON). Decides which engines are included, in every mode.                                                                                                                        |
| `--only IID`                      | all engines                      | Measure only this engine. Repeat for more than one. Live sweeps only.                                                                                                                          |
| `--refit-samples PATH`            | none                             | Refit TTFT from a saved samples file. The samples must be tied to engine process generations and cover every engine in the fleet. Needs `--out`. Can't be combined with `--only` or `--merge`. |
| `--merge PATH`                    | none                             | A profile store to merge, with its samples file beside it. Give this at least twice. Needs `--out`. Can't be combined with `--refit-samples`, `--only`, or `--overwrite`.                      |
| `--out PATH`                      | none                             | New output path for a refit or merge. The samples file is written beside it. Live sweeps write to `profiles.path` instead.                                                                     |
| `--limits PATH`                   | the requested concurrency points | A generated file of per-engine `max_num_seqs` limits, applied to live decode cohorts.                                                                                                          |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout    | Override the HTTP timeout for the health, tokenization, prefill, decode, metrics, and process-generation probes. Must be positive. Live sweeps only.                                           |
| `--overwrite`                     | off                              | Let a live sweep replace the existing profile and samples files. They're replaced once the first engine finishes. With `--only`, the new store contains only the selected engines.             |
| `--format`                        | `text`                           | Use `json` for [versioned command results](../Command-Results.md).                                                                                                                             |
| `--version`                       |                                  | Print the installed version.                                                                                                                                                                   |

A refit's samples file keeps the raw samples and process-generation evidence from the original.

A merge needs matching measurement evidence for every input profile, and together the inputs must cover every configured engine. Each engine, GPU group, role split, and target-role variant must appear exactly once across the inputs. The merged samples file records the path and SHA-256 hash of every source file, so keep the sources.

## Sweep settings

These options only affect live sweeps. Any values you pass are still validated before the mode is chosen, so an invalid value fails a refit or merge too.

| Option                      | Default                                   | Description                                                                                                                                                                |
| --------------------------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--prefill-lens LIST`       | `256,512,1024,2048,4096,8192,12288,16384` | Prompt lengths to try, separated by commas. Lengths above the `max_model_len` each running engine reports are dropped, and at least three distinct lengths must remain.    |
| `--decode-input-lens LIST`  | `512,4096,8192`                           | Prompt lengths for the decode sweep. At least two distinct values.                                                                                                         |
| `--decode-concurrency LIST` | `1,4,16,48`                               | Numbers of concurrent streams to try. With `--limits`, values above an engine's limit are dropped and the limit itself is added. At least two distinct values must remain. |
| `--decode-tokens N`         | `64`                                      | Tokens generated per decode stream. At least 3. Larger cohorts may need more tokens for their streams to overlap.                                                          |
| `--prefill-repeats N`       | `3`                                       | Runs per prefill length. The fit uses the median for each length, and every raw timing is kept. At least 3.                                                                |
| `--decode-repeats N`        | `1`                                       | Runs per decode point (one input length at one concurrency). At least 1.                                                                                                   |

If a healthy engine is slower than a probe's built-in timeout, raise the timeout with `--observation-timeout-s` for a diagnostic sweep. The value you use is recorded in the samples file. It has no effect on serving, where requests use the deadlines in the fleet config.

## Measuring with neighbors on a shared GPU

Engines that share a GPU slow each other down. `--colocated` measures each engine while the other engines in its `shared_device.group` run traffic that matches their configured roles.

With `--colocated`, you must set all five `--neighbour-*` options. Rates must be finite and positive, and token counts must be positive whole numbers. Each neighbor's tokenized input plus its output must fit within that neighbor's `max_model_len`.

| Option                               | Default                     | Description                                                                        |
| ------------------------------------ | --------------------------- | ---------------------------------------------------------------------------------- |
| `--colocated`                        | off                         | Measure each engine while its shared-GPU peers are under load.                     |
| `--neighbour-prefill-rps RATE`       | required with `--colocated` | Requests per second sent to each prefill neighbor.                                 |
| `--neighbour-decode-rps RATE`        | required with `--colocated` | Requests per second sent to each decode neighbor.                                  |
| `--neighbour-prefill-tokens N`       | required with `--colocated` | Input tokens per prefill neighbor request. Each request asks for one output token. |
| `--neighbour-decode-input-tokens N`  | required with `--colocated` | Input tokens per decode neighbor request.                                          |
| `--neighbour-decode-output-tokens N` | required with `--colocated` | Output tokens per decode neighbor request.                                         |

```bash
narwhal-profile --fleet fleet.json --colocated \
  --neighbour-prefill-rps 0.5 --neighbour-decode-rps 0.25 \
  --neighbour-prefill-tokens 512 --neighbour-decode-input-tokens 128 \
  --neighbour-decode-output-tokens 32
```

Every neighbor has to complete requests while the target engine is being measured. The samples file records each neighbor's role, completed requests, achieved rate, and errors. If a neighbor stalls or fails, the profile's measured role mix is rejected.

## When a sweep stops

Before each request, the profiler checks that the tokenized prompt plus the requested output fits the engine.

A sweep stops with an error if:

- `--only` doesn't match any configured engine.
- An engine's `/health` endpoint doesn't return HTTP 200.
- A `/tokenize` response fails the `max_model_len` check.
- Engine limits leave fewer than three prefill lengths, two decode input lengths, or two concurrency levels.
- The representative prefill fit has a mean error above 20% or a worst-point error above 50%.

When it finishes, each mode prints one line:

```text
wrote N profile(s) to PATH
refitted N profile(s) to PATH
merged N measured profiles to PATH
```