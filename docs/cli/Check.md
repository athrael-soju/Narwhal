# `narwhal-check`

`narwhal-check` runs deployment gates against a Narwhal fleet before it takes traffic. Given `--fleet PATH`, it runs them in this order: `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, `produce`, `consume`, `slo`.

Text output prints one line per gate: `ok`, `FAIL`, `SKIP`, or `WARN`.

## Options

General options:

| Option                      | Default | Description                                                                                                                     |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | optional | Fleet configuration file. Required for preflight, calibration, and evidence verification.                                       |
| `--format`                  | `text`  | `text` or `json`. JSON output follows the [versioned command results](../Command-Results.md).                                   |
| `--version`                 |         | Print the installed version.                                                                                                    |
| `--print-example-config`    | off     | Print the packaged, annotated config and exit. `--fleet` is optional. Takes precedence over `--print-contract-versions`.         |
| `--print-contract-versions` | off     | Print the versioned JSON interface registry and exit. `--fleet` is optional.                                                    |

Transfer probe options, for the fresh probes that run during preflight:

| Option        | Default | Description                                                                 |
| ------------- | ------- | --------------------------------------------------------------------------- |
| `--repeats N` | 1       | Probes per pair. Values below 1 count as 1.                                 |
| `--ring`      | off     | Test `consume` in a ring. The default mesh tests every eligible ordered pair. |
| `--no-kv`     | off     | Skip `produce` and `consume`.                                               |

KV evidence options:

| Option                   | Default | Description                                                                                                                              |
| ------------------------ | ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `--evidence-out PATH`    | optional | Save full-mesh KV evidence to a new JSON file. Mutually exclusive with `--ring`, `--no-kv`, and `--verify-evidence`.                     |
| `--verify-evidence PATH` | optional | Check saved evidence against the current fleet and profile hashes and the engines' live process generations. Mutually exclusive with `--evidence-out`. |

Both evidence options need an `engine_contract` in the fleet config.

First-token calibration options:

| Option                            | Default | Description                                                                                                                                                                               |
| --------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--calibrate-first-token`         | off     | Measure fresh KV handoffs at each input length and report a first-token deadline.                                                                                                         |
| `--input-tokens LIST`             | optional | Comma-separated target input lengths. Include the longest input your service admits. Every target must leave room for at least one output token within both engines' live context limits. |
| `--samples N`                     | 100     | Fresh handoffs per pair and input length.                                                                                                                                                 |
| `--observation-timeout-s SECONDS` | optional | How long to wait for first output. Must be above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`.                                                                  |
| `--calibration-out PATH`          | optional | New JSON file under `runs/` for raw samples and group summaries.                                                                                                                          |

`--calibrate-first-token` rules:

- It requires `--input-tokens`, `--observation-timeout-s`, and `--calibration-out`.
- It is mutually exclusive with the evidence options, `--ring`, `--no-kv`, and a `--repeats` value other than 1.
- `--samples` and the other calibration options require `--calibrate-first-token`.

## What a KV probe does

| Property                     | Value                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| Prompt                       | `"benchmark " * 64`, tokenized for the model under test                                     |
| `consume` input              | A KV handoff from an eligible peer                                                          |
| Output                       | Up to four tokens, with vLLM's `min_tokens` and `ignore_eos` set                            |
| Cache salt                   | One unique `cache_salt` shared by both legs of a probe, and a fresh salt for each `pace` repeat |
| First-token deadline         | `engine.first_token_timeout_s`                                                              |
| Probe deadline               | `serving.request_timeout_s`, with a clean stream end                                        |
| Missed first-token deadline  | The check reports the elapsed time, marks the transfer unconfirmed, and points to calibration |

## Recording and verifying KV evidence

Evidence is bound to the running engine processes and the current config. An engine restart or a config change invalidates it.

Record and verify evidence:

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

With `--evidence-out`, preflight saves the outcome of each pair and exits 0 when every gate passes. The run's evidence is rejected when any of these happens:

- an engine's process generation changes between pairs or repeats;
- an engine has lost its profile binding by the final identity check;
- the fleet or profile files differ from the hashes taken at the start of the run.

A `--verify-evidence` run repeats these checks against the current engines and runs preflight's fresh transfer probes.

## First-token calibration

Run calibration, following the [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline), before you record evidence for a fleet.

The output file contains the raw attempts, the p99 and maximum for each group, the candidate deadline, and the engines' process generations.

| Scope                 | Timeout                     |
| --------------------- | --------------------------- |
| Wait for first output | `--observation-timeout-s`   |
| Whole attempt         | `serving.request_timeout_s` |

Each handoff forces four output tokens, capped by the producer's and consumer's live context limits. A target of `max_model_len - 1` gets one token. A successful sample produces a token and ends its stream validly.

The artifact is valid evidence when all of these hold:

- every group has at least 100 completed attempts;
- the attempt numbers are distinct and cover the configured sample count;
- the process generations held steady and the final checks passed;
- the candidate deadline is strictly below `serving.request_timeout_s`.

When an attempt fails, a generation changes, or a generation check errors, the command keeps the artifact and marks it incomplete.

## The `slo` gate

`slo` compares each profile's smallest measured decode cohort, with its active-request and KV-token costs, against the service-level objective. The capacity output shows the request count used for the TPOT calculation.

## Exit codes

Text mode uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes):

| Outcome                                                    | Exit code |
| ---------------------------------------------------------- | --------: |
| A failed gate or operation                                 |       `1` |
| Invalid arguments, or a fleet config that fails to load or validate |       `2` |

JSON mode follows the [command result contract](../Command-Results.md).
