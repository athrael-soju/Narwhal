---
description: Options, exit codes and run directories for narwhal dev on one NVIDIA CUDA GPU.
---

# `narwhal dev`

[Narwhal dev](../Dev-Runtime.md) runs the native NVIDIA CUDA backend on Ubuntu or Ubuntu under WSL2.

A template selects the model, runtime, and memory budget. The installed template runs one prefill and one decode engine on a selected NVIDIA GPU with 8 GB of VRAM or less. The optional [RTX 5090 reference](../dev/RTX-5090-Reference.md) is a measured four-engine configuration. The installed [SGLang template](../Dev-Runtime.md#running-sglang-engines) runs SGLang engines.

## Lifecycle

Run the subcommands from the directory that holds the instance.

```bash
narwhal dev init --model /path/to/model.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Subcommand | Behaviour |
| --- | --- |
| `init` | Writes a private instance with model and runtime pins, memory budget, unique ports, engine launch records, and fleet configuration. |
| `up` | Launches the fleet and reports `launched`. |
| `verify` | Verifies the fleet and reports `ready`. |
| `status` | Reports `starting`, `launched`, `ready`, `degraded`, or `stopped`. |
| `down` | Stops the recorded process groups with SIGKILL for survivors and reports `stopped`. |

`status` reports one of these states:

| `status` report | Condition |
| --- | --- |
| `starting` | `up` is in progress |
| `launched` | Supervised processes pass HTTP health checks |
| `ready` | Verification succeeded with current transfer evidence |
| `degraded` | `problems` lists a process, health, readiness, evidence, or verification failure |
| `stopped` | The recorded processes have stopped |

Running `init` again on an existing instance keeps existing files, operator edits, and saved values of omitted settings. When the explicitly supplied settings match the saved instance, `init` reports `status: reused`. When they conflict with a saved setting, `init` exits `2` and lists the differing settings.

To change initialization settings, run `narwhal dev init --instance runs/new-instance` with the desired template and flags.

For engine authentication:

1. Export `NARWHAL_ENGINE_API_KEY` before `up`.
2. Keep it set for profiling, verification, and routing.

A failed `verify`:

- saves its reason, evidence directory, and failure time in `lifecycle.json` and the attempt's `failure.json`
- appears in later `status` output as `verification_failure`, with its reason in `problems`
- remains on record until a successful `verify` or a completed `down`

## Output and exit codes

In text mode, stdout carries the returned state as one JSON document. stderr carries preparation progress, profiling progress, and error diagnostics.

Save the state with a stdout redirect, such as `narwhal dev up > result.json`.

`narwhal dev` uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes), with these lifecycle cases:

| Case | Exit code |
| --- | :--: |
| `initialized`, `reused`, `starting`, `launched`, `ready`, or `stopped` | `0` |
| `degraded`, a failed `verify`, or a later `status` while the instance retains that failure | `1` |
| An instance configuration error, a runtime package error, or a failed `init` check | `2` |

With `--format json`, the state is the versioned [command result](../Command-Results.md). JSON mode adds exit code `3` for `degraded` and `4` for an operational error.

## Options

`--instance` and `--format` apply to every subcommand. Every other option applies to `init`.

Print the installed distribution version with `narwhal --version`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--instance` | `runs/dev` | Private instance directory. | |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--template` | Installed small-GPU template | Template file with versioned model, tokenizer, runtime, profiling, and memory settings. | |
| `--model` | Pinned Hugging Face cache file or snapshot | GGUF file or checkpoint directory matching the template checksums. | |
| `--model-dir` | Pinned tokenizer cache directory | Directory of tokenizer and configuration files. | |
| `--gpu` | Single discovered GPU | Physical GPU UUID. | |
| `--engine-count` | Template value, `2` | Number of independent engine processes. | 2 to 8 |
| `--port-base` | Template ports (router 18000, engine 18101, attestation 18201, KV side channel 5701) | Base port for the router, engine HTTP, attestation, and KV side-channel ports. | |
| `--gpu-memory-utilization` | Template value, `0.35` | Per-engine fraction of total GPU memory. | Finite, above zero, at most 1 |
| `--device-allowance` | Template value, `0.8` | Fraction of total GPU memory bounding the sum of engine fractions and the observed startup memory increase. | Finite, at most 1 |
| `--interface` | `eth0` | Local KV transfer network interface. | |

`--port-base P` port layout:

| Port | Value |
| --- | --- |
| Router | `P` |
| Engine HTTP range start | `P+1` |
| Attestation range start | `P+101` |
| NIXL range start | `P+201` |

Selected ports must be distinct and fit 1..65535.

`init` GPU checks:

| Check | Requirement |
| --- | --- |
| Engine fractions | The decimal product of `--engine-count` and `--gpu-memory-utilization` is at most `--device-allowance`. |
| Free memory | Free VRAM covers `--device-allowance` times total device memory plus the template's `gpu.reserve_mib` (512 MiB in the installed template). |
| GPU product | The GPU name matches the template's `gpu.product` when the template sets it. |
| Total memory | Total VRAM is at least the template's `gpu.minimum_total_mib` when the template sets it. |

Change the model or runtime:

1. Write a custom template with the changes, including the matching checksums and serving limits for a model override.
2. Select the template with `--template`.

## Role-split replay

The RTX 5090 reference's `role_cycle` holds deterministic workloads for three role splits:

- 1P:3D, one prefill and three decode engines
- 2P:2D, two prefill and two decode engines
- 3P:1D, three prefill and one decode engine

[Replay the workloads](../dev/RTX-5090-Reference.md#replaying-all-three-role-splits) through a verified fleet from a checkout:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev
```

The replay saves transition and latency checks.

## Run directories and recovery

Each `up` writes a `run-*` directory with the router fleet configuration, effective commands, engine logs, cache layouts, attestations, measured profiles, whole-device VRAM samples, and the request journal. A `verify-*` directory holds the preflight log, directed transfer evidence, routed response, and metrics.

If `up` fails:

1. Inspect the log of the stage named in the error.
2. Repair the configuration or runtime.
3. Run `narwhal dev down`.
4. Confirm that `down` reports `stopped`.
5. Run `narwhal dev up` again.
