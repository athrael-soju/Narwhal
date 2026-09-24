# `narwhal-check`

With `--fleet PATH`, `narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required for preflight | Native fleet config JSON                                                                    |
| `--ring`                    | mesh                   | Mesh tests every eligible ordered pair; `--ring` uses ring coverage for `consume`.           |
| `--repeats N`               | `1`                    | Transfer probes per pair, clamped to a minimum of 1.                                        |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.                |
| `--first-token-observation-s S` | larger of TTFT target and configured first-token deadline, capped by request timeout | Bounds a preflight decode observation. Serving keeps the configured first-token deadline. |
| `--handoff-input-tokens N,...` | default probe prompt | Sizes crossed-handoff prompts near each token target through the producer's tokenizer. |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits.         |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

Exit status 1 indicates a failed gate or a fleet config read or validation error. Exit status 2 indicates invalid arguments.

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.

The `consume` gate records prefill and first-token time for each permitted transfer. A completed transfer whose first token exceeds `engine.first_token_timeout_s` fails with a deadline violation and retains its measured time. An observation-window expiry reports its ceiling and directs the operator to inspect the path or widen the bound. An engine error reports the failed leg. The gate prints the maximum first-token time for each path and input length, plus nearest-rank p99 after at least 100 working samples on that path.
