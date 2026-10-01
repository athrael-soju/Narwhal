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

KV transfer probe and evidence options for preflight:

| Option | Default | Description |
| --- | --- | --- |
| `--repeats N` | 1 | Probes per pair. |
| `--ring` | off | Test `consume` in a ring. |
| `--no-kv` | off | Skip `produce` and `consume`. |
| `--evidence-out PATH` | optional | Save full-mesh KV evidence to a new JSON file. |
| `--verify-evidence PATH` | optional | Check saved evidence against the current fleet, profiles, and engine processes. |

`--evidence-out` is mutually exclusive with `--ring`, `--no-kv`, and `--verify-evidence`. Both evidence options require an `engine_contract` in the fleet configuration.

Options for first-token calibration:

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--calibrate-first-token` | off | Measure fresh KV handoffs at each input length and report a first-token deadline. | |
| `--input-tokens LIST` | optional | Comma-separated input lengths. | Each leaves room for at least one output token within both engines' live context limits. |
| `--samples N` | 100 | Fresh handoffs per pair and input length. | |
| `--observation-timeout-s SECONDS` | optional | First-output wait. | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`. |
| `--calibration-out PATH` | optional | Raw samples and group summaries file. | New JSON file under `runs/`. |

`--calibrate-first-token` follows these rules:

- It requires `--input-tokens`, `--observation-timeout-s`, and `--calibration-out`.
- `--samples`, `--input-tokens`, `--observation-timeout-s`, and `--calibration-out` each require it.
- It is mutually exclusive with the evidence options, `--ring`, `--no-kv`, and a `--repeats` value other than 1.
- `--input-tokens` must include the longest input the service admits.

## What a KV probe does

Each probe sends the prompt `"benchmark " * 64`, tokenized for the model under test, and asks for four output tokens with vLLM's `min_tokens` and `ignore_eos`. Both legs of a probe share one unique `cache_salt`, and each `pace` repeat uses a fresh one.

`consume` takes a KV handoff from an eligible peer. It tests every eligible ordered pair, or a ring with `--ring`.

The first-token deadline is `engine.first_token_timeout_s`, and the probe deadline is `serving.request_timeout_s`. A probe succeeds when its stream ends before the probe deadline.

When a probe misses the first-token deadline, `narwhal-check` reports the elapsed time and the transfer stays unconfirmed. After a miss, run first-token calibration.

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

Run the [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) before recording evidence for a fleet.

The `--calibration-out` file holds:

- the raw attempts
- the p99 and maximum for each group
- the candidate deadline
- the engines' process generations

During calibration, `--observation-timeout-s` bounds the wait for first output, and `serving.request_timeout_s` bounds the whole attempt.

Each calibration handoff forces four output tokens, capped by the producer's and consumer's live context limits. A handoff at a `max_model_len - 1` target gets one output token. A sample succeeds when it produces a token and a valid stream end.

The artifact is valid evidence when all of these hold:

- every group has at least 100 completed attempts
- the attempt numbers are distinct
- the attempt numbers cover the configured sample count
- each engine's identity matches its value at the start of the run
- the final checks passed
- the candidate deadline is strictly below `serving.request_timeout_s`
- `engine.first_token_timeout_s` is above the candidate deadline

When the fleet configuration sets `engine_contract`, an engine's identity during calibration is its attestation digest. Otherwise, it is the vLLM version and process start time.

The artifact is incomplete when any of these happens:

- an attempt fails
- an engine's identity changes during the run
- reading or verifying an engine's identity fails

## The `slo` gate

The `slo` gate runs three checks against each engine's profile:

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
