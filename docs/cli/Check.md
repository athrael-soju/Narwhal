# `narwhal-check`

Run `narwhal-check` before you send traffic to a fleet. It runs nine gates in order and exits 0 only if all of them pass:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

```bash
narwhal-check --fleet fleet.json
```

## Options

### Preflight

| Option         | Default         | Description                                                                                        |
| -------------- | --------------- | -------------------------------------------------------------------------------------------------- |
| `--fleet PATH` | none            | Fleet config (JSON). Needed for preflight, calibration, and evidence checks.                       |
| `--ring`       | off (full mesh) | Test each engine against one peer instead of every eligible pair. Only affects the `consume` gate. |
| `--repeats N`  | `1`             | Transfer probes per pair. Values below 1 count as 1.                                               |
| `--no-kv`      | off             | Skip the `produce` and `consume` gates.                                                            |

### KV evidence

| Option                   | Default | Description                                                                                                              |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `--evidence-out PATH`    | none    | Save KV transfer evidence to a new JSON file. See [Saving and verifying KV evidence](#saving-and-verifying-kv-evidence). |
| `--verify-evidence PATH` | none    | Check saved evidence against the running fleet.                                                                          |

### Calibration

These options only apply with `--calibrate-first-token`. See [Calibrating the first-token deadline](#calibrating-the-first-token-deadline).

| Option                            | Default | Description                                         |
| --------------------------------- | ------- | --------------------------------------------------- |
| `--calibrate-first-token`         | off     | Measure first-token latency and propose a deadline. |
| `--input-tokens LIST`             | none    | Input lengths to test, separated by commas.         |
| `--samples N`                     | `100`   | Handoffs per engine pair and input length.          |
| `--observation-timeout-s SECONDS` | none    | How long to wait for the first token.               |
| `--calibration-out PATH`          | none    | A new JSON file under `runs/` for the results.      |

### Output and information

| Option                      | Default | Description                                                                                                         |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------- |
| `--format`                  | `text`  | Use `json` for [versioned command results](../Command-Results.md).                                                  |
| `--print-example-config`    | off     | Print the annotated example config and exit. If you also pass `--print-contract-versions`, this one takes priority. |
| `--print-contract-versions` | off     | Print the versioned registry of JSON interfaces and exit.                                                           |
| `--version`                 |         | Print the installed version.                                                                                        |

The two `--print-*` options run before the fleet file is read, so they don't need `--fleet`.

## The KV gates

The `produce` and `consume` gates check that one engine can hand its KV cache to another. Every probe sends the same prompt, `"benchmark " * 64`. How many tokens that comes to depends on the model's tokenizer.

Each `consume` probe starts a new handoff and asks a different peer, one whose role allows it, for up to four output tokens. The peer has to return at least one token and close the stream cleanly. The gate reports how long the first token took.

Decode has `engine.first_token_timeout_s` to produce its first token, and the whole request, prefill included, has `serving.request_timeout_s`. If the first-token deadline runs out, the gate can't tell whether the transfer worked, and it will suggest running calibration.

With `--no-kv`, the other seven gates still run: `reach`, `contract`, `profile`, `model`, `pace`, `tokenize`, and `slo`.

## The `slo` gate

The `slo` gate prices the smallest decode cohort measured in each profile, counting both its active requests and its KV tokens. The capacity output shows how many requests it used to compute TPOT.

## Saving and verifying KV evidence

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

`--evidence-out` runs the full preflight and records the result for every pair. It exits 0 only if every gate passes. It needs an `engine_contract` in the fleet config and can't be used with `--ring`, `--no-kv`, or `--verify-evidence`.

Evidence is only valid if nothing changed while it was being collected:

- Every engine kept the same process generation across all pairs and repeats. A process generation is one run of an engine, and it changes whenever the engine restarts.
- Every engine was still bound to its profile at the final identity check.
- The fleet and profile files still match the hashes taken before testing started.

`--verify-evidence` doesn't run any transfers. It compares the saved evidence with the fleet and profile files as they are now and with each engine's current process generation. It also needs an `engine_contract` and can't be used with `--evidence-out`. If you pass `--ring`, `--no-kv`, or `--repeats` alongside it, they only apply to new probes.

## Calibrating the first-token deadline

Calibrate before you qualify a fleet. Calibration runs fresh handoffs at each input length you give it and proposes a deadline from what it measured.

```bash
narwhal-check --fleet fleet.json --calibrate-first-token \
  --input-tokens 512,4096,16384 \
  --observation-timeout-s 30 \
  --calibration-out runs/first-token.json
```

Calibration can't be combined with the evidence options, `--ring`, or `--no-kv`, and `--repeats` has to stay at its default.

### Picking input lengths

Include the longest input your service accepts. Each length has to leave room for at least one output token within the context limit of both engines, as the running engines report it. A length of `max_model_len - 1` asks for exactly one output token. Other lengths ask for up to four, or fewer if the smaller context limit requires it.

### Picking the timeout

`--observation-timeout-s` is how long to wait for the first output token. Set it above `engine.first_token_timeout_s` and no higher than `serving.request_timeout_s`, which limits the whole attempt.

### When a run counts

A group is one engine pair at one input length. Each group needs at least 100 completed attempts. Every sample has to produce a token and close the stream cleanly, and attempt numbers must be unique and cover the configured sample count.

A run is incomplete if any attempt fails, an engine's process generation changes, or a generation check errors. Incomplete runs are still saved so you can work out what went wrong, but they can't qualify a fleet. The proposed deadline also has to be strictly below `serving.request_timeout_s`.

### The output file

The file contains every raw attempt, the p99 and maximum for each group, the proposed deadline, and the engine generations. Narwhal doesn't apply the deadline for you. If you want to use it, copy it into `engine.first_token_timeout_s` yourself.

## Exit codes

In text mode:

| Code | Meaning                                                               |
| ---- | --------------------------------------------------------------------- |
| 0    | Every gate passed.                                                    |
| 1    | A gate or operation failed.                                           |
| 2    | Invalid arguments, or the fleet config couldn't be read or validated. |

In JSON mode, exit codes follow the [command result contract](../Command-Results.md).