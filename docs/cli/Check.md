# `narwhal-check`

Run `narwhal-check` before sending traffic to a fleet. It runs nine gates in order and exits 0 if none of them fails:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

```bash
narwhal-check --fleet fleet.json
```

## Options

### Preflight

| Option         | Default         | Description                                                                                                                                     |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH` | none            | Fleet config (JSON). Needed for preflight, calibration, and evidence checks.                                                                    |
| `--ring`       | off (full mesh) | Test a rotating set of pairs that covers every eligible producer and consumer. Full mesh tests every eligible pair. Affects only the `consume` gate. |
| `--repeats N`  | `1`             | Transfer probes per pair. Values below 1 count as 1.                                                                                            |
| `--no-kv`      | off             | Skip the `produce` and `consume` gates.                                                                                                         |

### KV evidence

| Option                   | Default | Description                                                                                                              |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `--evidence-out PATH`    | none    | Save KV transfer evidence to a new JSON file. See [Saving and verifying KV evidence](#saving-and-verifying-kv-evidence). |
| `--verify-evidence PATH` | none    | Check saved evidence against the running fleet.                                                                          |

### Calibration

These options need `--calibrate-first-token`, and `narwhal-check` rejects them without it. See [Calibrating the first-token deadline](#calibrating-the-first-token-deadline).

| Option                            | Default  | Description                                         |
| --------------------------------- | -------- | --------------------------------------------------- |
| `--calibrate-first-token`         | off      | Measure first-token latency and propose a deadline. |
| `--input-tokens LIST`             | required | Input lengths to test, separated by commas.         |
| `--samples N`                     | `100`    | Handoffs per engine pair and input length.          |
| `--observation-timeout-s SECONDS` | required | How long to wait for the first token.               |
| `--calibration-out PATH`          | required | A new JSON file under `runs/` for the results.      |

### Output and information

| Option                      | Default | Description                                                                                                         |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------- |
| `--format`                  | `text`  | Use `json` for [versioned command results](../Command-Results.md).                                                  |
| `--print-example-config`    | off     | Print the annotated example config and exit. Takes priority over `--print-contract-versions`.                        |
| `--print-contract-versions` | off     | Print the versioned registry of JSON interfaces and exit.                                                           |
| `--version`                 |         | Print the installed version.                                                                                        |

The two `--print-*` options run before the fleet file is read, so they need no `--fleet`.

## The KV gates

The `produce` and `consume` gates check that one engine can hand its KV cache to another. Every probe sends the prompt `"benchmark " * 64`. Its token count depends on the model's tokenizer.

Each `consume` probe starts a new handoff and asks a peer engine for up to four output tokens. The peer is a different engine from the producer, and the role rules allow it to decode. The peer must return at least one token and close the stream cleanly. The gate reports how long the first token took.

Decode has `engine.first_token_timeout_s` to produce its first token, and the whole request, prefill included, has `serving.request_timeout_s`. If the first-token deadline expires, the gate cannot tell whether the transfer worked and suggests running calibration.

With `--no-kv`, the other seven gates still run: `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.

## The `slo` gate

The `slo` gate prices the smallest decode cohort measured in each profile, counting both its active requests and its KV tokens. The capacity output shows how many requests it used to compute TPOT.

## Saving and verifying KV evidence

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

`--evidence-out` runs the full preflight and records the result for every pair. It exits 0 only if every gate passes. It requires an `engine_contract` in the fleet config and rejects `--ring`, `--no-kv`, or `--verify-evidence`.

Evidence is valid only when all of these hold:

- Every engine kept the same process generation across all pairs and repeats. A process generation identifies one running instance of an engine and changes on every restart.
- Every engine was still bound to its profile at the final identity check.
- The fleet and profile files still match the hashes taken before testing started.

`--verify-evidence` runs no transfers. It compares the saved evidence with the current fleet and profile files and with each engine's current process generation. It requires an `engine_contract` and rejects `--evidence-out`. It ignores `--ring`, `--no-kv` and `--repeats`.

## Calibrating the first-token deadline

Calibrate before qualifying a fleet. Calibration measures first-token latency over fresh handoffs at each input length you give it and proposes a deadline.

```bash
narwhal-check --fleet fleet.json --calibrate-first-token \
  --input-tokens 512,4096,16383 \
  --observation-timeout-s 30 \
  --calibration-out runs/first-token.json
```

Calibration rejects the evidence options, `--ring` and `--no-kv`. `--repeats` must stay at its default.

### Picking input lengths

Include the longest input your service accepts. Each length must leave room for at least one output token within the context limit that the running engines report, for both engines. A length of `max_model_len - 1` asks for one output token. Other lengths ask for up to four, or fewer if the smaller context limit requires it.

### Picking the timeout

`--observation-timeout-s` is how long to wait for the first output token. Set it above `engine.first_token_timeout_s` and no higher than `serving.request_timeout_s`, which limits the whole attempt.

### When a run counts

A group is one engine pair at one input length. Each group needs at least 100 completed attempts. Every attempt has to produce a token and close the stream cleanly, and attempt numbers must be unique and cover the configured `--samples` count.

A run is incomplete if any attempt fails, an engine's process generation changes, or a generation check errors. Incomplete runs are saved for diagnosis and cannot qualify a fleet. The proposed deadline must also be below `serving.request_timeout_s`.

### The output file

The file contains every raw attempt, the p99 and maximum for each group, the proposed deadline, and the engine generations. `narwhal-check` does not apply the deadline. Set `engine.first_token_timeout_s` above the proposed deadline, and point `engine.first_token_calibration_path` at the file. Preflight and router startup reject a configured deadline at or below the proposed one.

## Exit codes

In text mode:

| Code | Meaning                                                               |
| ---- | --------------------------------------------------------------------- |
| 0    | No gate failed.                                                       |
| 1    | A gate or operation failed.                                           |
| 2    | Invalid arguments, or the fleet config couldn't be read or validated. |

A skipped gate does not fail a text-mode run: `narwhal-check` prints `all gates pass, N skipped` and exits 0. For example, `contract` is skipped when the fleet declares no `engine_contract`. With `--evidence-out`, any skipped gate makes the run exit 1.

In JSON mode, exit codes follow the [command result contract](../Command-Results.md).
