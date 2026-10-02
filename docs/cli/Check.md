---
description: Run the narwhal-check deployment preflight gates, from engine reachability to live KV transfer probes.
---

# `narwhal-check`

`narwhal-check --fleet PATH` runs these deployment preflight gates in order: `reach`, `calibration`, `contract`, `profile`, `model`, `pace`, `tokenize`, `produce`, `consume`, `slo`.

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

### Sampling schedule

| Term | Rule |
| --- | --- |
| Group | One role-permitted directed pair at one input length, measured `--samples` times |
| Sweep `k` | Attempt `k` of every group |
| Device slot | The engine's [`shared_device.group`](../configuration/01-Fleet-Schema.md#21-shared-device-allocation), otherwise the engine |
| Round | A set of pairs in which each device slot produces at most once and consumes at most once |
| Round count | The largest number of pairs that one device slot produces or consumes |
| Sweep 1 | Each group runs alone, ordered by producer ID, then input length in `--input-tokens` order, then pair |
| Sweeps 2 to `--samples` | Each round runs as one lockstep step per input length, in `--input-tokens` order |

A fleet of `n` engines in separate device slots, with every directed pair permitted, takes `n - 1` rounds per input length.

A lockstep step runs two phases across every pair in its round:

| Phase | Work for each pair | Ends when |
| --- | --- | --- |
| Prefill | Prompt sizing and prefill on the producer | Every prefill in the step finishes or fails |
| Decode | Decode on the consumer from the producer's KV handoff | Every decode in the step finishes or fails |

Each device slot serves at most one calibration prefill or decode at a time.

These timers bound each attempt:

| Interval | Bound |
| --- | --- |
| Prompt sizing | `--observation-timeout-s` |
| Decode start to first output | `--observation-timeout-s` |
| The attempt's sizing, prefill, and decode | `serving.request_timeout_s`, with the clock paused between the phases |

Each calibration handoff forces four output tokens, capped by the producer's and consumer's live context limits. A handoff at a `max_model_len - 1` target gets one output token. A sample succeeds when it produces a token and a valid stream end.

### Calibration artifact

The `--calibration-out` file holds these fields:

| Field | Contents |
| --- | --- |
| `status` | `complete` or `incomplete` |
| `captured_at_unix` | Unix time of the write |
| `duration_s` | Seconds from the first engine read to the write |
| `generations` | Each engine's process generation digest |
| `process_starts` | Each engine's process start time, read with its process generation at the start of the run |
| `samples_per_group` | The `--samples` value |
| `groups` | Completed and failed counts, p99, maximum, and candidate deadline for each group |
| `attempts` | The raw attempts |
| `attempts[].attempt` | Sweep number, from 1 to `samples_per_group` |
| `attempts[].round` | `null` in sweep 1, otherwise the round number within the input length, from 1 |
| `candidate_deadline_s` | The largest group candidate deadline |

`groups` follow the sweep 1 order.

`attempts` follow the same group order, then `attempt`.

The artifact is valid evidence when all of these hold:

- the artifact records `captured_at_unix`, `duration_s`, a `process_starts` time for every engine, and a `round` for every attempt
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

## The `calibration` gate

The `calibration` gate checks the artifact at `engine.first_token_calibration_path` against each running engine's [process generation](../Core-Concepts.md#terms):

| Fleet and sidecar | Process generation |
| --- | --- |
| `engine_contract` set, sidecar reports launch evidence | `launch_digest` |
| `engine_contract` set, sidecar reports an attestation digest only | `attestation_digest` |
| `engine_contract` unset | vLLM version and process start time |

The gate labels each running engine:

| Running engine | Result |
| --- | --- |
| Process generation differs from `generations` | `FAIL` with `<iid> process differs from first-token calibration` |
| Same process generation and process start | `measured` |
| Same process generation, different process start | `reused` |

The gate reports one of these statuses:

| Status | Condition | Output |
| --- | --- | --- |
| `uncalibrated` | `engine.first_token_calibration_path` is empty | `WARN` |
| `rejected` | The artifact fails validation, or an engine's process generation differs or is unreadable | One `FAIL` per problem |
| `measured` | Every engine is `measured` | `ok` |
| `reused` | One or more engines are `reused` | `ok`, naming the reused engines |

In JSON mode, `data.first_token_calibration` holds:

| Field | Value |
| --- | --- |
| `status` | `uncalibrated`, `rejected`, `measured`, or `reused` |
| `path` | `engine.first_token_calibration_path`, or `null` when empty |
| `captured_at_unix` | The artifact's capture time when `measured` or `reused`, otherwise `null` |
| `candidate_deadline_s` | The artifact's candidate deadline when `measured` or `reused`, otherwise `null` |
| `engines` | The label of each engine when `measured` or `reused`, otherwise `{}` |

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
