# `narwhal dev`

[Narwhal dev](../Dev-Runtime.md) runs the native NVIDIA CUDA backend on Ubuntu or Ubuntu under WSL2. Its installed template starts two engines on a selected NVIDIA GPU and targets GPUs with 8 GB of VRAM or less.

Templates select the model, runtime, and memory budget; `gpu.product` pins a GPU product when a template requires one. The optional [RTX 5090 reference](../dev/RTX-5090-Reference.md) records a measured four-engine configuration.

## Lifecycle

Run the subcommands from the directory that holds the instance.

Typical session:

```bash
narwhal dev init --model /path/to/model.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Subcommand | Behaviour |
| --- | --- |
| `init` | Writes a private instance with model and runtime pins, memory budget, unique ports, engine launch records, and fleet configuration. The installed template assigns one prefill and one decode role. |
| `up` | Checks the ports and runtime, starts each engine, and captures live attestations. Profiles every role split with at least one prefill and one decode engine, starts the router, and reports `launched`. |
| `verify` | Runs full preflight across every eligible directed KV path and checks the profiles against current processes. Sends an arithmetic request through the router, retains router and engine metrics, and reports `ready`. |
| `status` | Reports `launched` while supervised processes pass HTTP health checks. Reports `ready` after successful verification with current transfer evidence. |
| `down` | Checks the recorded boot ID and start ticks, and waits for the group's workers, including workers that outlive the group leader. Escalates survivors to SIGKILL and reports `stopped`. |

Running `init` again on an existing instance:

- keeps the existing files and operator edits;
- returns `status: reused` when the explicitly supplied settings match the saved instance; omitted settings keep their saved values;
- exits 2 and names the differing settings when a flag conflicts with a saved setting.

To change initialization settings, run `narwhal dev init --instance runs/new-instance` with the desired template and flags.

For engine authentication, export `NARWHAL_ENGINE_API_KEY` before `up`. Keep it set for profiling, verification, and routing.

A failed `verify`:

- saves its reason, evidence directory, and failure time in `lifecycle.json` and the attempt's `failure.json`;
- makes later `status` calls report `degraded`, with the record as `verification_failure` and its reason in `problems`;
- remains until a successful verification or a completed `down`. A verification retry retains it while its checks execute.

A subsequent `up` adds a run directory with fresh profiles beside the earlier runs' logs and measurements.

## Output and exit codes

Text mode writes the returned state to stdout as one JSON document. Preparation progress, profiling progress, and error diagnostics go to stderr.

Redirect stdout to save the state, as in `narwhal dev up > result.json`.

`narwhal dev` uses the [text-mode exit codes](../CLI-Reference.md#text-mode-exit-codes), with these lifecycle cases:

| Case | Exit code |
| --- | ---: |
| `initialized`, `reused`, `starting`, `launched`, `ready`, or `stopped` | `0` |
| `degraded`, a failed `verify`, or a later `status` while the instance retains that failure | `1` |
| An instance configuration error, a runtime package error, or a failed `init` check | `2` |

`--format json` wraps the state in the versioned [command result](../Command-Results.md). In that mode, a `degraded` status returns 3 and an operational error returns 4.

## Options

`--instance` and `--format` apply to every subcommand; the other options apply to `init`. Print the installed distribution version with `narwhal --version`.

| Option | Default | Description |
| --- | --- | --- |
| `--instance` | `runs/dev` | Private instance directory. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--template` | Installed small-GPU template | Template file with versioned model, tokenizer, runtime, profiling, and memory settings. |
| `--model` | Pinned Hugging Face cache file | GGUF file matching the template checksum. |
| `--model-dir` | Pinned tokenizer cache directory | Directory of tokenizer and configuration files. |
| `--gpu` | Single discovered GPU | Physical GPU UUID. |
| `--engine-count` | Template value, `2` | Number of independent engine processes. |
| `--port-base` | Template ports (router 18000, engine 18101, attestation 18201, NIXL 5701) | Base port for the router, engine HTTP, attestation, and NIXL ports. |
| `--gpu-memory-utilization` | Template value, `0.35` | Per-engine fraction of total GPU memory; finite, greater than zero, and at most 1. |
| `--device-allowance` | Template value, `0.8` | Fraction of total GPU memory that bounds the sum of engine fractions and the aggregate observed startup memory increase; finite and at most 1. |
| `--interface` | `eth0` | Local NIXL/UCX network interface. |

`--port-base P` port layout:

| Port | Value |
| --- | --- |
| Router | `P` |
| Engine HTTP range start | `P+1` |
| Attestation range start | `P+101` |
| NIXL range start | `P+201` |

All selected ports must be distinct and fit 1..65535.

Memory rules:

- `init` accepts two to eight engines.
- The decimal sum of per-engine fractions must be at most the device allowance. Three engines at `0.1` fit an allowance of `0.3`.
- The free-memory check reserves the allowance times total device memory, plus the template's `gpu.reserve_mib` (512 MiB in the installed template).

Put model and runtime changes in a custom template, and select it with `--template`. Model overrides require their matching template checksums and serving limits.

## Role-split replay

The RTX 5090 reference also contains `role_cycle`, which holds deterministic workloads for three role splits:

- one prefill and three decode engines (1P:3D);
- two prefill and two decode engines (2P:2D);
- three prefill and one decode engine (3P:1D).

From a checkout, `python -m tools.measurement.dev_cycle --instance runs/dev` replays these workloads through a verified fleet and saves transition and latency checks.

Workload order, output files, and exit codes: [Replay all three role splits](../dev/RTX-5090-Reference.md#replay-all-three-role-splits).

## Run directories and recovery

| Directory | Contents |
| --- | --- |
| `run-*` | Router fleet configuration, effective commands, engine logs, cache layouts, attestations, measured profiles, whole-device VRAM samples, and the request journal. |
| `verify-*` | Preflight log, directed transfer evidence, routed response, and metrics. |

If `up` fails:

1. Inspect the log of the stage named in the error.
2. Repair the configuration or runtime.
3. Run `narwhal dev down` and confirm that it reports `stopped`.
4. Run `narwhal dev up` again.
