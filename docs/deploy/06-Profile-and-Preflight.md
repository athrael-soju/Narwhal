# Gate F: Profile once and run the live KV contract

## Profile idle engines

Reserve the real engines and keep them otherwise idle. From the router:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288 \
  --decode-input-lens 512,4096,8192
```

Preparation derives `profiling-limits.json` from each engine's `--max-num-seqs`. The profiler bounds decode concurrency to that limit and adds it as a sweep point when needed. It reads `max_model_len` from each live `/tokenize` response, chooses lengths that leave one prefill output token or 64 decode output tokens, and checks actual tokenised length before each completion.

Compare the effective sweep with every checked serving plan. A shorter context or one-sequence limit requires changing launch policy or sweep before profiling. Use private engine URLs and credentials from the router environment. Warm the model and keep prefix caching disabled.

Prefill profiling measures one-token latency versus input length. Decode profiling varies prompt length and concurrency while the cohort stays in decode, then fits observed token intervals against active-request count plus estimated resident KV. Choose lengths and concurrency points that cover expected production traffic. Keep the `.samples.json` sidecar written alongside the profile store configured by `profiles.path`.

The controller holds a role change if its projected decode point falls outside measured profile range. Profile engine IDs must exactly match the configured fleet; mismatch stops startup.

At preflight and router startup, Narwhal matches each saved profile to the live engine's attested process identity and requires a new profile when that identity changes.

Set `slo.ttft_s` and `slo.tpot_s` from the service requirement and measured engine curves before preflight, keeping TPOT above the measured per-token floor. A target edit changes the preflight budget while the curves continue to describe the same engines.

## Run preflight

From the router, keep the engines otherwise idle and run:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The `consume` gate uses a fixed prompt and a fresh handoff for each role-permitted engine pair. The default mesh covers every eligible ordered pair; `--ring` selects pairs that cover each eligible producer and consumer. See the [CLI reference](../cli/Check.md) for supported options.

Decode must produce its first generated token within `engine.first_token_timeout_s` and finish a valid stream with output. If a transfer fails, retain the failing pair, error, and engine logs. Diagnose the path before changing the timeout; use [separate latency measurements](#calibrate-the-first-token-deadline) when the configured deadline needs calibration.

`--repeats N` repeats the transfer checks per pair. It reports pass/fail results without recording latency samples or sweeping input lengths. Retain the command, fleet document, preflight output, process identities, and profile store together.

The full preflight runs these gates:

| Gate       | What must pass                                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine answers inside configured health budget.                                                                                                                                                       |
| `contract` | Attestation matches current process and declared runtime.                                                                                                                                                   |
| `profile`  | Each saved generation digest matches its live engine; profile IDs match the fleet and measured decode errors stay within policy.                                                                            |
| `model`    | Every engine serves the configured model.                                                                                                                                                                   |
| `pace`     | Prefill latency remains within permitted slowdown. With at least three successful probes, comparison uses fleet median. Saved per-engine profiles are used when available; smaller fleets require profiles. |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                                                                                                   |
| `produce`  | Every tested producer can export a KV handoff.                                                                                                                                                              |
| `consume`  | Every tested peer can consume that handoff.                                                                                                                                                                 |
| `slo`      | Configured TTFT/TPOT targets are feasible against measured profiles.                                                                                                                                        |

Start the router after every required gate passes.

## Calibrate the first-token deadline

Calibration requires instrumented direct-engine probes; `narwhal-check` has no independent observation window. Use a fresh producer handoff for each sample and input lengths spanning the served context range, including the longest admitted input. Record the actual token count, prefill duration, and elapsed time from starting the decode HTTP request to its first generated token. The [Python engine API](../http-api/03-Backend-and-Failures.md#python-api) exposes the prefill and decode calls.

Use a diagnostic first-token observation bound above `engine.first_token_timeout_s` and within `serving.request_timeout_s`. Record engine errors and observation-window expiries with the failed path; diagnose them before selecting a serving deadline.

For each path and input length, collect at least 100 completed samples and retain all failed attempts. Compute `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` separately for each group, then set the candidate deadline above the largest result. Check that the deadline, measured prefill, and router overhead fit the service's client TTFT requirement. Edit `engine.first_token_timeout_s` in the fleet document and rerun preflight with that configuration.

Model, runtime, transport, or served-context changes require new samples. Retain the probe code, commands, raw timings, failures, fleet document, and process identities with the calibration evidence. The [GPU qualification report](../measure/07-GPU-Qualification.md) records one deployment's separate direct-probe measurements.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
