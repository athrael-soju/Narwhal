---
description: Narwhal dev runs several vLLM engines with prefill and decode role swaps on one NVIDIA CUDA GPU under Ubuntu or WSL2.
---

# Narwhal dev

Narwhal dev runs several independent inference engines on one NVIDIA CUDA GPU, on Ubuntu natively or under WSL2.

An instance directory, `runs/dev` by default, holds the model choice, runtime, GPU allocation, ports, profiles, and process identities.

Model files come from the Hugging Face cache or from paths given to `narwhal dev init`.

For NVIDIA GPUs with 8 GB of VRAM or less, the shipped template starts one prefill and one decode engine running the Qwen3.5-0.8B GGUF model.

The [four-engine reference template](dev/RTX-5090-Reference.md) is measured on and pinned to the RTX 5090.

## Prepare Ubuntu or WSL2

1. Install the NVIDIA driver for your host:

    | Host | Driver |
    | --- | --- |
    | Native Ubuntu | A driver compatible with the CUDA runtime you plan to use |
    | WSL2 | The Windows driver from [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/) |

2. Keep the checkout, the model, the virtual environment, and the instance directory on the Linux filesystem.
3. Run every command from an Ubuntu shell, inside WSL2 on a Windows host.
4. Show the GPU and the IPv4 interfaces:

    ```bash
    nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
    "$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
    ip -brief -4 address
    ```

5. Record the name of an interface with exactly one IPv4 address for NIXL/UCX (default `eth0`).

## Install the runtime and model

Install the pinned vLLM, Torch, NIXL, Transformers, GGUF loader, model, and tokenizer with the [CUDA runtime and model steps](dev/CUDA-Runtime.md).

## Template and GPU allocation

Two `init` flags set the GPU memory allocation:

| Flag | Template field | Default | Meaning |
| --- | --- | --- | --- |
| `--gpu-memory-utilization` | `allocation.gpu_memory_utilization` | Template value, `0.35` installed | Share of total VRAM for each vLLM process, covering model weights, runtime overhead, and KV cache |
| `--device-allowance` | `allocation.device_allowance` | Template value, `0.8` installed | Cap on the sum of the engine fractions and on whole-device memory growth during startup |

`init` requires free VRAM of at least the device allowance times total VRAM plus the template's `gpu.reserve_mib` (512 MiB installed).

The shipped template sets development SLO targets:

| Target | Template field | Value |
| --- | --- | --- |
| Time to first token (TTFT) | `slo.ttft_s` | 5 seconds |
| Time per output token (TPOT) | `slo.tpot_s` | 500 ms |

Replace these placeholder targets with values measured on your card.

A custom `--template` sets the model, runtime, context length, profiling sweep, and reserve.

Template fields that pin the hardware:

| Field | Effect |
| --- | --- |
| `gpu.product` | GPU product name |
| `gpu.minimum_total_mib` | Minimum total VRAM |

Create a custom template:

1. Export the installed template:

    ```bash
    mkdir -p runs
    python - <<'PYTHON' > runs/small-cuda-template.json
    from importlib.resources import files
    print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
    PYTHON
    ```

2. Edit the exported file.
3. Initialize a fresh instance with
   `narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom`.

## Initialize and verify an instance

Run the lifecycle commands with the interface name from `ip`:

```bash
interface=eth0  # replace with the interface reported by ip
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

`init` runs these steps:

1. Checks the model and tokenizer checksums.
2. Selects a CUDA GPU by UUID and checks its free VRAM.
3. Checks the runtime package versions and GGUF plugin hashes.
4. Checks the network interface and ports.
5. Writes the private instance.

`up` runs these steps:

1. Checks the model, tokenizer, and GGUF plugin hashes.
2. Checks that the ports are free.
3. Starts the engines.
4. Captures attestations and starts the attestation sidecars.
5. Profiles every role split.
6. Starts the router.

`verify` runs these steps:

1. Runs preflight over every eligible directed KV path.
2. Sends a routed arithmetic request.
3. Saves the router and engine metrics.
4. Reports `ready` when every check passes.

Run every later lifecycle command in the Python environment that ran `init`.

Each command writes to two streams:

| Stream | Content |
| --- | --- |
| stdout | The [lifecycle result](cli/Dev.md) as one JSON document |
| stderr | Preparation, profiling, and error diagnostics |

For scripts, `--format json` returns [versioned command results and automation exit codes](Command-Results.md).

Read the router's state and metrics at its default address:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

Optional: [forward these metrics to a Prometheus and Grafana host](observability/04-WSL2.md).

Two `narwhal dev` options change the layout and target:

| Option | Effect |
| --- | --- |
| `--port-base` on `narwhal dev init` | Selects a different port layout |
| `--instance` on any command | Targets another instance |

## Inspect and stop the instance

The run directory that `status` prints holds:

| Content | Path |
| --- | --- |
| Routed response | `verify-*/completion.json` |
| Whole-device memory samples | `up-memory.jsonl`, `verify-memory.jsonl` |
| Request journal | `journal.jsonl` |
| Teardown record | `teardown.json` |
| Model loading and engine requests | `engine-*/startup.log` |
| Probe and fit outcomes | `profile-*.log` |
| Runtime, profile, and transfer checks | `verify-*/preflight.log` |

Stop the instance:

```bash
narwhal dev down
narwhal dev status
```

`down` reports `stopped` for the recorded process groups.

Each `up` writes a new run directory.

For a runtime change or engine restart:

1. Run `down`.
2. Run `up`.
3. Run `verify`.

## Stage deadlines and recovery

`dev up` and `dev verify` run subprocess stages with time budgets.

| Variable | Default | Scope |
| --- | --- | --- |
| `NARWHAL_STAGE_TIMEOUT_SECONDS` | 300 seconds | Every stage |
| `NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS` | | One stage, with `<NAME>` as the stage name in uppercase and hyphens replaced by underscores |

Stage names:

| Stage | Work |
| --- | --- |
| `engine-<n>` | Runtime check for engine `n` |
| `native-start-shared` | Native shared startup |
| `attest-<n>` | Attestation capture for engine `n` |
| `profile-<p>p<d>d` | Profiling a role split with `p` prefill and `d` decode engines |
| `profile-merge` | Merging the role-split profiles, with three or more engines |
| `preflight` | Directed KV preflight during `verify` |

Override the startup and preflight budgets:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=720 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

Budgets and fixed limits:

| Limit | Value |
| --- | --- |
| Stage budget | Covers the stage's work, helper imports, and subprocess startup |
| Profiled role split | A fresh stage budget for each split |
| Engine health check in native shared startup | 180 seconds per engine |
| HTTP health request | 2-second timeout per request |
| Routed verification request | 30-second timeout |
| Cleanup grace periods | Added to the execution budget |

`narwhal dev` stops the stage's supervised processes when:

- a stage budget expires
- `narwhal dev` receives SIGINT (Ctrl-C) during a stage
- `narwhal dev` receives SIGTERM during a stage

Cleanup steps:

| Step | Signal | Wait | Variable |
| :---: | --- | --- | --- |
| 1 | SIGTERM | Up to 10 seconds | `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS` |
| 2 | SIGKILL to surviving processes | Up to 5 seconds | `NARWHAL_STAGE_KILL_GRACE_SECONDS` |

Files next to each stage log:

| File | Content |
| --- | --- |
| `*.command.json` | The stage's command line |
| `*.stdout`, `*.stderr` | The stage's private output |
| `*.stage.json` | The budget, wall-clock start, elapsed time, exit status, process identities, cleanup escalation, and surviving PIDs |

Failed command outcomes:

| Failed command | Result | Report |
| --- | --- | --- |
| `up` | Startup rolls back. | `lifecycle.json` records the original failure and teardown errors. |
| `verify` | The attempt directory stays. | `status` reports `degraded` with the reason until a later `verify` succeeds or `down` completes. |

Recover from a failed stage, with `PATH` as the instance directory:

1. Read the failed stage's output.
2. Run `narwhal dev status --instance PATH`.
3. Stop the process generation with `narwhal dev down --instance PATH`.
4. Run `narwhal dev up --instance PATH`.

If `status` or `down` lists surviving PIDs, [inspect each one by hand](dev/Recovery-and-Qualification.md) against its recorded boot ID, start tick, and process group.

## Contribute another GPU recipe

Contribute a template for a new small CUDA GPU:

1. Follow the [checks and pull request workflow](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md).
2. Submit the working template with qualification evidence.

The qualification evidence records:

- the GPU, driver, model, and runtime versions
- the free-memory reserve and peak startup memory
- both directed KV transfers
- the routed completion

A contribution for a GPU from another vendor includes:

- a measured template
- optionally, discovery, memory accounting, engine launch, and a compatible transfer runtime
