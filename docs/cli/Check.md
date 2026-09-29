# `narwhal-check`

`narwhal-check` runs deployment gates against a Narwhal fleet before it takes traffic. Given `--fleet PATH`, it runs them in this order:

`reach` → `contract` → `profile` → `model` → `pace` → `tokenize` → `produce` → `consume` → `slo`

Text output prints one line per gate: `ok`, `FAIL`, `SKIP`, or `WARN`.

## Options

**General**

| Option                      | Default | Description                                                                                                                     |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `--fleet PATH`              | none    | Fleet configuration file. Required for preflight, calibration, and evidence verification.                                       |
| `--format`                  | `text`  | `text` or `json`. JSON output follows the [versioned command results](../Command-Results.md).                                   |
| `--version`                 |         | Print the installed version.                                                                                                    |
| `--print-example-config`    | off     | Print the packaged, annotated config and exit. Needs no `--fleet`. If you also pass `--print-contract-versions`, this one wins. |
| `--print-contract-versions` | off     | Print the versioned JSON interface registry and exit. Needs no `--fleet`.                                                       |

**Transfer probes** (these apply to the fresh probes that run during preflight)

| Option        | Default | Description                                                                                    |
| ------------- | ------- | ---------------------------------------------------------------------------------------------- |
| `--repeats N` | 1       | Probes per pair. Values below 1 are treated as 1.                                              |
| `--ring`      | off     | Test `consume` in a ring instead of the default mesh, which tries every eligible ordered pair. |
| `--no-kv`     | off     | Skip `produce` and `consume`. The other seven gates still run.                                 |

**KV evidence**

| Option                   | Default | Description                                                                                                                                           |
| ------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--evidence-out PATH`    | none    | Save full-mesh KV evidence to a new JSON file. Can't be combined with `--ring`, `--no-kv`, or `--verify-evidence`.                                    |
| `--verify-evidence PATH` | none    | Check saved evidence against the current fleet and profile hashes and the engines' live process generations. Can't be combined with `--evidence-out`. |

Both evidence options need an `engine_contract` in the fleet config.

**First-token calibration**

| Option                            | Default | Description                                                                                                                                                                               |
| --------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--calibrate-first-token`         | off     | Measure fresh KV handoffs at each input length and report a first-token deadline.                                                                                                         |
| `--input-tokens LIST`             | none    | Comma-separated target input lengths. Include the longest input your service admits. Every target must leave room for at least one output token within both engines' live context limits. |
| `--samples N`                     | 100     | Fresh handoffs per pair and input length.                                                                                                                                                 |
| `--observation-timeout-s SECONDS` | none    | How long to wait for first output. Must be above `engine.first_token_timeout_s` and no more than `serving.request_timeout_s`.                                                             |
| `--calibration-out PATH`          | none    | New JSON file under `runs/` for raw samples and group summaries.                                                                                                                          |

Calibration requires `--input-tokens`, `--observation-timeout-s`, and `--calibration-out`. It can't be combined with the evidence options, `--ring`, `--no-kv`, or a non-default `--repeats`. Conversely, `--samples` and the other calibration options only work alongside `--calibrate-first-token`.

## What a KV probe does

Each probe sends the prompt `"benchmark " * 64`, tokenized for the model under test. For `consume`, the probe starts a KV handoff from an eligible peer and asks for up to four output tokens.

A few details worth knowing:

- The probe sets vLLM's `min_tokens` and `ignore_eos`. A model that would otherwise stop right after the prompt still generates all four tokens.
- Both legs of a probe share a unique `cache_salt`, so a prefix-cache hit can't hide a failed transfer. Each `pace` repeat gets a fresh salt to force a full prefill.
- The first token must arrive within `engine.first_token_timeout_s`. The whole probe must finish within `serving.request_timeout_s`, and the stream must end cleanly.
- If the first-token deadline passes, the check reports the elapsed time, marks the transfer as unconfirmed, and points you to calibration.

## Recording and verifying KV evidence

Evidence ties the results to the running engines, so it stops being valid if an engine restarts or the config changes.

```bash
narwhal-check --fleet fleet.json --repeats 3 --evidence-out runs/kv-evidence.json
narwhal-check --fleet fleet.json --verify-evidence runs/kv-evidence.json
```

Pass `--evidence-out` and preflight saves the outcome of each pair, exiting 0 only when every gate passes. A run's evidence gets thrown out if an engine restarted partway through (its process generation changed between pairs or repeats), if an engine had lost its profile binding by the final identity check, or if the fleet or profile files no longer match the hashes taken before the run began.

`--verify-evidence` runs those same checks later, against the engines as they are now. Preflight's fresh transfer probes still run alongside it.

## First-token calibration

Run calibration before you record evidence for a fleet. The [calibration guide](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) has the full procedure.

The output file contains the raw attempts, the p99 and maximum for each group, the candidate deadline, and the engines' process generations.

Two timeouts apply here. `--observation-timeout-s` caps the wait for first output, and `serving.request_timeout_s` caps the whole attempt. Each handoff asks for four output tokens and forces the engine to produce them, or fewer if the producer's or consumer's live context limit is tighter. A target of `max_model_len - 1` therefore gets one token. A sample counts as successful only if it produced a token and the stream ended validly.

The artifact is rejected as evidence unless every group has at least 100 completed attempts, the attempt numbers are distinct and cover the configured sample count, the process generations held steady and the final checks passed, and the candidate deadline sits strictly below `serving.request_timeout_s`. If an attempt fails, a generation changes, or a generation check errors, the artifact is marked incomplete. The command still keeps it so you can see what went wrong.

## The `slo` gate

`slo` compares each profile's smallest measured decode cohort against the service-level objective, counting both active-request and KV-token costs. The capacity output shows the request count it used for the TPOT calculation.

## Exit codes

In text mode, a failed gate or operation exits 1, and invalid arguments or a fleet config that can't be read or validated exit 2. See the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes). In JSON mode, outcomes follow the [command result contract](../Command-Results.md).

Scripts can run `--print-contract-versions` to see which versioned artifacts and result formats are stable.