---
description: Run the narwhal-check deployment preflight gates, from engine reachability to live KV transfer probes.
---

# `narwhal-check`

`narwhal-check` checks the running engines and their measured profiles before you start the router. It has three modes:

- **Preflight**, the default, runs a fixed sequence of gates against the fleet.
- **Evidence** records the KV transfers of every directed pair with `--evidence-out`, or checks a saved record with `--verify-evidence`.
- **Calibration** measures first-token times of fresh KV handoffs with `--calibrate-first-token`.

## Options

General options:

| Option | Default | Description |
| --- | --- | --- |
| `--fleet PATH` | optional | Fleet configuration file for preflight, calibration, and evidence verification. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--version` | | Print the installed version. |
| `--print-example-config` | off | Print the packaged, annotated config and exit. |
| `--print-contract-versions` | off | Print the versioned JSON interface registry and exit. |

Every mode needs `--fleet`. When both print options are set, `--print-example-config` wins.

KV transfer probe and evidence options:

| Option | Default | Description |
| --- | --- | --- |
| `--repeats N` | 1 | Probes per pair. |
| `--ring` | off | Test `consume` in a ring. |
| `--no-kv` | off | Skip `produce` and `consume`. |
| `--evidence-out PATH` | optional | Save full-mesh KV evidence to a new JSON file. |
| `--verify-evidence PATH` | optional | Check saved evidence against the current fleet, profiles, and engine processes. |

Options for first-token calibration:

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--calibrate-first-token` | off | Measure fresh KV handoffs at each input length and report a first-token deadline. | |
| `--input-tokens LIST` | optional | Comma-separated input lengths. | Distinct positive lengths, each leaving room for one output token within both engines' live context limits. |
| `--samples N` | 100 | Fresh handoffs per pair and input length. | At least 100 for valid evidence |
| `--observation-timeout-s SECONDS` | optional | First-output wait. | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`. |
| `--calibration-out PATH` | optional | Raw samples and group summaries file. | A new JSON file |
| `--calibration-base PATH` | optional | Previous calibration artifact. Calibration reuses its complete groups and measures the rest. | An artifact with the same model, contract, `--input-tokens` and `--samples` |

## Preflight

`narwhal-check --fleet fleet.json` runs the gates below in order. Under each gate's name, it prints result lines marked `ok`, `FAIL`, `SKIP`, or `WARN`.

| Gate | Checks |
| --- | --- |
| `reach` | Each engine answers `/health` with HTTP 200. Later gates skip an engine that fails here. |
| `calibration` | The [first-token calibration](#the-calibration-gate) at `engine.first_token_calibration_path` matches the running engines. |
| `contract` | With `engine_contract` set, each engine's identity is readable, its version matches the contract, and its attestation sidecar verifies against the contract and the live process. Without it, the gate reads engine versions only. Mixed engine versions fail on either fleet. |
| `profile` | Each engine has a profile within `profiles.max_decode_fit_mape` and `profiles.max_decode_cv_mape`, the store holds no profile for an unconfigured engine, and each profile's generation matches the live engine. |
| `model` | Each engine lists the fleet's `model` in `/v1/models`. |
| `pace` | Each engine's best time over two cold one-token completions of a long prompt stays within 1.5 times the fleet median, with at least three timed engines, and within 1.5 times its own profile's prefill time. |
| `tokenize` | Each engine returns an exact token count for the probe prompt. With `engine.tokenize` off, the gate is skipped. |
| `produce`, `consume` | [KV transfer probes](#kv-transfer-probes) between engines. |
| `slo` | Each engine's profile meets the [SLO targets](#the-slo-gate). |

Between `reach` and `calibration`, preflight on vLLM warns about host-sharing producers that need a [whole-wave restart](../concepts/03-Failure-and-State.md#whole-wave-fallback) to recover from a crash.

When `pace`, `contract`, profile generation or `model` fails on an engine, preflight skips `produce` and `consume`.

On SGLang, each timed `pace` request runs its decode leg on a peer, and its time includes that leg. The peer is the first other live engine, and `pace` and `consume` switch engines into the roles each request needs. Preflight returns every engine to its starting role at the end, and a failed return shows as a `FAIL` on `role restore`. Calibration switches engines the same way and returns every engine to its starting role at the end, and a failed return prints `role restore failed` and exits with status 1.

### KV transfer probes

A KV probe sends the prompt `"benchmark " * 64` and asks for four output tokens with `min_tokens` and `ignore_eos`. Each attempt carries a fresh `cache_salt`, so it starts from a cold cache.

On vLLM, `produce` runs a prefill on each engine and checks that it returns handoff parameters. `consume` then moves KV from producer to consumer, across every ordered pair that role pins permit or, with `--ring`, across rotating pairs that give every consumer a producer. On SGLang, `produce` defers to `consume`, which checks each pair's whole transfer.

A probe passes when the consumer produces at least one token within `serving.request_timeout_s`. The first token must arrive within `engine.first_token_timeout_s`, or on SGLang within the smaller of that and 30 seconds. When the first token misses its deadline, `narwhal-check` fails the pair, reports the deadline, and leaves the transfer unconfirmed. Run [first-token calibration](#first-token-calibration) after a miss.

## Recording and verifying KV evidence

KV evidence ties each directed pair's transfers to specific engine processes. Both evidence options need an `engine_contract` in the fleet configuration. `--evidence-out` probes the full mesh, so it excludes `--ring`, `--no-kv` and `--verify-evidence`.

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

With `--evidence-out`, preflight reads each engine's attestation and transfer metrics before and after every probe. A transfer passes when both the count and the sum of the decode engine's transfer histogram grow. On vLLM this histogram is `vllm:nixl_xfer_time_seconds`, and on SGLang it is the `decode_transferred` stage of `sglang:per_stage_req_latency_seconds`.

Each pair record holds the histogram name in `transfer_metric` and its growth in `transfer_count_delta` and `transfer_seconds`. It also holds the fields that told the decode engine where to fetch the cache: `transfer_mode`, `remote_engine_id`, `remote_host` and `remote_port` on vLLM, or `bootstrap_host`, `bootstrap_port` and `bootstrap_room` on SGLang.

The run fails in any of these cases:

- A pair has fewer passing probes than `--repeats`.
- An engine's process changes during or after the probes.
- An engine's profiles no longer match its live generation.
- The fleet or profile file changes during the run.

`narwhal-check` writes the evidence file either way, with `status` `passed` or `failed`.

`--verify-evidence` runs in place of preflight. It fails in any of these cases:

- The saved file contains failed or skipped gates.
- The fleet file, profile store, engine contract or eligible pairs changed.
- A pair has fewer passing records than the saved repeat count, or lacks token and transfer evidence.
- An engine's process changed during the saved run or differs from it now.

After an engine restart or a configuration change, record new evidence.

## First-token calibration

Calibration measures how long fresh KV handoffs take to produce their first token, and reports a candidate deadline that `engine.first_token_timeout_s` must exceed. Follow the [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) to run it before you record evidence for a fleet.

`--calibrate-first-token` needs `--input-tokens`, `--observation-timeout-s` and `--calibration-out`. Those options, and a `--samples` value other than 100, apply only to calibration. Calibration probes every directed pair, so it excludes the evidence options, `--ring`, `--no-kv`, and a `--repeats` value other than 1. Include the longest input the service admits in `--input-tokens`, and keep the output file under `runs/`.

Calibration needs at least one directed pair that role pins permit and an engine tokenizer route that returns exact counts. It exits with status 0 when the artifact is `complete` and every switched engine returns to its starting role, and 1 otherwise.

### Sampling schedule

A group is one directed pair at one input length, sampled `--samples` times. Sweep 1 runs each group alone, ordered by producer ID, then input length, then pair. Each later sweep runs the pairs in rounds, one lockstep step per round and input length:

1. Calibration sizes each pair's prompt and sends every prefill in the round together.
2. On vLLM, once every prefill has finished or failed, each consumer decodes from its producer's KV handoff. On SGLang, each consumer starts its decode leg once every prefill has been sent. The step ends when every decode has finished or failed.

A device slot is an engine's [`shared_device.group`](../configuration/01-Fleet-Schema.md#shared-device-allocation), or the engine itself without one. In a round, each device slot produces at most once and consumes at most once, so `n` engines on separate slots that permit every pair need `n - 1` rounds per input length. On SGLang, calibration splits the pairs into phases that keep each engine in one role, runs every sweep within one phase before the next, and gives each device slot one leg per round. Round numbers continue across phases.

Three timers bound each attempt. Prompt sizing and the wait from decode start to first output each get `--observation-timeout-s`, and the whole attempt gets `serving.request_timeout_s`. On SGLang, the first-output wait is at most 30 seconds, and the first-token time includes the prefill. An attempt asks for four output tokens, or fewer when the paired engines' live context limit leaves less room, and completes when it generates a token.

With `--calibration-base`, calibration reuses a base group that completed every sample when both its engines keep their URL and process generation. An engine listed in the base's `changed_generations` or `generation_errors` counts as changed. Calibration measures the other groups, and each reused group's output line ends with `, reused`.

### Calibration artifact

The artifact at `--calibration-out` holds these fields:

| Field | Contents |
| --- | --- |
| `status` | `complete` or `incomplete` |
| `captured_at_unix` | Unix time of the write |
| `calibration_base` | The `--calibration-base` path, or `null` |
| `duration_s` | Seconds from the first engine read to the write |
| `model`, `contract_fingerprint`, `engine_urls` | The fleet the run measured |
| `generations` | Each engine's process generation digest at the start of the run |
| `process_starts` | Each engine's process start time at the start of the run |
| `changed_generations`, `generation_errors` | Engines whose process changed during the run, and identity reads that failed at its end |
| `input_tokens`, `samples_per_group`, `observation_timeout_s`, `configured_deadline_s` | The run's inputs |
| `groups` | Per group: input token range, completed and failed counts, `p99_seconds`, `maximum_seconds`, `candidate_deadline_s` and `captured_at_unix` |
| `attempts` | Every attempt, with its `attempt` sweep number, its `round` (`null` in sweep 1), timings, and `status` |
| `candidate_deadline_s` | The largest group candidate |

A group's candidate deadline is the larger of its maximum and 1.2 times its p99, plus 0.5 seconds. An attempt's `status` is `completed`, `observation_expired`, `request_expired`, `failed_sizing`, `failed_prefill` or `failed_transfer`. `groups` follow the sweep 1 order, and `attempts` follow the group order and then sweep number.

The artifact is `complete` when all of these hold:

- Every group completed every sample with zero failures.
- `--samples` is at least 100.
- No engine's process changed, and every final identity read succeeded.
- The largest candidate is below `serving.request_timeout_s`.

An engine's process counts as changed when its attestation digest changes, or with `engine_contract` unset, its engine version or process start time. On SGLang, `narwhal-check` reads the process start from the engine's [attestation sidecar](Attest.md#routes).

## The `calibration` gate

The `calibration` gate reads the artifact at `engine.first_token_calibration_path` and warns when the path is unset. It rejects an artifact in any of these cases:

- The artifact is not `complete`.
- The artifact names a different model or contract, lacks a configured engine, or records a different address for one.
- Its groups and attempts fail to cover every current directed pair at every recorded input length.
- Its recomputed p99, maximum and candidate differ from the recorded ones.
- It lacks a capture time, a run duration, a process start for every engine or a round for every attempt.
- `engine.first_token_timeout_s` is at or below its candidate deadline.

It then compares each running engine's [process generation](../Core-Concepts.md#terms) with `generations`:

| Fleet and sidecar | Process generation |
| --- | --- |
| `engine_contract` set, sidecar reports launch evidence | `launch_digest` |
| `engine_contract` set, sidecar reports an attestation digest only | `attestation_digest` |
| `engine_contract` unset | Engine version and process start time |

An engine whose generation differs fails with `<iid> process differs from first-token calibration`. An engine with the same generation is `measured` when its process start also matches and `reused` when it differs. A restarted engine keeps its calibration only when its sidecar reports launch evidence.

| Status | Condition | Output |
| --- | --- | --- |
| `uncalibrated` | `engine.first_token_calibration_path` is empty | `WARN` |
| `rejected` | The artifact fails validation, or an engine's process generation differs or is unreadable | One `FAIL` per problem |
| `measured` | Every engine is `measured` | `ok` |
| `reused` | One or more engines are `reused` | `ok` with the IDs of the reused engines |

In JSON mode, `data.first_token_calibration` holds:

| Field | Value |
| --- | --- |
| `status` | `uncalibrated`, `rejected`, `measured`, or `reused` |
| `path` | `engine.first_token_calibration_path`, or `null` when the path is empty |
| `captured_at_unix` | The artifact's capture time when `status` is `measured` or `reused`, otherwise `null` |
| `candidate_deadline_s` | The artifact's candidate deadline when `status` is `measured` or `reused`, otherwise `null` |
| `engines` | The label of each engine when `status` is `measured` or `reused`, otherwise `{}` |

## The `slo` gate

The `slo` gate checks each engine's profile against the SLO targets. An engine without a profile is skipped. When every check passes, the gate reports the batch tokens the engine holds at the TPOT target and the request count behind them.

| Check | Fails when |
| --- | --- |
| Decode bounds | The profile's measured decode request or KV-token bound is missing. |
| TPOT | The token interval at the profile's smallest measured decode cohort exceeds `slo.tpot_s`. |
| TTFT | `slo.ttft_s` is at or below the profile's single-token prefill time. |

## Exit codes

Text mode uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes):

| Outcome | Exit code |
| --- | :--: |
| A failed gate, a failed evidence verification, an incomplete calibration, or a failed role restore | `1` |
| A skipped gate with `--evidence-out` | `1` |
| Invalid arguments | `2` |
| A fleet config that fails to load or validate | `2` |

Skipped gates alone leave preflight at exit code `0` in text mode. JSON mode follows the [command result contract](../Command-Results.md): failed gates set the status `failed_gate`, and skipped gates set `degraded`.
