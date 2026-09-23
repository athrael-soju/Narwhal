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

Exit status 1 indicates a failed gate or a fleet config read or validation error. Exit status 2 indicates invalid arguments.

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.
