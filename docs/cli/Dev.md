# `narwhal dev`

`narwhal dev` manages a local development fleet on a single NVIDIA GPU: init, up, verify, status, and down. It uses the native CUDA backend and runs on Ubuntu, or Ubuntu under WSL2. See [Narwhal dev](../Dev-Runtime.md).

The installed template starts two engines, one prefill and one decode, and is sized for GPUs with 8 GB of VRAM or less. For a larger setup, see the [RTX 5090 reference](../dev/08-RTX-5090-Reference.md), which documents a measured four-engine configuration.

A template sets the model, runtime, and memory budget. To require a specific GPU model, set `gpu.product` in the template.

## Quick start

```bash
narwhal dev init --model /path/to/Qwen3.5-0.8B-Q4_K_M.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

If your engines require an API key, export `NARWHAL_ENGINE_API_KEY` before `up`. The generated fleet config reads the key from that variable, so keep it set for every command that profiles, verifies, or routes.

To print the installed version, run `narwhal --version`.

## Commands

### `init`

`init` creates a private instance directory containing:

- the model and runtime pins
- the memory budget
- the unique port assignments
- the engine launch records
- the fleet config

Re-running `init` on an existing instance keeps its files and edits. Omitted settings keep their saved values. If the passed settings match, `init` reports `reused`. If any differ, it exits with code 2 and lists them.

To change settings, create a new instance (see also [Changing the model or runtime](#changing-the-model-or-runtime)):

```bash
narwhal dev init --instance runs/new-instance
```

### `up`

`up` checks the ports and the runtime, then:

1. Starts each engine.
2. Captures a live attestation from each one.
3. Profiles every role split that has at least one prefill engine and one decode engine.
4. Starts the router.

On success, `up` reports `launched`.

### `verify`

`verify` runs the full preflight on every eligible KV transfer path in both directions, checks the profiles against the running processes, sends an arithmetic request through the router, and saves the router and engine metrics. If everything passes, it reports `ready`.

### `status`

`status` reports `launched` while the instance's processes pass their HTTP health checks. Once `verify` has succeeded and its transfer evidence is still current, it reports `ready`.

If `verify` fails, it saves the reason, the evidence directory, and the failure time in `lifecycle.json` and in that attempt's `failure.json`. Until a later `verify` or `down` succeeds, `status` reports `degraded`, includes the saved record as `verification_failure`, and adds the reason to `problems`. The earlier failure stays in place while a new `verify` is running.

### `down`

`down` checks the recorded boot ID and process start times and stops only processes the instance started. It sends SIGTERM to each process group, waits for the group's workers and leader to exit, and sends SIGKILL to anything still running. It then reports `stopped`.

## Options

| Option                     | Default                                                                                  | Description                                                                                                                                                                                 |
| -------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--instance`               | `runs/dev`                                                                               | Instance directory. Works with every subcommand.                                                                                                                                            |
| `--template`               | installed small-GPU template                                                             | Template for `init`, with versioned model, tokenizer, runtime, profiling, and memory settings.                                                                                              |
| `--model`                  | pinned file in the Hugging Face cache                                                    | GGUF model file. Its file name and checksum must match the template.                                                                                                                        |
| `--model-dir`              | pinned tokenizer cache directory                                                         | Directory with the tokenizer and config files.                                                                                                                                              |
| `--gpu`                    | the GPU found, if there's only one                                                       | Physical GPU UUID.                                                                                                                                                                          |
| `--engine-count`           | template value (2)                                                                       | Number of engine processes, from 2 to 8.                                                                                                                                                    |
| `--port-base`              | template ports: router 18000, engines from 18101, attestation from 18201, NIXL from 5701 | With `--port-base P`, the router uses `P`, engine HTTP ports start at `P+1`, attestation ports at `P+101`, and NIXL ports at `P+201`. Every port must be different and between 1 and 65535. |
| `--gpu-memory-utilization` | template value (0.35)                                                                    | Share of the GPU's total memory for each engine. Greater than 0 and at most 1.                                                                                                         |
| `--device-allowance`       | template value (0.8)                                                                     | Share of the GPU's total memory that all engines together can use, at most 1. It caps both the sum of the per-engine shares and the total memory growth measured at startup.                  |
| `--interface`              | `eth0`                                                                                   | Network interface for NIXL/UCX.                                                                                                                                                             |
| `--format`                 | `text`                                                                                   | Use `json` for [versioned command results](../Command-Results.md).                                                                                                                          |

### Memory budget

Memory fractions must be finite numbers. `init` sums the per-engine fractions using exact decimal arithmetic and fails if the total exceeds the device allowance. For example, three engines at `0.1` fit an allowance of `0.3`.

It also checks that the GPU has enough free memory for the allowance times the GPU's total memory, plus the template's `gpu.reserve_mib`. The installed template reserves 512 MiB.

### Changing the model or runtime

Put model and runtime changes in a custom template and select it with `--template`. The default template's runtime and tokenizer checksums tie it to its pinned GGUF loader. A different model requires matching template checksums and serving limits.

## Output and exit codes

With the default `--format text`, lifecycle commands print their final state as a single JSON document on stdout. Progress messages for preparation and profiling, and any error details, go to stderr. To save the state and still watch progress, redirect stdout:

```bash
narwhal dev up > result.json
```

Exit codes in text mode:

| Code | When                                                                                                                                                                                                          |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | The status is `initialized`, `reused`, `starting`, `launched`, `ready`, or `stopped`.                                                                                                                         |
| 1    | `up`, `verify`, `status`, or `down` failed, or the status is `degraded`. After a failed `verify`, `verify` and `status` return 1 until the failure is cleared, even if the HTTP checks pass. |
| 2    | Invalid arguments, any `init` failure, a problem with the instance configuration, or a runtime package error.                                                                                                 |

With `--format json`, the state is wrapped in a versioned command result, and the exit code follows the result's status. For example, a degraded instance returns 3. [Command results](../Command-Results.md#statuses-and-exit-codes) lists every status and its exit code.

## Replaying role splits on the RTX 5090

The RTX 5090 reference includes `role_cycle`, whose fixed, repeatable workloads drive the controller through three prefill:decode splits: 1P:3D, 2P:2D, and 3P:1D. From a checkout of the repository, this command replays them through a verified fleet and saves the transition and latency checks:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev
```

[Replay the role cycle](../dev/08-RTX-5090-Reference.md#replay-the-role-cycle) describes the workload order, the output files, and the exit codes.

## Run directories

Each `run-*` directory contains:

- the fleet config used by the router
- the commands that were actually run
- engine logs, cache layouts, and attestations
- the measured profiles
- whole-GPU VRAM samples
- the request journal

Each `verify-*` directory also has the preflight log, the transfer evidence for each direction, the routed response, and the metrics.

The next `up` creates a new `run-*` directory with new profiles. Earlier `run-*` directories are kept.

## If `up` fails

The error names the stage that failed.

1. Check that stage's log in the `run-*` directory and fix the configuration or runtime.
2. Run `down` and wait for it to report `stopped`.
3. Run `up` again.
