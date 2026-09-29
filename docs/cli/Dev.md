# `narwhal dev`

`narwhal dev` sets up a local development fleet on a single NVIDIA GPU. It uses the native CUDA backend and runs on Ubuntu, or Ubuntu under WSL2. For background, see [Narwhal dev](../Dev-Runtime.md).

The installed template starts two engines, one prefill and one decode, and is sized for GPUs with 8 GB of VRAM or less. For a larger setup, the optional [RTX 5090 reference](../dev/08-RTX-5090-Reference.md) records a measured four-engine configuration.

Templates choose the model, runtime, and memory budget. If a recipe needs a particular card, the template pins it with `gpu.product`.

## Quick start

```bash
narwhal dev init --model /path/to/model.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

If your engines need an API key, export `NARWHAL_ENGINE_API_KEY` before `up`. The generated fleet config reads the key from that variable when profiling, verifying, and routing, so keep it set whenever you run those commands.

## Commands

### `init`

`init` creates a private instance directory. It holds the model and runtime pins, the memory budget, a unique set of ports, the engine launch records, and the fleet config.

Running `init` again on an existing instance keeps its files and any edits you've made. Settings you don't pass keep their saved values. If the settings you do pass match what's saved, `init` reports `reused`. If any of them differ, it exits with code 2 and lists them.

To change settings, create a new instance with the template and flags you want:

```bash
narwhal dev init --instance runs/new-instance
```

### `up`

`up` checks the ports and the runtime, then:

1. Starts each engine.
2. Captures a live attestation from each one.
3. Profiles every role split that has at least one prefill engine and one decode engine.
4. Starts the router.

When it's done, it reports `launched`.

### `verify`

`verify` runs the full preflight on every eligible KV transfer path, in each direction. It then checks the profiles against the processes that are running now, sends an arithmetic request through the router, and saves the router and engine metrics. If everything passes, it reports `ready`.

### `status`

`status` reports `launched` while the instance's processes pass their HTTP health checks. Once `verify` has succeeded and its transfer evidence is still current, it reports `ready`.

If `verify` fails, it saves the reason, the evidence directory, and the time of the failure in `lifecycle.json` and in that attempt's `failure.json`. From then on, `status` reports `degraded`, includes the saved record as `verification_failure`, and adds the reason to `problems`. This lasts until `verify` succeeds or `down` finishes. While a new `verify` is running, the earlier failure stays in place.

### `down`

`down` checks the recorded boot ID and process start times, so it only stops processes the instance started. It waits for each process group's workers and leader to exit, and sends SIGKILL to anything still running. It reports `stopped`.

The next `up` creates a new run directory with new profiles. Logs and measurements from earlier runs are kept next to it.

## Options

| Option                     | Default                                                                                  | Description                                                                                                                                                                                 |
| -------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--instance`               | `runs/dev`                                                                               | Instance directory. Works with every subcommand.                                                                                                                                            |
| `--template`               | installed small-GPU template                                                             | Template for `init`, with versioned model, tokenizer, runtime, profiling, and memory settings.                                                                                              |
| `--model`                  | pinned file in the Hugging Face cache                                                    | GGUF model file. Its checksum must match the template.                                                                                                                                      |
| `--model-dir`              | pinned tokenizer cache directory                                                         | Directory with the tokenizer and config files.                                                                                                                                              |
| `--gpu`                    | the GPU found, if there's only one                                                       | Physical GPU UUID.                                                                                                                                                                          |
| `--engine-count`           | template value (2)                                                                       | Number of engine processes, from 2 to 8.                                                                                                                                                    |
| `--port-base`              | template ports: router 18000, engines from 18101, attestation from 18201, NIXL from 5701 | With `--port-base P`, the router uses `P`, engine HTTP ports start at `P+1`, attestation ports at `P+101`, and NIXL ports at `P+201`. Every port must be different and between 1 and 65535. |
| `--gpu-memory-utilization` | template value (0.35)                                                                    | Share of the GPU's total memory for each engine. Greater than 0 and no more than 1.                                                                                                         |
| `--device-allowance`       | template value (0.8)                                                                     | Share of the GPU's total memory that all engines together can use, up to 1. It caps both the sum of the per-engine shares and the total memory growth measured at startup.                  |
| `--interface`              | `eth0`                                                                                   | Network interface for NIXL/UCX.                                                                                                                                                             |
| `--format`                 | `text`                                                                                   | Use `json` for [versioned command results](../Command-Results.md).                                                                                                                          |

Memory fractions must be finite numbers. To print the installed version, run `narwhal --version`.

### Memory budget

`init` adds up the per-engine fractions and checks the total against the device allowance. The sum is exact, so three engines at `0.1` fit an allowance of `0.3`. If the total is higher, `init` fails.

It also checks that the GPU has enough free memory for the allowance times the GPU's total memory, plus the template's `gpu.reserve_mib`. The installed template reserves 512 MiB.

### Changing the model or runtime

Put model and runtime changes in a custom template and select it with `--template`. The default template's runtime and tokenizer checksums tie it to its pinned GGUF loader. To use a different model, you need the template checksums and serving limits that match it.

## Output and exit codes

In text mode, lifecycle commands print their final state as a single JSON document on stdout. Progress messages for preparation and profiling, and any error details, go to stderr. To save the state and still watch progress, redirect stdout:

```bash
narwhal dev up > result.json
```

Exit codes in text mode:

| Code | When                                                                                                                                                                                            |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | The status is `initialized`, `reused`, `starting`, `launched`, `ready`, or `stopped`.                                                                                                           |
| 1    | A lifecycle operation failed, or the status is `degraded`. After a failed `verify`, both `verify` and later `status` calls return 1 until the failure is cleared, even if the HTTP checks pass. |
| 2    | Invalid arguments, a problem with the instance configuration, or a runtime package error.                                                                                                       |

With `--format json`, the state is wrapped in a versioned command result. A degraded status returns 3 and an operational error returns 4.

## Replaying role splits on the RTX 5090

The RTX 5090 reference also includes `role_cycle`, which has fixed, repeatable workloads for three splits of prefill to decode engines: 1P:3D, 2P:2D, and 3P:1D. From a checkout of the repository, this command replays them through a verified fleet and saves the transition and latency checks:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev
```

[Replay all three role splits](../dev/08-RTX-5090-Reference.md#replay-all-three-role-splits) describes the workload order, the output files, and the exit codes.

## Run directories

Each `run-*` directory contains:

- the fleet config the router used
- the commands that were actually run
- engine logs, cache layouts, and attestations
- the measured profiles
- VRAM samples for the whole GPU
- the request journal

Each `verify-*` directory also has the preflight log, the transfer evidence for each direction, the routed response, and the metrics.

## If `up` fails

The error names the stage that failed. Check that stage's log and fix the configuration or runtime. Then run `down`, and once it confirms everything has stopped, run `up` again.