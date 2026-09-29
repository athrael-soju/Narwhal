# `narwhal-check`

With `--fleet PATH`, `narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

| Option                            | Default                                                       | Contract                                                                                                                                                                                                                           |
| --------------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--version`                       |                                                               | Print the installed distribution version.                                                                                                                                                                                          |
| `--fleet PATH`                    | required for preflight, calibration, or evidence verification | Native fleet config JSON                                                                                                                                                                                                           |
| `--ring`                          | mesh                                                          | Mesh tests every eligible ordered pair; `--ring` uses ring coverage for `consume`.                                                                                                                                                 |
| `--repeats N`                     | `1`                                                           | Transfer probes per pair, clamped to a minimum of 1.                                                                                                                                                                               |
| `--no-kv`                         | false                                                         | Runs `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.                                                                                                                                                       |
| `--evidence-out PATH`             | gate output only                                              | Write process-bound full-mesh KV evidence to a fresh JSON path. Requires a fleet `engine_contract`; exclusive with `--ring`, `--no-kv` and `--verify-evidence`.                                                                    |
| `--verify-evidence PATH`          | run preflight                                                 | Verify saved directed KV evidence against current fleet/profile hashes and live process generations. Requires a fleet `engine_contract`; exclusive with `--evidence-out`. `--ring`, `--no-kv` and `--repeats` apply to new probes. |
| `--calibrate-first-token`         | false                                                         | Measure fresh directed handoffs across specified input lengths with a diagnostic first-token bound. Exclusive with directed KV evidence modes, `--ring`, `--no-kv`, and nondefault `--repeats`.                                    |
| `--input-tokens LIST`             | required for calibration                                      | Comma-separated positive target input lengths. Include the longest input admitted by the service. Each target must leave at least one output token within both engines' live context limits.                                       |
| `--samples N`                     | `100`                                                         | Fresh handoffs per pair and input length. At least 100 completed attempts per group are required for qualifying evidence.                                                                                                          |
| `--observation-timeout-s SECONDS` | required for calibration                                      | Diagnostic bound above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`.                                                                                                                                     |
| `--calibration-out PATH`          | required for calibration                                      | Write raw samples and group summaries to a fresh JSON path under `runs/`.                                                                                                                                                          |
| `--print-example-config`          | false                                                         | Prints the packaged annotated config before resolving the input config, then exits. Takes precedence over `--print-contract-versions`.                                                                                             |
| `--print-contract-versions`       | false                                                         | Prints the versioned JSON interface registry before resolving the input config, then exits.                                                                                                                                        |

KV checks send the prompt `"benchmark " * 64`, tokenized per model. Each `consume` probe initiates a handoff requesting up to four output tokens from an eligible peer. The probe sets vLLM's `min_tokens` and `ignore_eos`, so a model that ends the prompt at once still generates the requested tokens. Both legs of a probe share a unique `cache_salt` to prevent prefix-cache hits from masking transfer failures. Each `pace` repeat also uses a fresh salt to force full prefill. First-token generation must complete within `engine.first_token_timeout_s` followed by valid stream termination, while total probe time is bounded by `serving.request_timeout_s`. If the first-token deadline expires, the check reports the elapsed time, marks the transfer unconfirmed, and points to calibration.

When writing directed KV evidence, qualification requires each engine's
process generation to match across all pairs and repeats and to retain its
profile binding at the final identity check. The fleet and profile files
must retain the hashes captured before qualification.

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

`--verify-evidence` checks the retained qualification against current engine identities and configuration. Fresh transfer probes run through preflight, where `--evidence-out` records pair outcomes and requires every gate to pass for exit status 0.

The `slo` gate prices each profile's smallest measured decode cohort,
including its active-request and KV-token costs. Capacity output states the
request count used for the TPOT calculation.

Run [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before qualifying a fleet. The artifact contains raw attempts, group p99s and maxima, the candidate deadline, and engine generations.

Each group requires at least 100 completed attempts. Any failed attempt, generation change or generation-check error makes the artifact incomplete. The candidate must be strictly below `serving.request_timeout_s`. The command retains incomplete artifacts for diagnosis.

`--observation-timeout-s` bounds the wait for first output; `serving.request_timeout_s` bounds the complete attempt.

Calibration requests up to four output tokens per handoff and forces the engine to generate them. It reduces the count to fit the smaller live context limit of the producer and consumer. A target of `max_model_len - 1` requests one output token. Every successful sample must produce a generated token and finish a valid stream. Saved evidence must contain distinct attempt numbers covering the configured sample count in every group, stable engine generations, and successful final generation checks.

In default text mode, exit status 1 indicates a failed gate or operation; exit status 2 indicates invalid arguments or a fleet config read or validation error. JSON mode maps outcomes through the [command result contract](../Command-Results.md).

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.

| Output option | Default  | Purpose                                                               |
| ------------- | -------- | --------------------------------------------------------------------- |
| `--format`    | `"text"` | Select `json` for [versioned command results](../Command-Results.md). |
