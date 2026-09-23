# `narwhal-check`

`narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `model` → `pace` → `tokenize` → `produce` → `consume` → `profile` → `slo`

Preflight execution requires `--fleet`.

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required for preflight | Native fleet config JSON                                                                    |
| `--ring`                    | mesh                   | Uses ring coverage for `consume`. Without it, every eligible ordered pair is tested.        |
| `--repeats N`               | `1`                    | Transfer probes per pair. Values below 1 still run one probe.                               |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `model`, `pace`, `tokenize`, `profile`, and `slo`.                |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits.         |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

Exit status 1 reports either a failed gate or a fleet config that could not be read or validated. Exit status 2 reports invalid arguments.

Gate output and tables are designed for operator diagnosis. Automation should consume the contract registry and versioned artifacts.
