# `narwhal-check`

With `--fleet PATH`, `narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `profile` → `profile generation` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | required for preflight | Native fleet config JSON                                                                    |
| `--ring`                    | mesh                   | Mesh tests every eligible ordered pair; `--ring` uses ring coverage for `consume`.           |
| `--repeats N`               | `1`                    | Transfer probes per pair, clamped to a minimum of 1.                                        |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.                |
| `--evidence-out PATH`       | absent                 | Write a private full-mesh result with live process identity and observed consumer NIXL transfer counters. Fails if a gate or pair is skipped, or if the path exists. |
| `--verify-evidence PATH`    | absent                 | Reject saved mesh evidence if the fleet, profiles, contract, eligible pairs, or any live engine process has changed. |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits.         |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

Exit status 1 indicates a failed gate or a fleet config read or validation error. Exit status 2 indicates invalid arguments.

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.

Use `--evidence-out` on a full mesh before serving, then use `--verify-evidence` to check that the saved result still describes the live fleet. A restart invalidates it. The evidence records directed producer and consumer identities, cache and connector source hashes, side-channel endpoint, transfer mode, NIXL transfer count and duration, output tokens, and failures. It does not infer the NIXL transport backend from those metrics; retain the engine startup log to identify the backend used by the run.
