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

## Calibrate the first-token deadline

From the router host, keep the engines idle. With the example engine launch limit of 16,384 total tokens, inputs can use at most 16,383 tokens while leaving room for one output token. For a service admitting that input range, run:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16383 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-out runs/deployment/first-token-calibration.json
```

Choose input lengths that span the served range and include its longest admitted input. Every target must leave at least one output token within both engines' live context limits. Calibration requests up to four output tokens, reducing that count to fit the smaller producer/consumer context limit. Set the observation bound above the current `engine.first_token_timeout_s` and within `serving.request_timeout_s`.

The command gives each prompt a unique prefix, sizes it with the live tokenizer without exceeding the requested input length, then creates a fresh handoff for every sample on every role-permitted directed pair. It records actual input tokens, prefill time, elapsed time from starting decode to the first generated token, failed sizing or transfer attempts, and observation expiries. The output path must be new and should remain under the ignored `runs/` tree.

A complete artifact has at least 100 successful samples and no failed attempts per pair and input length, with unchanged engine generations. For each group, the command computes `max(observed maximum, 1.2 × nearest-rank p99) + 0.5 seconds`; its printed candidate is the largest group result. Diagnose failed transfers and observation expiries before using the candidate. A larger observation bound changes only the diagnostic run.

Each attempt, including prompt sizing, prefill, and decode completion, remains bounded by `serving.request_timeout_s`. An attempt that expires after producing its first token is retained as `request_expired` and excluded from the candidate calculation.

Set `engine.first_token_timeout_s` strictly above the candidate and set `engine.first_token_calibration_path` to the artifact path. Check that the selected deadline, measured prefill, and router overhead fit the service's client TTFT requirement. Model, engine generation, transport, and served-context changes require new samples. Write each replacement to a fresh path, then update the fleet configuration and distribute both files to every router host before preflight and startup. Retain the fleet document, raw artifact, command, and process identities together. The [GPU qualification report](../measure/07-GPU-Qualification.md) records one deployment's separate measurements.

## Run preflight

From the router, keep the engines otherwise idle and run:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

The `consume` gate uses a fixed prompt and a fresh handoff for each role-permitted engine pair. The default mesh covers every eligible ordered pair; `--ring` selects pairs that cover each eligible producer and consumer. See the [CLI reference](../cli/Check.md) for supported options.

Decode must produce its first generated token within `engine.first_token_timeout_s` and finish a valid stream with output. Each consume attempt's prefill and decode must complete within `serving.request_timeout_s`. The consume gate reports first-token time for a passing transfer. A deadline expiry means that the transfer remains unconfirmed; check the calibration artifact and configured deadline. Retain the failing pair, error, and engine logs for other transfer failures.

Preflight warns when no calibration path is configured and fails when configured evidence is stale or the deadline does not exceed its candidate. Router startup follows the same evidence check and logs a warning for an unconfigured path.

`--repeats N` runs fixed transfer probes for each pair and reports their verdicts. Retain the command, fleet document, preflight output, process identities, and profile store together.

The full preflight runs these gates:

| Gate       | What must pass                                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine returns HTTP 200 with the configured health HTTP I/O timeout.                                                                                                                                   |
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
