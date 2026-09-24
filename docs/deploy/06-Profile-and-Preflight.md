# Gate F: Characterise performance and run the live KV contract

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

Prefill profiling measures one-token latency versus input length. Decode profiling varies prompt length and concurrency while the cohort stays in decode, then fits observed token intervals against active-request count plus estimated resident KV. Choose lengths and concurrency points that cover expected production traffic. Keep the sample sidecar.

The controller holds a role change if its projected decode point falls outside measured profile range. Profile engine IDs must exactly match the configured fleet; mismatch stops startup.

Narwhal records the verified attestation and process start with each engine's samples, then checks the resulting generation digest at preflight and router startup. After a restart or runtime change, profile the affected engine process again, assemble a fleet-wide store, and rerun preflight.

Set `slo.ttft_s` and `slo.tpot_s` from light-load measurements on the deployed engine shape. Keep TPOT above the measured per-token floor.

## Calibrate the first-token deadline

Choose input targets spanning the served context range, including its longest admitted input. On an idle representative fleet, size crossed-handoff prompts with the live tokenizer and collect first-token times for every role-permitted producer-consumer path:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --handoff-input-tokens 256,4096,8192 --repeats 100 \
  --first-token-observation-s 12
```

Replace the example lengths and 12-second observation bound with the fleet's context range and a bound inside `serving.request_timeout_s`. The observation bound lets preflight identify a working transfer beyond the configured serving deadline. Serving still enforces `engine.first_token_timeout_s`, and `serving.request_timeout_s` still bounds the full client request.

For each length and permitted path, retain at least 100 working first-token samples. Compute nearest-rank p99 and set `engine.first_token_timeout_s` above `max(observed maximum, 1.2 × p99) + 0.5 seconds` across those groups. Check the resulting bound against the TTFT target after measured prefill; a bound that exhausts that target calls for a different SLO or engine path. Keep the fleet document, command, preflight output, engine process identities, and profile store together so the deadline can be traced to its samples.

An engine error or observation-window expiry fails the gate. Compute latency distributions from completed transfers after correcting the path. For an observation expiry, a second run with a larger bound inside the request deadline can expose a slow transfer or establish a higher observation ceiling. Rerun calibration after changes to the model, engine runtime, KV transport, or served context range.

## Run preflight

From the router:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

Run preflight with the fleet document and profiles planned for the trial.

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

By default every eligible producer-consumer pair is exercised. `--ring` tests the configured maintenance ring. `--repeats` is for intermittent transfer diagnosis.

Start the router after every required gate passes.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
