---
description: Options, exit codes and run directories for narwhal dev on one NVIDIA CUDA GPU.
---

# `narwhal dev`

`narwhal dev` runs a development fleet on one NVIDIA CUDA GPU, on Ubuntu natively or under WSL2. The fleet holds native vLLM or SGLang engines, their attestation sidecars and a router. To set up the host, follow [Narwhal dev](../Dev-Runtime.md). A template pins the model, the runtime, the memory budget and the ports. The package installs three templates:

| Template | Engine | Engines | Model input |
| --- | --- | --- | --- |
| `small-cuda-v1.json`, the default | vLLM | 1 prefill, 1 decode | GGUF file |
| [`small-cuda-sglang-v1.json`](../Dev-Runtime.md#running-sglang-engines) | SGLang with Mooncake | 1 prefill, 1 decode | Checkpoint directory |
| [`reference-v1.json`](../dev/RTX-5090-Reference.md), for an RTX 5090 | vLLM | 2 prefill, 2 decode | GGUF file |

## Lifecycle

Run the subcommands with the Python environment that ran `init`:

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Subcommand | Operation | Reports |
| --- | --- | --- |
| `init` | Checks the model files, runtime packages, GPU, network interface and ports, then writes `template.json`, `instance.json`, `engine-launch.json` and `fleet.json` to the instance directory. | `initialized`, or `reused` |
| `up` | Prepares, checks and starts the engines with [`narwhal-engine`](Engine.md), captures their attestations, starts the attestation sidecars, profiles the engines and starts the router. | `launched` |
| `verify` | Records [KV evidence](Check.md#recording-and-verifying-kv-evidence) for every directed pair, sends a routed `2 + 3` request that must return `5`, saves router and engine metrics, and checks the router's `/ready`. | `ready` |
| `status` | Reads the recorded processes, health routes and saved evidence. | A state from the next table |
| `down` | Sends SIGTERM to each recorded process group and SIGKILL to members still running 10 seconds later. | `stopped` |

`status` reports one of these states:

| State | Condition |
| --- | --- |
| `starting` | `up` is running. |
| `launched` | Every engine, sidecar and router process is running, every engine and the router answer `/health`, and the router answers `/ready`. |
| `ready` | The `launched` conditions hold, and the KV evidence of the last `verify` still verifies against the running engines. |
| `degraded` | `problems` lists a failed condition, a surviving process, or a failed `verify`. |
| `stopped` | The last generation stopped and none of its processes survive, or `up` has never run. |

`up` requires an instance that has never started or whose last generation stopped, through `down` or the rollback of a failed `up`. When the template's `profile` sets neighbour traffic, as the vLLM templates do, `up` profiles every role split from 1 prefill engine to one less than the engine count with [`--colocated`](Profile.md#shared-gpu-neighbour-traffic), and merges the splits when there are several. Otherwise, as with the SGLang template, `up` profiles only the opening split, where the [SGLang pair sweep](Profile.md#sglang-pairs) measures both roles of each engine.

Running `init` on an existing instance changes nothing. When the settings you supply match the saved instance, `init` reports `reused`. When a setting conflicts, `init` exits with code `2` and lists the conflicting options. To change a setting, run `init` with a new `--instance` directory.

To authenticate engine requests, export `NARWHAL_ENGINE_API_KEY` before `up` and keep it set for later commands. `up` passes it to the engines, and sets `engine.engine_api_key_env` in the fleet configuration so that the profiler, preflight and router send it.

A failed `verify` writes its reason, evidence directory and failure time to `verification_failure` in `lifecycle.json` and to `failure.json` in its evidence directory. `status` reports `degraded` with that `verification_failure` until a later `verify` succeeds or `down` stops every process.

## Output and exit codes

In text mode, stdout carries the result as one JSON document, and stderr carries preparation and profiling progress and error diagnostics. To save the result, redirect stdout, as in `narwhal dev up > result.json`.

| Outcome | Exit code |
| --- | :--: |
| `initialized`, `reused`, `starting`, `launched`, `ready` or `stopped` | `0` |
| `degraded`, or a failed `up`, `verify` or `down` | `1` |
| A failed `init` check, an unreadable instance or lifecycle record, a missing runtime package, or a Python environment other than the one that ran `init` | `2` |

With `--format json`, the command prints a [versioned command result](../Command-Results.md), and the exit code follows its status. A `degraded` instance exits with code `3`, and a failed step or routed check during `up` or `verify` exits with code `4`.

## Options

`--instance` and `--format` apply to every subcommand. The other options apply to `init`, and an omitted option takes its value from the saved instance or the template. `narwhal --version` prints the installed distribution version.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--instance` | `runs/dev` | Private instance directory. | |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--template` | `small-cuda-v1.json` | Template file with versioned model, tokenizer, runtime, profiling and memory settings. | |
| `--model` | The template's pinned file or snapshot in the Hugging Face cache | Model to serve, a GGUF file for a vLLM template or a checkpoint directory for an SGLang template. | Matches the template's checksums, and for a GGUF file its file name |
| `--model-dir` | The template's pinned tokenizer snapshot in the Hugging Face cache | Directory with the tokenizer files and `config.json`. | Matches the template's checksums |
| `--gpu` | The only GPU that `nvidia-smi` lists | Physical GPU UUID. | |
| `--engine-count` | Template value, `2` in the small templates | Number of engines. | 2 to 8 |
| `--port-base` | Template ports | Base of the port layout below. | Every port within 1 to 65535 and distinct |
| `--gpu-memory-utilization` | Template value, `0.35` in the small templates | Each engine's fraction of total GPU memory on vLLM, or of the GPU memory free at engine start on SGLang. | Above 0 and at most 1 |
| `--device-allowance` | Template value, `0.8` in the small templates | Fraction of total GPU memory that bounds the sum of engine fractions and the memory growth during startup. | At least `--engine-count` times `--gpu-memory-utilization`, and at most 1 |
| `--interface` | `eth0` | Network interface for KV transfer. | An interface with exactly one IPv4 address |

`--port-base P` puts the router at `P` and starts the engine HTTP, attestation and KV side-channel ranges at `P+1`, `P+101` and `P+201`, one port per engine. The KV side channel is the NIXL side channel on vLLM and the bootstrap server on SGLang. The installed templates put the router at 18000 and start the ranges at 18101, 18201 and 5701. `init` and `up` require every port to be free on `127.0.0.1`.

`init` also checks the GPU:

| Check | Requirement |
| --- | --- |
| Free memory | Free VRAM covers `--device-allowance` times total VRAM, plus the template's `gpu.reserve_mib`, 512 MiB in the small templates. |
| GPU product | The GPU name matches the template's `gpu.product` when the template sets it. |
| Total memory | Total VRAM is at least the template's `gpu.minimum_total_mib` when the template sets it. |

To serve another model or runtime, write a [custom template](../Dev-Runtime.md#template-and-gpu-allocation) and select it with `--template`.

## Role-split replay

The RTX 5090 reference template's `role_cycle` holds three seeded workloads that drive the four-engine fleet's controller from 2P:2D to 1P:3D, back to 2P:2D, to 3P:1D and back to 2P:2D. To [replay them](../dev/RTX-5090-Reference.md#replaying-all-three-role-splits), run this from a checkout against an instance that reports `ready`:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev
```

The replay writes its records to a new `cycle-*` directory in the instance, or to `--out`. Its `summary.json` sets `passed` when all of these hold:

- The controller's reactive moves produce exactly the expected splits on the same engine processes.
- Each workload runs on schedule with every request completed or rejected for its TTFT.
- Each workload meets the SLO where the recipe requires it.

The command exits with code `0` when the replay passes and `2` when it fails.

## Run directories and recovery

Each `up` writes a `run-*` directory in the instance. It holds:

- the router's fleet configuration with the engine contract
- the command line and log of each stage
- one launch directory per engine
- the profiles
- whole-device VRAM samples in `up-memory.jsonl` and `verify-memory.jsonl`
- the request journal

On vLLM, each launch directory also holds the engine's `cache-layout.json`.

Each `verify` writes a `verify-*` directory in the run directory with the preflight log, the KV evidence in `kv.json`, the routed response in `completion.json`, and the router and engine metrics.

When `up` fails, it stops the processes it started and records the failure in `lifecycle.json`. To recover:

1. Read the log of the stage that the error names.
2. Repair the cause.
3. Run `narwhal dev down` and confirm that it reports `stopped`.
4. Run `narwhal dev up`.

To find a stage's time budget or handle processes that survive `down`, follow [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery).
