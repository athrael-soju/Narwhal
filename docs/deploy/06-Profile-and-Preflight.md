---
description: Run the narwhal-check preflight gates against profiled, idle Narwhal engines.
---

# Profile engines and run preflight

## Profiling idle engines

Run these steps from the router shell, with the engines reserved for profiling and otherwise idle.

1. Warm the model.
2. Choose the sweep from your production traffic:

    - `--prefill-lens` values that meet the [`ttft_split` length rules](../measure/01-Profile.md#prefill-sweep).
    - `--decode-input-lens` values near the input lengths you serve.
    - `--decode-concurrency` values near the concurrency you serve, when it differs from the default `1,4,16,48`.

3. Run the profiler:

    ```bash
    .venv/bin/narwhal-profile \
      --fleet runs/deployment/fleet.json \
      --limits runs/deployment/profiling-limits.json \
      --prefill-lens 256,700,1300,2300,4096,4300,8300,12300 \
      --decode-input-lens 512,4096,8192
    ```

4. Compare the line printed for each engine, `<iid>: max_model_len <n>; prefill up to <x>, decode input up to <y>, decode concurrency up to <z>`, with the `max_model_len` and sequence limit in that engine's checked serving plan.

`narwhal deploy package` writes each engine's `runtime.max_sequences` to `profiling-limits.json`. An SGLang engine without the setting gets 256 and a TensorRT-LLM engine without it gets 64.

The profiler fits each sweep to the engine's live `max_model_len`, as [Prefill sweep](../measure/01-Profile.md#prefill-sweep) and [Decode sweep](../measure/01-Profile.md#decode-sweep) define. It ends decode concurrency at the engine's sequence limit.

Prefix caching can stay on during profiling. On an SGLang fleet, `narwhal-profile` requires the `mooncake` connector and at least two engines.

When an engine refuses a decode measurement request, reports an error in its stream, or times out or drops the stream (for example after a lost KV transfer), the profiler discards that cohort's measurement and runs it again, up to twice. The log shows each rerun as `rerun after engine error`. A third consecutive error stops the profile.

If the live context leaves fewer than three prefill lengths or two decode input lengths, pass shorter `--prefill-lens` and `--decode-input-lens`. If the sequence limit leaves fewer than two decode concurrency points, change the engine launch policy.

Keep the profiler's `.samples.json` sidecar beside the `profiles.path` store.

Preflight warns when an engine's profile measures decode below its sequence limit. It reads that limit from the profile's sidecar (recorded by `--limits`) and, on a fleet with an engine contract, from the engine's attestation when the sidecar records a lower limit or none. Decode seats stop at the profiled maximum, as [Engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) describes. To clear the warning, re-profile the engine with `--limits`, after you:

- regenerate the limits file, when the sidecar records a lower limit than the attestation.
- pass shorter `--decode-input-lens`, or lower the engine's sequence limit and regenerate the limits file, when the sweep skipped cohorts that exceed the engine's KV capacity.

Preflight fails and router startup stops when the profile engine IDs differ from the configured fleet. A standby router also starts when its store has profiles for engines outside the fleet. When a live engine's process generation differs from its saved profile, preflight and router startup require a [new profile](#reusing-or-repeating-profiles). The role controller holds a role change whose projected decode point falls outside the measured profile range.

Set the service-level objective (SLO) targets before preflight:

1. [Set `slo.ttft_s` and `slo.tpot_s`](../measure/02-Targets-and-Freeze.md#setting-production-slos) from the service requirement and the measured engine curves.
2. Keep the time per output token (TPOT) target above the measured per-token floor.

### Reusing or repeating profiles

After a change to the fleet or its engines, run preflight. Re-profile only the engines the `profile` gate names, with the same sweep options, and merge the new store into the current `profiles.path` store:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,700,1300,2300,4096,4300,8300,12300 \
  --decode-input-lens 512,4096,8192 \
  --only <iid> --out runs/profiles-<iid>.json
.venv/bin/narwhal-profile --fleet runs/deployment/fleet.json \
  --merge runs/profiles.json --merge runs/profiles-<iid>.json \
  --out runs/profiles-merged.json
```

On an SGLang fleet, the sweep also switches each selected engine's peer, so add `--router-url` with the active router's URL, or `--no-router` when no router serves the fleet's engines. With `--router-url`, the router must hold every selected engine and peer that it serves, as [Router drain check](../cli/Profile.md#router-drain-check) describes. The router holds one engine at a time outside a whole-wave drain, so when the selected engine and its peer are both members, profile during a whole-wave drain or pair the engine with a spare engine outside the fleet, as [Refresh profiles](../operate/03-Restart-Engines.md#refresh-profiles) describes. A sweep with `--colocated` takes its peers from the engine's own `shared_device` group, so it needs the whole-wave drain.

The later store replaces the earlier store's row for each engine variant it holds. After you remove an engine from the fleet, merge the current store alone to write a store without that engine's rows.

Set `profiles.path` to the merged store and run preflight. To load it into a running router, follow [Activating replacement profiles](../operate/03-Restart-Engines.md#activating-replacement-profiles).

## Calibrating the first-token deadline

Calibrate with the engines idle. `narwhal-check --calibrate-first-token` takes these inputs:

| Flag                      | Value                                                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--input-tokens`          | Lengths spanning the served range, up to the longest input that leaves room for two output tokens on both engines |
| `--observation-timeout-s` | Above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`                                            |
| `--calibration-out`       | A fresh artifact path under the ignored `runs/` tree                                                                    |
| `--samples`               | 100, the default and the minimum for qualifying evidence                                                                |

On SGLang the live context limit is the smaller of `context_length` and `max_req_input_len`, which is below the launch's context length.

For engines whose live context limit is 16,384 tokens, run the calibration from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16382 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

The command exits 0 when the artifact is [`complete`](../cli/Check.md#calibration-artifact) and every engine returns to its starting role. Otherwise it exits 1.

`serving.request_timeout_s` bounds each attempt's prompt sizing, prefill, and decode completion. Each attempt records one of these statuses:

| Attempt status                                       | Meaning                                                          |
| ---------------------------------------------------- | ---------------------------------------------------------------- |
| `completed`                                          | Decode produced its first token within `--observation-timeout-s` |
| `request_expired`                                    | `serving.request_timeout_s` expired                              |
| `observation_expired`                                | The first token missed `--observation-timeout-s`                 |
| `failed_sizing`, `failed_prefill`, `failed_transfer` | The named phase failed                                           |

Only `completed` timings enter the candidate calculation.

For four engines in separate device slots, the command prints:

```text
calibration groups: 36; concurrent rounds per input length: 3; sweep 1 runs each group alone
n1 -> n2, target 256 tokens: 100/100 completed, 0 failed
...
first-token calibration: runs/deployment/first-token-calibration.json
candidate deadline: above 1.840s
duration 1200s
```

Diagnose the failed transfers and observation expiries before using the candidate. Set `engine.first_token_timeout_s` strictly above the candidate and `engine.first_token_calibration_path` to the artifact path.

The deadline, the measured prefill and the router overhead must fit the client time to first token (TTFT) requirement.

### Reusing or repeating the calibration

After a change to the fleet or its engines, run preflight. When the `calibration` gate fails, apply the fix its message names, or recalibrate when it names none. Also recalibrate when either of these changes:

- the fabric route or host behind an unchanged engine URL
- an engine's context limit, when it exceeds the longest calibrated input by more than two tokens

The artifact stays valid when you remove or pin an engine.

To measure only the groups a previous artifact lacks or did not complete, pass that artifact with `--calibration-base`, with the same `--input-tokens` and `--samples`. When an artifact records a length that leaves room for fewer than two output tokens, recalibrate without `--calibration-base`.

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16382 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-base runs/deployment/first-token-calibration.json \
  --calibration-out runs/deployment/first-token-calibration-2.json
```

For each new calibration:

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

| Gate          | What must pass                                                                                                                                               |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `reach`       | Every engine returns HTTP 200 within the configured health HTTP I/O timeout                                                                                  |
| `calibration` | The configured first-token calibration is valid, matches every running process generation, and has a candidate deadline below `engine.first_token_timeout_s` |
| `contract`    | Attestation matches the current process and declared runtime, and every engine attests the same model files                                                  |
| `profile`     | Each saved process generation digest matches its live engine, the profile IDs match the fleet, and the measured decode errors stay within policy             |
| `model`       | Every engine serves the configured model                                                                                                                     |
| `pace`        | Prefill latency stays within the [permitted slowdown](#pace-gate)                                                                                            |
| `tokenize`    | Exact input sizing succeeds when `engine.tokenize` is on                                                                                                     |
| `produce`     | Every tested vLLM producer can export a KV handoff; an SGLang producer passes here, and its consume pairs check the transfer                                 |
| `consume`     | Every tested peer can consume that KV handoff                                                                                                                |
| `slo`         | The configured TTFT and TPOT targets are feasible against the measured profiles                                                                              |

Preflight and router startup report the first-token calibration:

| Calibration                                                                   | Preflight          | Router startup         |
| ----------------------------------------------------------------------------- | ------------------ | ---------------------- |
| `engine.first_token_calibration_path` is empty                                | `WARN`             | Logs a warning         |
| The artifact fails validation                                                 | `FAIL`             | Stops                  |
| An engine's process generation differs from the artifact                      | `FAIL`             | Stops                  |
| `engine.first_token_timeout_s` is at or below the candidate deadline          | `FAIL`             | Stops                  |
| Every engine runs the measured process                                        | `ok` measured line | Logs the measured line |
| One or more engines run a relaunched process with the same process generation | `ok` reused line   | Logs the reused line   |

For a fleet that runs the measured processes, preflight prints:

```text
calibration
  ok    first-token calibration measured on the running engines: candidate 1.840s, deadline 2.5s
```

For a fleet with relaunched engines `n2` and `n4`, preflight prints:

```text
calibration
  ok    first-token calibration reused for n2, n4: launch unchanged since capture; candidate 1.840s, deadline 2.5s
```

### Pace gate

Preflight fails the [`pace` gate](../cli/Check.md#preflight) when a probe fails.

When three or more engines probe successfully, the gate compares each engine against the fleet median. It compares every profiled engine against its profile's prefill prediction at the exact `usage.prompt_tokens`. With fewer than three successful engines, an unprofiled engine skips the gate.

### KV transfer gates

[`narwhal-check`](../cli/Check.md) selects transfer pairs with these options:

| Option        | Pairs and probes                                         |
| ------------- | -------------------------------------------------------- |
| Default mesh  | Every eligible ordered pair                              |
| `--ring`      | The pairs covering each eligible producer and consumer   |
| `--repeats N` | `N` transfer probes per pair, with one verdict per probe |

In the consume gate, a transfer across a role-permitted engine pair passes when:

- Decode produces its first generated token within `engine.first_token_timeout_s`. On SGLang, the deadline is at most 30 seconds.
- Decode finishes a valid stream with output.
- Prefill and decode complete within `serving.request_timeout_s`.

The gate reports the first-token time for each passing transfer.

If the first-token deadline expires, check the calibration artifact and the deadline. For any other failure, keep the pair, the error, and the engine logs.

Keep the command, fleet file, preflight output, process identities, and profile store together.

[Serve and measure](07-Serve-and-Measure.md)
