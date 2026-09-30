# Gate F: Profile the engines and run the live KV contract

This gate profiles each engine, calibrates the first-token deadline from measured KV handoffs, and runs preflight. The router will not start until preflight passes.

## Profile idle engines

Before profiling:

- Reserve the engines so nothing else reaches them.
- Warm the model.
- Keep prefix caching off, as set in the launch plan.

Run from the router:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288 \
  --decode-input-lens 512,4096,8192
```

The profiler works as follows:

- It contacts the engines using the private URLs and credentials in the router environment.
- It reads the concurrency limit from `profiling-limits.json`, which Gate B wrote from each engine's `--max-num-seqs`. Decode concurrency never exceeds that limit, and the limit is added to the sweep if it is missing.
- It reads `max_model_len` from each engine's `/tokenize` response and picks lengths that leave room for one output token in prefill or 64 output tokens in decode.
- It checks the real tokenized length before each completion.

Confirm the sweep fits each engine's serving plan. If an engine has a shorter context than the longest sweep length or allows only one sequence, change the launch policy or the sweep before profiling.

Prefill profiling measures the latency of generating one token as input length grows. Decode profiling varies prompt length and concurrency while the whole cohort stays in decode, then fits the observed token intervals against the number of active requests plus the estimated resident KV. Cover the expected production traffic with the lengths and concurrency levels. If the controller projects a decode point outside the measured range, it holds the role change.

Keep the `.samples.json` file the profiler writes next to the profile store at `profiles.path`.

Profiles must satisfy these requirements:

- Engine IDs in the profile are identical to the fleet's engine IDs. Otherwise the router refuses to start.
- At preflight and at router startup, each saved profile is matched to the live engine's attested process identity.
- A restarted engine has a new identity and needs a new profile.

Before preflight, set `slo.ttft_s` and `slo.tpot_s` from the service requirement and the measured curves. Set TPOT above the measured minimum inter-token interval. Changing the SLOs later requires a preflight rerun; the profiles stay valid.

## Calibrate the first-token deadline

Calibration derives the first-token deadline (`engine.first_token_timeout_s`) from measured KV handoffs. Run it from a router host with the engines idle.

The example launch policy allows 16,384 total tokens per engine, so the longest input is 16,383 tokens (one token reserved for output). This example serves inputs up to that length:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16383 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

Cover the range you serve, including the longest input you accept. Each length must leave at least one output token within the context limits of both engines in a pair. Calibration requests up to four output tokens, fewer when the smaller of the producer's and consumer's context limits requires it.

Set `--observation-timeout-s` above the current `engine.first_token_timeout_s` and within `serving.request_timeout_s`. It applies only to calibration.

Method:

- Each sample uses a unique prompt prefix and a fresh handoff on a directed pair the roles allow.
- The live tokenizer sizes each prompt to no more than the requested token count.
- The command probes every allowed pair.

Recorded per sample: the actual input tokens, the prefill time, and the time from starting decode to the first generated token. Failed and expired attempts are also recorded.

Every attempt, from prompt sizing through prefill and decode, is limited by `serving.request_timeout_s`. An attempt that exceeds it after producing its first token is recorded as `request_expired`, excluded from the calculation, and makes the calibration artifact incomplete.

Write the artifact to a new path under `runs/`, which Git ignores.

The calibration artifact is complete when all of these hold:

- Each pair and input length has at least 100 successful samples.
- Every attempt succeeded.
- No engine generation changed during the run.

For each pair and length, the command computes this value and prints the largest result as the candidate deadline:

```text
max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds
```

Investigate every failed transfer and expired attempt before using the candidate; the artifact stays incomplete until every attempt succeeds.

Then:

1. Set `engine.first_token_timeout_s` strictly above the candidate deadline.
2. Set `engine.first_token_calibration_path` to the artifact's path.
3. Verify that the deadline, the measured prefill time, and the router's overhead together fit the client's TTFT requirement.

Recalibrate after any change to the model, an engine generation, the transport, or the served context. Write the new artifact to a new path, update the fleet configuration, and copy both the new artifact and the updated fleet configuration to every router host before running preflight or starting the router.

## Run preflight

Run from the router with the engines idle:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The `consume` gate sends a fixed prompt through a fresh handoff for each engine pair the roles allow. By default it tests every eligible ordered pair. `--ring` tests a subset of pairs that covers each eligible producer and consumer. `--repeats N` runs the transfer probe N times per pair and reports each result. The [CLI reference](../cli/Check.md) lists the other options.

A transfer passes when:

- Decode produces its first token within `engine.first_token_timeout_s` and finishes a valid stream with output.
- Each attempt's prefill and decode finish within `serving.request_timeout_s`.

For each passing transfer, the gate reports the first-token time. A deadline miss is inconclusive: check the calibration artifact and the configured deadline. For any other transfer failure, keep the failing pair, the error, and the engine logs.

If no calibration path is configured, preflight and router startup both warn. If a path is configured, both require:

- The calibration artifact is complete.
- The calibration artifact matches the running engines.
- The configured deadline is strictly above the artifact's candidate deadline.

If any requirement is not met, preflight fails and the router refuses to start.

A full preflight runs these gates:

| Gate       | What must pass                                                                                                                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine returns HTTP 200 within the configured health HTTP I/O timeout.                                                                                                                                         |
| `contract` | Attestation matches the current process and the declared runtime.                                                                                                                                                    |
| `profile`  | Each saved generation digest matches its live engine, profile IDs match the fleet, and measured decode errors stay within policy.                                                                                    |
| `model`    | Every engine serves the configured model.                                                                                                                                                                            |
| `pace`     | Prefill latency stays within the permitted slowdown. With at least three successful probes, the comparison uses the fleet median. Saved per-engine profiles are used when available. Smaller fleets require profiles. |
| `tokenize` | Exact input sizing succeeds, when enabled.                                                                                                                                                                           |
| `produce`  | Every tested producer can export a KV handoff.                                                                                                                                                                       |
| `consume`  | Every tested peer can consume that handoff.                                                                                                                                                                          |
| `slo`      | The configured TTFT and TPOT targets are feasible against the measured profiles.                                                                                                                                     |

When all gates pass, start the router.

Record the evidence: the fleet document, the raw calibration artifact, the commands you ran, the preflight output, the process identities, and the profile store. Measured results are in the [GPU qualification report](../measure/07-GPU-Qualification.md).

Next: [Gate G: Start the service and validate capacity through the private path](07-Serve-and-Measure.md).
