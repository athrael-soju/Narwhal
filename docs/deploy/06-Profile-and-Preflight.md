# Gate F: Characterise performance and run the live KV contract

## Measure engine service curves

Reserve the attested engines for the sweep. From the router:

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

The profiler stores each engine's curves with its verified attestation and process start. Preflight and router startup compare that digest with the live engine. An SLO or router setting change uses the existing curves with preflight against the revised fleet document.

After a failed sweep or an engine replacement, reuse the saved profile pair with the same sweep options and a fresh output path:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288 \
  --decode-input-lens 512,4096,8192 \
  --reuse runs/profiles.json \
  --out runs/profiles-recovered.json
```

The command checks each saved row against the live engine generation, context limit, sequence limit, and effective sweep. It measures engines with missing or changed rows and writes a complete profile and sample pair. Set `profiles.path` to the new file, then run preflight. Keep the source pair with the deployment record.

Set `slo.ttft_s` and `slo.tpot_s` from the service requirement and the profile samples for the deployed engine shape. Keep TPOT above the measured per-token floor.

## Run preflight

From the router:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

Run the full preflight mesh with the fleet document, profiles, and SLO targets planned for the trial. A fleet or target edit requires preflight against the revised document.

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
