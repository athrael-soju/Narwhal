# Gate F: Profile the engines and run the live KV contract

This gate measures how each engine performs, sets a first-token deadline from real handoffs, and then runs preflight: the full set of checks the router needs to pass before it can start.

## Profile idle engines

Reserve the engines so nothing else reaches them while you profile. Warm the model before you start. Prefix caching is already off in the launch plan, so leave it that way. Then, from the router:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288 \
  --decode-input-lens 512,4096,8192
```

The profiler contacts the engines using the private URLs and credentials in the router environment. `profiling-limits.json` was written in Gate B from each engine's `--max-num-seqs`. The profiler never pushes decode concurrency past that limit, and it adds the limit itself as a sweep point if it isn't already in the sweep. It reads `max_model_len` from each engine's `/tokenize` response and picks lengths that leave room for one output token in prefill or 64 in decode. Before each completion, it checks the real tokenized length.

Before you rely on the sweep, compare it with each engine's checked serving plan. If an engine has a shorter context than the sweep expects, or allows only one sequence, change the launch policy or the sweep first.

Prefill profiling measures the latency of generating one token as input length grows. Decode profiling varies prompt length and concurrency while the whole cohort stays in decode. It then fits the observed token intervals against the number of active requests plus the estimated resident KV. Choose lengths and concurrency levels that cover the traffic you expect in production. If the controller projects a decode point outside the measured range, it holds the role change instead of guessing. Keep the `.samples.json` file the profiler writes next to the profile store at `profiles.path`.

The engine IDs in the profile must match the fleet exactly, or startup stops. At preflight and at router startup, Narwhal matches each saved profile to the live engine's attested process identity. If an engine is restarted, it has a new identity and needs a new profile.

Before preflight, set `slo.ttft_s` and `slo.tpot_s` from the service requirement and the measured curves. Keep TPOT above the measured per-token floor. If you change these targets later, you only need to rerun preflight. The profiles still describe the same engines.

## Calibrate the first-token deadline

The router needs a deadline for the first token after a KV handoff, and this step derives one from real handoffs. Keep the engines idle and run it from the router host.

In the example launch policy, an engine allows 16,384 tokens in total, so the longest input is 16,383 tokens, which leaves room for one output token. For a service that accepts inputs up to that length:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16383 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

Choose input lengths that span the range you serve, including the longest input you accept. Every length must leave at least one output token within the context limits of both engines in a pair. Calibration asks for up to four output tokens, and fewer when the smaller of the producer's and consumer's context limits requires it.

Set `--observation-timeout-s` above the current `engine.first_token_timeout_s` and within `serving.request_timeout_s`. This timeout only applies to the calibration run, so raising it doesn't change how the router serves.

Each sample uses a unique prompt prefix and a fresh handoff on a directed pair the roles allow. The live tokenizer sizes each prompt to no more than the requested token count. The command probes every allowed pair and records the actual input tokens, the prefill time, the time from starting decode to the first generated token, and any attempts that failed or expired. Write the artifact to a new path under `runs/`, which Git ignores.

Every attempt, from prompt sizing through prefill and decode, is limited by `serving.request_timeout_s`. An attempt that runs out of time after producing its first token is recorded as `request_expired` and left out of the calculation.

The artifact is complete when there are at least 100 successful samples for each pair and input length, every attempt succeeded, and no engine generation changed during the run. For each pair and length, the command computes `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds` and prints the largest of these as the candidate. If any transfers failed or any observations expired, find out why before you use the candidate.

Then:

- set `engine.first_token_timeout_s` to a value strictly greater than the candidate;
- set `engine.first_token_calibration_path` to the artifact's path;
- make sure the deadline, the measured prefill time, and the router's overhead together still fit the client's TTFT requirement.

Recalibrate after any change to the model, an engine generation, the transport, or the served context. Write the new artifact to a new path, update the fleet configuration, and copy both files to every router host before running preflight or starting the router.

Keep the fleet document, the raw artifact, the command you ran, and the process identities together. Measured results are in the [GPU qualification report](../measure/07-GPU-Qualification.md).

## Run preflight

From the router, with the engines otherwise idle:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The `consume` gate sends a fixed prompt through a fresh handoff for each engine pair the roles allow. By default it tests every eligible ordered pair. `--ring` tests a smaller set of pairs that still covers every eligible producer and consumer. The [CLI reference](../cli/Check.md) lists the other options.

For a transfer to pass, decode has to produce its first token within `engine.first_token_timeout_s` and finish a valid stream with output, and each attempt's prefill and decode have to finish within `serving.request_timeout_s`. For each passing transfer, the gate reports the first-token time. If a transfer misses the deadline, it hasn't been confirmed either way, so check the calibration artifact and the configured deadline. For any other transfer failure, keep the failing pair, the error, and the engine logs.

If no calibration path is configured, preflight and router startup both warn. If a path is configured, both require the evidence to be complete and to match the running engines, and the configured deadline must be strictly above the artifact's candidate. If either condition isn't met, preflight fails and the router refuses to start.

`--repeats N` runs the fixed transfer probe N times per pair and reports each result. Keep the command, fleet document, preflight output, process identities, and profile store together.

A full preflight runs these gates:

| Gate       | What must pass                                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine returns HTTP 200 with the configured health HTTP I/O timeout.                                                                                                                                  |
| `contract` | Attestation matches current process and declared runtime.                                                                                                                                                   |
| `profile`  | Each saved generation digest matches its live engine; profile IDs match the fleet and measured decode errors stay within policy.                                                                            |
| `model`    | Every engine serves the configured model.                                                                                                                                                                   |
| `pace`     | Prefill latency remains within permitted slowdown. With at least three successful probes, comparison uses fleet median. Saved per-engine profiles are used when available; smaller fleets require profiles. |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                                                                                                   |
| `produce`  | Every tested producer can export a KV handoff.                                                                                                                                                              |
| `consume`  | Every tested peer can consume that handoff.                                                                                                                                                                 |
| `slo`      | Configured TTFT/TPOT targets are feasible against measured profiles.                                                                                                                                        |

Once every required gate passes, you can start the router.

Next: [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
