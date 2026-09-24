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

Prefill profiling measures one-token latency versus input length. Decode profiling varies prompt length and concurrency while the cohort stays in decode, then fits observed token intervals against active-request count plus estimated resident KV. Choose lengths and concurrency points that cover expected production traffic. Keep the sample sidecar.

The controller holds a role change if its projected decode point falls outside measured profile range. Profile engine IDs must exactly match the configured fleet; mismatch stops startup.

At preflight and router startup, Narwhal matches each saved profile to the live engine's attested process identity and requires a new profile when that identity changes.

Set `slo.ttft_s` and `slo.tpot_s` from the service requirement and measured engine curves before preflight, keeping TPOT above the measured per-token floor. A target edit changes the preflight budget while the curves continue to describe the same engines.

## Run preflight

From the router, use input targets spanning the served context range, including the longest admitted input:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --handoff-input-tokens 256,4096,8192 \
  --first-token-observation-s 12
```

Set the input lengths to the fleet's served context range and the observation bound between `engine.first_token_timeout_s` and `serving.request_timeout_s`. The `consume` gate times each role-permitted crossed handoff through its first decode token; its independent observation bound records a working transfer beyond the serving deadline.

When a handoff completes after the serving deadline, compare its first-token time plus measured prefill with the TTFT target, set `engine.first_token_timeout_s` within that budget, and rerun preflight against the edited fleet document. An engine error or observation-window expiry identifies the path to diagnose; a wider observation bound within the request deadline can expose a slow transfer. The fleet edit retains the running engines, attestation, and profiles.

For a deadline based on a latency distribution, `--repeats 100` collects 100 working samples per path and length; set the deadline above `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds`. Model, runtime, transport, or served-context changes require new samples. Retain the command, fleet document, preflight output, process identities, and profile store together.

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

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
