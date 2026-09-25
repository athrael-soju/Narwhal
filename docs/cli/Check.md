# `narwhal-check`

With `--fleet PATH`, `narwhal-check` runs deployment gates in this order:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

| Option                      | Default                | Contract                                                                                    |
| --------------------------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| `--version`                 |                        | Print the installed distribution version. |
| `--fleet PATH`              | required for preflight or evidence verification | Native fleet config JSON |
| `--ring`                    | mesh                   | Mesh tests every eligible ordered pair; `--ring` uses ring coverage for `consume`.           |
| `--repeats N`               | `1`                    | Transfer probes per pair, clamped to a minimum of 1.                                        |
| `--no-kv`                   | false                  | Runs `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.                |
| `--evidence-out PATH` | gate output only | Write process-bound full-mesh KV evidence to a fresh JSON path. Requires a fleet `engine_contract`; exclusive with `--ring`, `--no-kv` and `--verify-evidence`. |
| `--verify-evidence PATH` | run preflight | Verify saved directed KV evidence against current fleet/profile hashes and live process generations. Requires a fleet `engine_contract`; exclusive with `--evidence-out`. `--ring`, `--no-kv` and `--repeats` apply to new probes. |
| `--print-example-config`    | false                  | Prints the packaged annotated config before resolving the input config, then exits. Takes precedence over `--print-contract-versions`. |
| `--print-contract-versions` | false                  | Prints the versioned JSON interface registry before resolving the input config, then exits. |

KV checks use the fixed prompt `"benchmark " * 64`, whose token count depends on the model tokenizer. Each `consume` probe creates a fresh handoff and requests up to four output tokens from a distinct, role-permitted peer. Decode uses the fleet's `engine.first_token_timeout_s` and must return generated output followed by a valid stream termination.

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

`--repeats` repeats these checks without collecting latency samples. Input-length sweeps and an independent first-token observation window require [separately instrumented calibration probes](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).

In default text mode, exit status 1 indicates a failed gate or operation; exit status 2 indicates invalid arguments or a fleet config read or validation error. JSON mode maps outcomes through the [command result contract](../Command-Results.md).

Use gate tables to diagnose deployment failures and the contract registry and versioned artifacts for automation.

| Output option | Default | Purpose |
| --- | --- | --- |
| `--format` | `"text"` | Select `json` for [versioned command results](../Command-Results.md). |
