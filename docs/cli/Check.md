# `narwhal-check`

`narwhal-check --fleet PATH` runs these deployment preflight gates in order: `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, `produce`, `consume`, `slo`.

Text output prints one line per gate: `ok`, `FAIL`, `SKIP`, or `WARN`.

## Options

General options:

| Option                      | Default | Description                                                                                                                     |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | optional | Fleet configuration file for preflight, calibration, and evidence verification.                                                |
| `--format`                  | `text`  | `text` or `json` for [versioned command results](../Command-Results.md).                                                        |
| `--version`                 |         | Print the installed version.                                                                                                    |
| `--print-example-config`    | off     | Print the packaged, annotated config and exit.                                                                                  |
| `--print-contract-versions` | off     | Print the versioned JSON interface registry and exit.                                                                           |

`--print-example-config` takes precedence over `--print-contract-versions`.

Transfer probe options for preflight:

| Option        | Default | Description                                 |
| ------------- | ------- | ------------------------------------------- |
| `--repeats N` | 1       | Probes per pair.                            |
| `--ring`      | off     | Test `consume` in a ring.                   |
| `--no-kv`     | off     | Skip `produce` and `consume`.               |

KV evidence options:

| Option                   | Default | Description                                                                                                  | Mutually exclusive with                   |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| `--evidence-out PATH`    | optional | Save full-mesh KV evidence to a new JSON file.                                                              | `--ring`, `--no-kv`, `--verify-evidence`  |
| `--verify-evidence PATH` | optional | Check saved evidence against the current fleet and profile hashes. | `--evidence-out`                          |

`--verify-evidence` checks the engines' live process generations.

Both evidence options need an `engine_contract` in the fleet config.

First-token calibration options:

| Option                            | Default  | Description                                                                 | Valid values                                                                                      |
| --------------------------------- | -------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `--calibrate-first-token`         | off      | Measure fresh KV handoffs at each input length and report a first-token deadline. |                                                                                             |
| `--input-tokens LIST`             | optional | Comma-separated input lengths.                                              | Each leaves room for at least one output token within both engines' live context limits.         |
| `--samples N`                     | 100      | Fresh handoffs per pair and input length.                                   |                                                                                                   |
| `--observation-timeout-s SECONDS` | optional | First-output wait.                                                          | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`.                     |
| `--calibration-out PATH`          | optional | Raw samples and group summaries file.                                       | New JSON file under `runs/`.                                                                      |

`--calibrate-first-token` rules:

| Rule                     | Options                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------ |
| Requires                 | `--input-tokens`, `--observation-timeout-s`, `--calibration-out`                     |
| Mutually exclusive with  | The evidence options, `--ring`, `--no-kv`, a `--repeats` value other than 1          |
| Required by              | `--samples`, `--input-tokens`, `--observation-timeout-s`, `--calibration-out`        |
| `--input-tokens` includes | The longest input the service admits                                                |

## What a KV probe does

| Property                     | Value                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| Prompt                       | `"benchmark " * 64`, tokenized for the model under test                                     |
| `consume` input              | A KV handoff from an eligible peer                                                          |
| `consume` pairs              | Every eligible ordered pair, or a ring with `--ring`                                        |
| Output tokens                | Up to four                                                                                  |
| Output flags                 | vLLM's `min_tokens` and `ignore_eos`                                                        |
| Cache salt per probe         | One unique `cache_salt` shared by both legs                                                 |
| Cache salt per `pace` repeat | Fresh                                                                                       |
| First-token deadline         | `engine.first_token_timeout_s`                                                              |
| Probe deadline               | `serving.request_timeout_s`                                                                 |
| Probe success                | A stream end before the probe deadline                                                      |
| Missed first-token deadline  | Elapsed time reported                                                                       |
| Transfer after a miss        | Unconfirmed                                                                                 |
| Next step after a miss       | Calibration                                                                                 |

## Recording and verifying KV evidence

An engine restart or a config change invalidates recorded evidence.

Record and verify evidence:

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

`--evidence-out` rejects evidence when:

- an engine's process generation changes between pairs or repeats
- an engine has lost its profile binding by the final identity check
- the fleet or profile files differ from the hashes taken at the start of the run.

A `--verify-evidence` run:

- repeats these checks against the current engines
- runs preflight's fresh transfer probes.

## First-token calibration

Run the [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before recording evidence for a fleet.

The `--calibration-out` file holds:

- the raw attempts
- the p99 and maximum for each group
- the candidate deadline
- the engines' process generations.

| Scope                 | Timeout                     |
| --------------------- | --------------------------- |
| Wait for first output | `--observation-timeout-s`   |
| Whole attempt         | `serving.request_timeout_s` |

| Handoff property                          | Value                                                               |
| ----------------------------------------- | ------------------------------------------------------------------- |
| Forced output tokens                      | Four                                                                |
| Forced output token cap                   | The producer's and consumer's live context limits                   |
| Output tokens at a `max_model_len - 1` target | One                                                             |
| Successful sample                         | A produced token                                                    |
| Successful stream end                     | Valid                                                               |

The artifact is valid evidence when all of these hold:

- every group has at least 100 completed attempts
- the attempt numbers are distinct
- the attempt numbers cover the configured sample count
- the process generations are unchanged
- the final checks passed
- the candidate deadline is strictly below `serving.request_timeout_s`.

The artifact is incomplete when an attempt fails, a generation changes, or a generation check errors.

## The `slo` gate

- `slo` checks each profile's smallest measured decode cohort, its active-request cost, and its KV-token cost against the service-level objective.
- The capacity output shows the request count used for the TPOT calculation.

## Exit codes

Text mode uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes):

| Outcome                                                    | Exit code |
| ---------------------------------------------------------- | --------: |
| A failed gate or operation                                 |       `1` |
| A skipped gate with `--evidence-out`                       |       `1` |
| Invalid arguments                                          |       `2` |
| A fleet config that fails to load or validate              |       `2` |

JSON mode follows the [command result contract](../Command-Results.md).
