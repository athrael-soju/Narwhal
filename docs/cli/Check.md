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

For each permitted crossed pair, `consume` times prefill and the first decode token, then reports a completed handoff above `engine.first_token_timeout_s` as a deadline violation with its measured latency. An observation expiry reports the ceiling and directs the operator to inspect the path or widen the bound; an engine error names the failed leg. Each path and input length gets its observed maximum and, after 100 completed samples, nearest-rank p99.
