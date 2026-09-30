---
description: Run the narwhal-check deployment preflight gates, from engine reachability to live KV transfer probes.
---

# `narwhal-check`

`narwhal-check --fleet PATH` runs these deployment preflight gates in order: `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, `produce`, `consume`, `slo`.

Text output prints each gate name followed by result lines marked `ok`, `FAIL`, `SKIP`, or `WARN`.

## Options

General options:

| Option | Default | Description |
| --- | --- | --- |
| `--fleet PATH` | optional | Fleet configuration file for preflight, calibration, and evidence verification. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--version` | | Print the installed version. |
| `--print-example-config` | off | Print the packaged, annotated config and exit. |
| `--print-contract-versions` | off | Print the versioned JSON interface registry and exit. |

`--print-example-config` takes precedence over `--print-contract-versions`.

Transfer probe options for preflight:

| Option | Default | Description |
| --- | --- | --- |
| `--repeats N` | 1 | Probes per pair. |
| `--ring` | off | Test `consume` in a ring. |
| `--no-kv` | off | Skip `produce` and `consume`. |

KV evidence options:

| Option | Default | Description | Mutually exclusive with |
| --- | --- | --- | --- |
| `--evidence-out PATH` | optional | Save full-mesh KV evidence to a new JSON file. | `--ring`, `--no-kv`, `--verify-evidence` |
| `--verify-evidence PATH` | optional | Check saved evidence against the current fleet, profiles, and engine processes. | `--evidence-out` |

Both evidence options require an `engine_contract` in the fleet configuration.

Options for first-token calibration:

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--calibrate-first-token` | off | Measure fresh KV handoffs at each input length and report a first-token deadline. | |
| `--input-tokens LIST` | optional | Comma-separated input lengths. | Each leaves room for at least one output token within both engines' live context limits. |
| `--samples N` | 100 | Fresh handoffs per pair and input length. | |
| `--observation-timeout-s SECONDS` | optional | First-output wait. | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`. |
| `--calibration-out PATH` | optional | Raw samples and group summaries file. | New JSON file under `runs/`. |

`--calibrate-first-token` rules:

| Rule | Options |
| --- | --- |
| Requires | `--input-tokens`, `--observation-timeout-s`, `--calibration-out` |
| Mutually exclusive with | The evidence options, `--ring`, `--no-kv`, a `--repeats` value other than 1 |
| Required by | `--samples`, `--input-tokens`, `--observation-timeout-s`, `--calibration-out` |
| `--input-tokens` includes | The longest input the service admits |

## What a KV probe does

| Property | Value |
| --- | --- |
| Prompt | `"benchmark " * 64`, tokenized for the model under test |
| `consume` input | A KV handoff from an eligible peer |
| `consume` pairs | Every eligible ordered pair, or a ring with `--ring` |
| Output tokens | Four |
| Output flags | vLLM's `min_tokens` and `ignore_eos` |
| Cache salt per probe | One unique `cache_salt` shared by both legs |
| Cache salt per `pace` repeat | Fresh |
| First-token deadline | `engine.first_token_timeout_s` |
| Probe deadline | `serving.request_timeout_s` |
| Probe success | A stream end before the probe deadline |
| Missed first-token deadline | Elapsed time reported |
| Transfer after a miss | Unconfirmed |
| Next step after a miss | Calibration |

## Recording and verifying KV evidence

An engine restart or a config change invalidates recorded evidence.

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

`--evidence-out` rejects evidence when:

- an engine's process generation changes between pairs or repeats
- an engine has lost its profile binding by the final identity check
- the fleet or profile files differ from the hashes taken at the start of the run

`--verify-evidence` rejects saved evidence when:

- the saved run has failed or skipped gates
- the fleet file, profile store, engine contract, or eligible pairs have changed
- a pair has fewer passing transfer and token records than the saved repeat count
- an engine's live process generation differs from the saved records

## First-token calibration

Run the [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before recording evidence for a fleet.

The `--calibration-out` file holds:

- the raw attempts
- the p99 and maximum for each group
- the candidate deadline
- the engines' process generations

| Scope | Timeout |
| --- | --- |
| Wait for first output | `--observation-timeout-s` |
| Whole attempt | `serving.request_timeout_s` |

| Handoff property | Value |
| --- | --- |
| Forced output tokens | Four |
| Forced output token cap | The producer's and consumer's live context limits |
| Output tokens at a `max_model_len - 1` target | One |
| Successful sample | A produced token and a valid stream end |

The artifact is valid evidence when all of these hold:

- every group has at least 100 completed attempts
- the attempt numbers are distinct
- the attempt numbers cover the configured sample count
- the process generations are unchanged
- the final checks passed
- the candidate deadline is strictly below `serving.request_timeout_s`
- `engine.first_token_timeout_s` is above the candidate deadline

The artifact is incomplete when an attempt fails, a generation changes, or a generation check errors.

## The `slo` gate

| Check | Fails when |
| --- | --- |
| TPOT | The token interval at the profile's smallest measured decode cohort exceeds `slo.tpot_s`. |
| TTFT | `slo.ttft_s` is at or below the profile's single-token prefill time. |
| Decode bounds | The profile's measured decode request or KV-token bound is missing. |

The capacity output shows the request count used for the TPOT calculation.

## Exit codes

Text mode uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes):

| Outcome | Exit code |
| --- | :--: |
| A failed gate or operation | `1` |
| A skipped gate with `--evidence-out` | `1` |
| Invalid arguments | `2` |
| A fleet config that fails to load or validate | `2` |

JSON mode follows the [command result contract](../Command-Results.md).
