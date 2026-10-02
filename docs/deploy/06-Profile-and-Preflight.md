---
description: Run the narwhal-check preflight gates against profiled, idle Narwhal engines.
---

# Gate F: Profiling once and running the live KV contract

## Profiling idle engines

Run these steps from the router shell.

1. Reserve the real engines.
2. Keep the engines otherwise idle.
3. Load the private engine URLs and credentials from the router environment.
4. Warm the model.
5. Pick input lengths and concurrency points that match production traffic.
6. Choose `--prefill-lens` values that meet the [`ttft_split` length rules](../measure/01-Profile.md#prefill-sweep).
7. Run the profiler:

    ```bash
    .venv/bin/narwhal-profile \
      --fleet runs/deployment/fleet.json \
      --limits runs/deployment/profiling-limits.json \
      --prefill-lens 256,700,1300,2300,4096,4300,8300,12300 \
      --decode-input-lens 512,4096,8192
    ```

8. Compare each printed effective sweep with every checked serving plan.

Gate B's `deploy_hosts.py prepare` writes `profiling-limits.json` from each engine's `--max-num-seqs`.

The effective sweep has these bounds:

| Sweep input          | Bound                                                     |
| -------------------- | --------------------------------------------------------- |
| Decode concurrency   | At most `--max-num-seqs`                                  |
| Prefill lengths      | Input plus one output token fits the live `max_model_len` |
| Decode input lengths | Input plus 64 output tokens fits the live `max_model_len` |

Prefix caching can stay on during profiling.

If the live context leaves fewer than three prefill lengths or two decode input lengths, pass shorter `--prefill-lens` and `--decode-input-lens`. If `--max-num-seqs` allows fewer than two decode concurrency points, change the engine launch policy.

Keep the profiler's `.samples.json` sidecar beside the `profiles.path` store.

Preflight fails and router startup stops when the profile engine IDs differ from the configured fleet. When a live engine's process generation differs from its saved profile, preflight and router startup require a new profile. The role controller holds a role change whose projected decode point falls outside the measured profile range.

Set the service-level objective (SLO) targets before preflight:

1. [Set `slo.ttft_s` and `slo.tpot_s`](../measure/02-Targets-and-Freeze.md#5-setting-production-slos) from the service requirement and the measured engine curves.
2. Keep the time per output token (TPOT) target above the measured per-token floor.

## Calibrating the first-token deadline

Calibrate with the engines idle. `narwhal-check --calibrate-first-token` takes these inputs:

| Flag                      | Value                                                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--input-tokens`          | Lengths spanning the served range, including the longest admitted input, with room for one output token on both engines |
| `--observation-timeout-s` | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`                                            |
| `--calibration-out`       | A fresh artifact path under the ignored `runs/` tree                                                                    |
| `--samples`               | 100, the default and the minimum for qualifying evidence                                                                |

For an engine launch limit of 16,384 total tokens, run the calibration from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16383 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

The artifact records input tokens, prefill time, decode-to-first-token time, and failed or expired attempts.

Calibration runs [concurrent rounds](../cli/Check.md#sampling-schedule) in which each device slot serves at most one calibration prefill or decode at a time.

The command exits 0 with artifact status `complete` when the artifact holds at least 100 samples per role-permitted directed pair and input length, every attempt completed, and the process generations match. Otherwise it exits 1 with status `incomplete`.

`serving.request_timeout_s` bounds each attempt's prompt sizing, prefill, and decode completion. Each attempt records one of these statuses:

| Attempt status                                       | Meaning                                                          |
| ---------------------------------------------------- | ---------------------------------------------------------------- |
| `completed`                                          | Decode produced its first token within `--observation-timeout-s` |
| `request_expired`                                    | `serving.request_timeout_s` expired                              |
| `observation_expired`                                | The first token missed `--observation-timeout-s`                 |
| `failed_sizing`, `failed_prefill`, `failed_transfer` | The named phase failed                                           |

Only `completed` timings enter the candidate calculation.

For four engines in separate device slots, the command prints the schedule, each group's result, the candidate deadline, and the run duration:

```text
calibration groups: 36; concurrent rounds per input length: 3; sweep 1 runs each group alone
e0 -> e1, target 256 tokens: 100/100 completed, 0 failed
...
first-token calibration: runs/deployment/first-token-calibration.json
candidate deadline: above 1.840s
duration 1200s
```

The candidate deadline is the largest `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` across pairs and input lengths.

Diagnose the failed transfers and observation expiries before using the candidate. Set `engine.first_token_timeout_s` strictly above the candidate and `engine.first_token_calibration_path` to the artifact path.

The deadline, the measured prefill, and the router overhead must fit the client time to first token (TTFT) requirement.

Preflight and router startup label an engine relaunched with the same process generation `reused`.

Repeat the calibration when the model, process generation, transport, or served context changes:

1. Use a fresh artifact path.
2. Update the fleet config.
3. Copy the artifact and the fleet config to every router host before preflight.

Retain the fleet file, the raw artifact, the command, and the process identities together.

## Running preflight

With the engines otherwise idle, run the preflight from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The full preflight runs these gates:

| Gate          | What must pass                                                                                                                                      |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`       | Every engine returns HTTP 200 within the configured health HTTP I/O timeout                                                                         |
| `calibration` | The configured first-token calibration is valid, matches every running process generation, and has a candidate below `engine.first_token_timeout_s` |
| `contract`    | Attestation matches the current process and declared runtime                                                                                        |
| `profile`     | Each saved process generation digest matches its live engine, the profile IDs match the fleet, and the measured decode errors stay within policy    |
| `model`       | Every engine serves the configured model                                                                                                            |
| `pace`        | Prefill latency stays within the [permitted slowdown](#pace-gate)                                                                                   |
| `tokenize`    | Exact input sizing succeeds when `engine.tokenize` is on                                                                                            |
| `produce`     | Every tested producer can export a KV handoff                                                                                                       |
| `consume`     | Every tested peer can consume that KV handoff                                                                                                       |
| `slo`         | The configured TTFT and TPOT targets are feasible against the measured profiles                                                                     |

Preflight and router startup report the first-token calibration:

| Calibration                                                                                                         | Preflight          | Router startup         |
| ------------------------------------------------------------------------------------------------------------------- | ------------------ | ---------------------- |
| `engine.first_token_calibration_path` empty                                                                         | `WARN`             | Logs a warning         |
| The artifact fails validation, an engine's process generation differs, or the deadline is at or below the candidate | `FAIL`             | Stops                  |
| Every engine runs the measured process                                                                              | `ok` measured line | Logs the measured line |
| One or more engines run a relaunched process with the same process generation                                       | `ok` reused line   | Logs the reused line   |

A fleet running the measured processes prints:

```text
calibration
  ok    first-token calibration measured on the running engines: candidate 1.840s, deadline 2.5s
```

A fleet with relaunched engines `e1` and `e3` prints:

```text
calibration
  ok    first-token calibration reused for e1, e3: launch unchanged since capture; candidate 1.840s, deadline 2.5s
```

Under `recovery.engine_restart_policy: individual`, preflight warns about each host-sharing KV producer whose crash recovery needs a [whole-wave restart](../concepts/03-Failure-and-State.md#whole-wave-fallback).

### Pace gate

The pace gate sends two cold one-token completions to each engine and scores the engine by its faster probe time. The permitted slowdown is 1.5x, and a failed probe fails the gate.

When three or more engines probe successfully, the gate compares each engine against the fleet median. It compares every profiled engine against its profile's prefill prediction at the exact `usage.prompt_tokens`. With fewer than three successful engines, an unprofiled engine skips the gate.

### KV transfer gates

[`narwhal-check`](../cli/Check.md) selects transfer pairs with these options:

| Option        | Pairs and probes                                         |
| ------------- | -------------------------------------------------------- |
| Default mesh  | Every eligible ordered pair                              |
| `--ring`      | The pairs covering each eligible producer and consumer   |
| `--repeats N` | `N` transfer probes per pair, with one verdict per probe |

In the consume gate, a transfer across a role-permitted engine pair passes when:

- Decode produces its first generated token within `engine.first_token_timeout_s`.
- Decode finishes a valid stream with output.
- Prefill and decode complete within `serving.request_timeout_s`.

The gate reports the first-token time for each passing transfer.

If the first-token deadline expires, check the calibration artifact and the deadline. For any other failure, keep the pair, the error, and the engine logs.

Keep the command, fleet file, preflight output, process identities, and profile store together.

[![Next: Gate G: Starting the service and validating capacity through the private path](https://img.shields.io/badge/next-Gate%20G%3A%20Starting%20the%20service%20and%20validating%20capacity%20through%20the%20private%20path-0f766e)](07-Serve-and-Measure.md)
