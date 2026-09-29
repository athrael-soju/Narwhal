# Gate F: Profile once and run the live KV contract

Profile the idle engines, calibrate the first-token deadline, then run preflight before starting the router.

## Profile idle engines

Run these steps from the router shell.

1. Reserve the real engines and keep them otherwise idle.
2. Load the private engine URLs and credentials from the router environment.
3. Warm the model.
4. Pick input lengths and concurrency points that match production traffic; the command below uses 256 to 12288 tokens.
5. Run the profiler:

    ```bash
    .venv/bin/narwhal-profile \
      --fleet runs/deployment/fleet.json \
      --limits runs/deployment/profiling-limits.json \
      --prefill-lens 256,512,1024,2048,4096,8192,12288 \
      --decode-input-lens 512,4096,8192
    ```

6. Compare each printed effective sweep with every checked serving plan.

Gate B's `deploy_hosts.py prepare` derives `profiling-limits.json` from each engine's `--max-num-seqs`. The profiler bounds decode concurrency to that limit and adds the limit as a sweep point when needed. It reads `max_model_len` from each `/tokenize` response and keeps only the lengths that leave room for one prefill or 64 decode output tokens. Before each completion it checks the tokenised length. Prefix caching can stay on because the profiler salts each probe request.

Prefill profiling measures one-token latency against input length. Decode profiling varies prompt length and concurrency while the cohort stays in decode, then fits the observed token intervals to the active request count plus the estimated resident KV. The profiler stops with an error when the live context leaves fewer than three prefill lengths or two decode input lengths. Pass shorter `--prefill-lens` and `--decode-input-lens`. It also stops when `--max-num-seqs` allows fewer than two decode concurrency points; change the engine launch policy first.

Keep the `.samples.json` sidecar that the profiler writes beside the profile store configured by `profiles.path`.

The role controller holds a role change whose projected decode point falls outside the measured profile range. Startup stops if the profile engine IDs differ from the configured fleet.

Preflight and router startup match each saved profile to the live engine's attested process identity. A changed identity needs a new profile.

Before preflight, set the service-level objective (SLO) targets `slo.ttft_s` and `slo.tpot_s` from the service requirement and the measured engine curves, keeping the time per output token (TPOT) target above the measured per-token floor. See [Set production SLOs](../measure/02-Targets-and-Freeze.md#5-set-production-slos) for target selection.

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

Each role-permitted directed pair gets a unique prompt prefix and a fresh KV handoff. The probe asks for up to four output tokens, fewer if the smaller producer or consumer context limit requires it. The live tokenizer sizes each prompt. The artifact records input tokens, prefill time, decode-to-first-token time, and failed or expired attempts.

The command exits 0 when the artifact is complete: at least 100 samples per pair and input length, all attempts successful, process generations unchanged.

`serving.request_timeout_s` bounds each attempt, including prompt sizing, prefill, and decode completion. An attempt that expires after its first token records as `request_expired`, counts as failed, and stays out of the candidate calculation.

For each pair and input length, the command computes `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` and prints the largest result as the candidate deadline. Diagnose the failed transfers and observation expiries before using the candidate.

Set `engine.first_token_timeout_s` strictly above the candidate and `engine.first_token_calibration_path` to the artifact path. The deadline, the measured prefill, and the router overhead must fit the client time to first token (TTFT) requirement.

Repeat the calibration when the model, process generation, transport, or served context changes. Use a fresh artifact path, update the fleet config, and copy both to every router host before preflight.

Retain the fleet file, the raw artifact, the command, and the process identities together. See the [GPU qualification report](../measure/07-GPU-Qualification.md) for the measured results.

## Run preflight

Keep the engines otherwise idle and run the preflight from the router shell:

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
| `pace`     | Prefill latency stays within the permitted slowdown. See the [pace gate](#pace-gate).                                            |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                        |
| `produce`  | Every tested producer can export a KV handoff.                                                                                   |
| `consume`  | Every tested peer can consume that KV handoff.                                                                                   |
| `slo`      | The configured TTFT and TPOT targets are feasible against the measured profiles.                                                 |

An empty `engine.first_token_calibration_path` produces a warning in both preflight and router startup. With the path set, preflight and startup both require complete evidence for the running engines and a deadline above the candidate; either check failing stops the router.

### Pace gate

The pace gate sends each live engine two cold one-token completion probes and keeps the faster time. Its slowdown limit is 1.5x. With at least three successful probes, it compares each engine against the fleet median and, for every profiled engine, against that profile's prefill prediction at the exact `usage.prompt_tokens`. With fewer than three successful probes, the gate compares only against profiles. Any failed probe fails the gate.

### KV transfer gates

The default mesh tests every eligible ordered pair, while `--ring` selects the pairs covering each eligible producer and consumer. `--repeats N` runs fixed transfer probes for each pair and reports a verdict per probe. See the [`narwhal-check` reference](../cli/Check.md) for the supported options.

The consume gate uses a fixed prompt, a fresh cache salt, and a fresh handoff for each role-permitted engine pair. Decode must produce its first generated token within `engine.first_token_timeout_s` and finish a valid stream with output. Each consume attempt's prefill and decode must complete within `serving.request_timeout_s`, and the gate reports the first-token time for a passing transfer. An attempt that hits the first-token deadline fails as an unconfirmed transfer. Check the calibration artifact and the deadline. For other failures, keep the pair, the error, and the engine logs.

Keep the command, fleet file, preflight output, process identities, and profile store together.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
