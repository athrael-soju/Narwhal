# Gate F: Profile once and run the live KV contract

Complete these stages before starting the router:

1. Profile the idle engines.
2. Calibrate the first-token deadline.
3. Run preflight.

## Profile idle engines

Run these steps from the router shell.

1. Reserve the real engines and keep them otherwise idle.
2. Load the private engine URLs and credentials from the router environment.
3. Warm the model.
4. Pick input lengths and concurrency points that match production traffic. The command below uses 256 to 12288 tokens.
5. Run the profiler:

    ```bash
    .venv/bin/narwhal-profile \
      --fleet runs/deployment/fleet.json \
      --limits runs/deployment/profiling-limits.json \
      --prefill-lens 256,512,1024,2048,4096,8192,12288 \
      --decode-input-lens 512,4096,8192
    ```

6. Compare each printed effective sweep with every checked serving plan.

Gate B's `deploy_hosts.py prepare` derives `profiling-limits.json` from each engine's `--max-num-seqs`. `max_model_len` comes from each `/tokenize` response. The effective sweep has these bounds:

| Sweep input          | Bound                                                                                   |
| -------------------- | --------------------------------------------------------------------------------------- |
| Decode concurrency   | At most `--max-num-seqs`. The limit joins the sweep as a point when needed.            |
| Prefill lengths      | Input plus one output token fits `max_model_len`.                                       |
| Decode input lengths | Input plus 64 output tokens fits `max_model_len`.                                       |

Prefix caching can stay on during profiling.

| Profiler error                                                                    | Fix                                                        |
| --------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| The live context leaves fewer than three prefill lengths or two decode input lengths | Pass shorter `--prefill-lens` and `--decode-input-lens`. |
| `--max-num-seqs` allows fewer than two decode concurrency points                  | Change the engine launch policy.                           |

Keep the `.samples.json` sidecar that the profiler writes beside the profile store configured by `profiles.path`.

| Condition                                                                  | Result                              |
| -------------------------------------------------------------------------- | ----------------------------------- |
| Profile engine IDs differ from the configured fleet                        | Router startup stops.               |
| The live engine's attested process identity differs from its saved profile | Preflight and router startup require a new profile. |
| A role change's projected decode point falls outside the measured profile range | The role controller holds the change. |

Before preflight, [set the service-level objective (SLO) targets](../measure/02-Targets-and-Freeze.md#5-set-production-slos) `slo.ttft_s` and `slo.tpot_s` from the service requirement and the measured engine curves. Keep the time per output token (TPOT) target above the measured per-token floor.

## Calibrate the first-token deadline

Keep the engines idle. Set these inputs:

- Input lengths spanning the served range, including its longest admitted input. Each must leave room for one output token in both engines.
- An observation bound, `--observation-timeout-s`, above the current `engine.first_token_timeout_s` and within `serving.request_timeout_s`.
- A fresh artifact path under the ignored `runs/` tree.

The example assumes an engine launch limit of 16,384 total tokens and inputs up to 16,383 tokens. Run the calibration from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16383 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

The calibration covers every role-permitted directed pair. The artifact records input tokens, prefill time, decode-to-first-token time, and failed or expired attempts.

The command exits 0 when the artifact is complete:

- at least 100 samples per pair and input length;
- every attempt succeeded;
- process generations stayed the same.

`serving.request_timeout_s` bounds each attempt's prompt sizing, prefill, and decode completion. An attempt that expires after its first token records as `request_expired` and counts as failed. The candidate calculation excludes it.

For each pair and input length, the command computes `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` and prints the largest result as the candidate deadline. Diagnose the failed transfers and observation expiries before using the candidate.

| Field                                  | Value                          |
| -------------------------------------- | ------------------------------ |
| `engine.first_token_timeout_s`         | Strictly above the candidate.  |
| `engine.first_token_calibration_path`  | The artifact path.             |

The deadline, the measured prefill, and the router overhead must fit the client time to first token (TTFT) requirement.

Repeat the calibration when the model, process generation, transport, or served context changes:

1. Use a fresh artifact path.
2. Update the fleet config.
3. Copy the artifact and the fleet config to every router host before preflight.

Retain the fleet file, the raw artifact, the command, and the process identities together.

## Run preflight

Keep the engines otherwise idle. Run the preflight from the router shell:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The full preflight runs these gates:

| Gate       | What must pass                                                                                                                   |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine returns HTTP 200 within the configured health HTTP I/O timeout.                                                     |
| `contract` | Attestation matches the current process and declared runtime.                                                                    |
| `profile`  | Each saved process generation digest matches its live engine; the profile IDs match the fleet and the measured decode errors stay within policy. |
| `model`    | Every engine serves the configured model.                                                                                        |
| `pace`     | Prefill latency stays within the [permitted slowdown](#pace-gate).                                                               |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                        |
| `produce`  | Every tested producer can export a KV handoff.                                                                                   |
| `consume`  | Every tested peer can consume that KV handoff.                                                                                   |
| `slo`      | The configured TTFT and TPOT targets are feasible against the measured profiles.                                                 |

The calibration path sets the first-token evidence check:

| `engine.first_token_calibration_path` | Preflight and router startup                                                                                               |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Empty                                 | Warning.                                                                                                                   |
| Set                                   | Require complete evidence for the running engines and a deadline above the candidate. A failed check stops the router.    |

### Pace gate

The pace gate sends each live engine two cold one-token completion probes and scores the faster time. The slowdown limit is 1.5x. Any failed probe fails the gate.

| Successful probes | Each engine is compared against                                                                                              |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Three or more     | The fleet median, and for every profiled engine, that profile's prefill prediction at the exact `usage.prompt_tokens`.      |
| Fewer than three  | For every profiled engine, that profile's prefill prediction.                                                                |

### KV transfer gates

[`narwhal-check`](../cli/Check.md) selects transfer pairs with these options:

| Option          | Pairs and probes                                                         |
| --------------- | ------------------------------------------------------------------------ |
| Default mesh    | Every eligible ordered pair.                                             |
| `--ring`        | The pairs covering each eligible producer and consumer.                  |
| `--repeats N`   | Fixed transfer probes for each pair, with a verdict per probe.           |

The consume gate tests each role-permitted engine pair. A transfer passes when:

- decode produces its first generated token within `engine.first_token_timeout_s`;
- decode finishes a valid stream with output;
- prefill and decode complete within `serving.request_timeout_s`.

The gate reports the first-token time for each passing transfer.

| Failure                                                          | Action                                             |
| ---------------------------------------------------------------- | -------------------------------------------------- |
| Unconfirmed transfer at the first-token deadline                 | Check the calibration artifact and the deadline.   |
| Any other failure                                                | Keep the pair, the error, and the engine logs.     |

Keep the command, fleet file, preflight output, process identities, and profile store together.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
