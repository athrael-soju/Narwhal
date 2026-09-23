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

Preparation derives `profiling-limits.json` from each engine's `--max-num-seqs`. The profiler caps concurrency at that limit, includes the limit as a measurement point when necessary, reads `max_model_len` from each live `/tokenize` response, chooses lengths that leave one prefill output token or 64 decode output tokens, and checks exact tokenised length before each completion.

Compare the effective sweep with every checked serving plan. A shorter context or one-sequence limit requires changing launch policy or sweep before profiling. Use private engine URLs and credentials from the router environment. Warm the model and keep prefix caching disabled.

Prefill profiling measures one-token latency versus input length. Decode profiling varies prompt length and concurrency while the cohort stays in decode, then fits observed token intervals against active-request count plus estimated resident KV. Choose lengths and concurrency points that cover expected production traffic. Keep the sample sidecar.

The controller holds a role change if its projected decode point falls outside measured profile range. Profile engine IDs must exactly match the configured fleet; mismatch stops startup.

Do not restart engines or change runtime configuration between profiling, preflight, and trial. Any restart or runtime change requires a fresh profile set and new preflight.

Set `slo.ttft_s` and `slo.tpot_s` from light-load measurements on the deployed engine shape. Keep TPOT above the measured per-token floor.

## Run preflight

From the router:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

Use the same engine processes, fleet document, and profiles used for the trial.

| Gate       | What must pass                                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine answers inside configured health budget.                                                                                                                                                       |
| `contract` | Attestation matches current process and declared runtime.                                                                                                                                                   |
| `model`    | Every engine serves the configured model.                                                                                                                                                                   |
| `pace`     | Prefill latency remains within permitted slowdown. With at least three successful probes, comparison uses fleet median. Saved per-engine profiles are used when available; smaller fleets require profiles. |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                                                                                                   |
| `produce`  | Every tested producer can export a KV handoff.                                                                                                                                                              |
| `consume`  | Every tested peer can consume that handoff.                                                                                                                                                                 |
| `profile`  | Profile engine IDs exactly match fleet IDs.                                                                                                                                                                 |
| `slo`      | Configured TTFT/TPOT targets are feasible against measured profiles.                                                                                                                                        |

By default every eligible producer-consumer pair is exercised. `--ring` tests the configured maintenance ring. `--repeats` is for intermittent transfer diagnosis.

Do not start the router until all required gates pass.

Continue with [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
