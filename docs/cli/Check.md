# `narwhal-check`

With `--fleet PATH`, `narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required for preflight | Native fleet config JSON                                                                    |
| `--ring`                    | mesh                   | Mesh tests every eligible ordered pair; `--ring` uses ring coverage for `consume`.           |
| `--repeats N`               | `1`                    | Transfer probes per pair, clamped to a minimum of 1.                                        |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.                |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits.         |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

KV checks use the fixed prompt `"benchmark " * 64`, whose token count depends on the model tokenizer. Each `consume` probe creates a fresh handoff and requests up to four output tokens from a distinct, role-permitted peer. Decode uses the fleet's `engine.first_token_timeout_s` and must return generated output followed by a valid stream termination.

When writing directed KV evidence, qualification requires each engine's
process generation to match across all pairs and repeats and to retain its
profile binding at the final identity check. The fleet and profile files
must retain the hashes captured before qualification.

The `slo` gate prices each profile's smallest measured decode cohort,
including its active-request and KV-token costs. Capacity output states the
request count used for the TPOT calculation.

`--repeats` repeats these checks without collecting latency samples. Input-length sweeps and an independent first-token observation window require [separately instrumented calibration probes](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).

Exit status 1 indicates a failed gate or a fleet config read or validation error. Exit status 2 indicates invalid arguments.

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.
