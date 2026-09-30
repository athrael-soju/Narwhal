---
description: Run the narwhal-check preflight gates against profiled, idle Narwhal engines.
---

# Gate F: Profile once and run the live KV contract

## Profile idle engines

Run these steps from the router shell.

1. Reserve the real engines.
2. Keep the engines otherwise idle.
3. Load the private engine URLs and credentials from the router environment.
4. Warm the model.
5. Pick input lengths and concurrency points that match production traffic.
6. Place at least six prefill lengths:
    - at least two that end within the first cache block or on a block boundary
    - at least two that end between later block boundaries
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

| Sweep input          | Bound                                                      |
| -------------------- | ---------------------------------------------------------- |
| Decode concurrency   | At most `--max-num-seqs`.                                  |
| Prefill lengths      | Input plus one output token fits the live `max_model_len`. |
| Decode input lengths | Input plus 64 output tokens fits the live `max_model_len`. |

Engines keep prefix caching on during profiling.

| Profiler error                                                                       | Fix                                                      |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| The live context leaves fewer than three prefill lengths or two decode input lengths | Pass shorter `--prefill-lens` and `--decode-input-lens`. |
| `--max-num-seqs` allows fewer than two decode concurrency points                     | Change the engine launch policy.                         |

Keep the profiler's `.samples.json` sidecar beside the `profiles.path` store.

| Condition                                                                       | Result                                              |
| ------------------------------------------------------------------------------- | --------------------------------------------------- |
| Profile engine IDs differ from the configured fleet                             | Preflight fails and router startup stops.           |
| The live engine's attested process identity differs from its saved profile      | Preflight and router startup require a new profile. |
| A role change's projected decode point falls outside the measured profile range | The role controller holds the change.               |

Set the service-level objective (SLO) targets before preflight:

1. [Set `slo.ttft_s` and `slo.tpot_s`](../measure/02-Targets-and-Freeze.md#5-set-production-slos) from the service requirement and the measured engine curves.
2. Keep the time per output token (TPOT) target above the measured per-token floor.

## Calibrate the first-token deadline

Calibration inputs, with the engines idle:

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

| Exit code | Artifact status                                                                                                                               |
| :-------: | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 0         | `complete`: at least 100 samples per role-permitted directed pair and input length, every attempt completed, and matching process generations |
| 1         | `incomplete`                                                                                                                                  |

`serving.request_timeout_s` bounds each attempt's prompt sizing, prefill, and decode completion.

| Attempt status                                       | Meaning                                                           |
| ---------------------------------------------------- | ----------------------------------------------------------------- |
| `completed`                                          | Decode produced its first token within `--observation-timeout-s`. |
| `request_expired`                                    | `serving.request_timeout_s` expired.                              |
| `observation_expired`                                | The first token missed `--observation-timeout-s`.                 |
| `failed_sizing`, `failed_prefill`, `failed_transfer` | The named phase failed.                                           |

Only `completed` timings enter the candidate calculation.

The command prints the candidate deadline, the largest `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` across pairs and input lengths.

Diagnose the failed transfers and observation expiries before using the candidate.

| Field                                 | Value                         |
| ------------------------------------- | ----------------------------- |
| `engine.first_token_timeout_s`        | Strictly above the candidate. |
| `engine.first_token_calibration_path` | The artifact path.            |

The deadline, the measured prefill, and the router overhead must fit the client time to first token (TTFT) requirement.

Repeat the calibration when the model, process generation, transport, or served context changes:

1. Use a fresh artifact path.
2. Update the fleet config.
3. Copy the artifact and the fleet config to every router host before preflight.

Retain the fleet file, the raw artifact, the command, and the process identities together.

## Run preflight

With the engines otherwise idle, run the preflight from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The full preflight runs these gates:

| Gate       | What must pass                                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine returns HTTP 200 within the configured health HTTP I/O timeout.                                                                      |
| `contract` | Attestation matches the current process and declared runtime.                                                                                     |
| `profile`  | Each saved process generation digest matches its live engine, the profile IDs match the fleet, and the measured decode errors stay within policy. |
| `model`    | Every engine serves the configured model.                                                                                                         |
| `pace`     | Prefill latency stays within the [permitted slowdown](#pace-gate).                                                                                |
| `tokenize` | Exact input sizing succeeds when `engine.tokenize` is on.                                                                                         |
| `produce`  | Every tested producer can export a KV handoff.                                                                                                    |
| `consume`  | Every tested peer can consume that KV handoff.                                                                                                    |
| `slo`      | The configured TTFT and TPOT targets are feasible against the measured profiles.                                                                  |

The calibration path sets the first-token evidence check:

| `engine.first_token_calibration_path` | Preflight and router startup                                                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Empty                                 | Warning.                                                                                                                                   |
| Set                                   | Preflight fails and router startup stops when evidence for the running engines is incomplete or the deadline is at or below the candidate. |

### Pace gate

| Pace gate property | Value                          |
| ------------------ | ------------------------------ |
| Probes per engine  | Two cold one-token completions |
| Score              | The faster probe time          |
| Slowdown limit     | 1.5x                           |
| Failed probe       | Fails the gate                 |

| Successful probes | Each engine is compared against                                                                                        |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Three or more     | The fleet median, and for every profiled engine, that profile's prefill prediction at the exact `usage.prompt_tokens`. |
| Fewer than three  | For every profiled engine, that profile's prefill prediction at the exact `usage.prompt_tokens`.                       |

### KV transfer gates

[`narwhal-check`](../cli/Check.md) selects transfer pairs with these options:

| Option        | Pairs and probes                                          |
| ------------- | --------------------------------------------------------- |
| Default mesh  | Every eligible ordered pair.                              |
| `--ring`      | The pairs covering each eligible producer and consumer.   |
| `--repeats N` | `N` transfer probes per pair, with one verdict per probe. |

In the consume gate, a transfer across a role-permitted engine pair passes when:

- Decode produces its first generated token within `engine.first_token_timeout_s`.
- Decode finishes a valid stream with output.
- Prefill and decode complete within `serving.request_timeout_s`.

The gate reports the first-token time for each passing transfer.

| Failure                          | Action                                           |
| -------------------------------- | ------------------------------------------------ |
| The first-token deadline expires | Check the calibration artifact and the deadline. |
| Any other failure                | Keep the pair, the error, and the engine logs.   |

Keep the command, fleet file, preflight output, process identities, and profile store together.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
