---
description: Narwhal dev runs several vLLM or SGLang engines with prefill and decode role swaps on one NVIDIA CUDA GPU under Ubuntu or WSL2.
---

# Narwhal dev

Narwhal dev runs two to eight independent vLLM or SGLang engines and a router on one NVIDIA CUDA GPU, on Ubuntu natively or under WSL2. An instance directory, `runs/dev` by default, holds the model choice, runtime, GPU allocation, ports, profiles, and process identities. Model files come from the Hugging Face cache or from paths given to `narwhal dev init`.

A template selects the backend, model, runtime pins, engine count, and memory budget. Narwhal installs four:

| Template | Backend and KV transfer | Model | Engines |
| --- | --- | --- | --- |
| `small-cuda-v1.json`, the default | vLLM 0.30.0 with NIXL over UCX | Qwen3.5-0.8B Q4_K_M GGUF | 2 |
| `small-cuda-sglang-v1.json` | SGLang 0.5.21 with Mooncake over TCP | Qwen3.5-0.8B checkpoint in bfloat16 | 2 |
| `four-engine-cuda-sglang-v1.json` | SGLang 0.5.21 with Mooncake over TCP | Qwen3.5-0.8B checkpoint in bfloat16 | 4 |
| `reference-v1.json` | vLLM 0.30.0 with NIXL over UCX | Qwen3.5-0.8B Q4_K_M GGUF | 4 |

The two small templates start one prefill and one decode engine on an NVIDIA GPU whose free VRAM is at least 80% of its total plus 512 MiB. The [four-engine SGLang template](#four-sglang-engines-and-the-role-cycle) starts two prefill and two decode engines on a GPU whose free VRAM is at least 80% of its total plus 2,048 MiB. The [four-engine reference template](dev/RTX-5090-Reference.md) pins the RTX 5090.

## Preparing Ubuntu or WSL2

1. Install the NVIDIA driver for your host:

    - On native Ubuntu, install a driver compatible with the CUDA runtime you plan to use.
    - On WSL2, install the Windows driver from [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/).

2. Keep the checkout, the model, the virtual environment, and the instance directory on the Linux filesystem.
3. Run every command from an Ubuntu shell, inside WSL2 on a Windows host.
4. Show the GPU and the IPv4 interfaces:

    ```bash
    nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
    "$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
    ip -brief -4 address
    ```

5. Record the name of the interface for KV transfer, `eth0` by default. Pick one with exactly one IPv4 address that is not a bridge such as `docker0`. On a bridge, vLLM engines fail to start with `NIXL_ERR_BACKEND`.

## Installing the runtime and model

For the default vLLM template, install the pinned vLLM, Torch, NIXL, Transformers, GGUF loader, model, and tokenizer with the [CUDA runtime and model steps](dev/CUDA-Runtime.md). For the SGLang templates, follow [Running SGLang engines](#running-sglang-engines).

## Template and GPU allocation

Two `init` flags set the GPU memory allocation, and each defaults to its template value. `--gpu-memory-utilization` sets `allocation.gpu_memory_utilization`, each engine's memory fraction, which the small templates set to `0.35`. A vLLM engine receives it as `--gpu-memory-utilization`, the share of total VRAM for the process, covering model weights, runtime overhead, and KV cache. An SGLang engine receives it as `--mem-fraction-static`, which SGLang applies to the VRAM that is free when the engine starts, so the second engine receives a smaller budget than the first.

Under WSL2, each SGLang engine reads the free VRAM as if the other engines held none, so every engine receives the budget the first engine receives, and a fraction that fits on native Ubuntu can overcommit the GPU. For SGLang under WSL2, initialize a fresh instance with a lower `--gpu-memory-utilization`, in a new `--instance` directory for each attempt, until every engine starts.

`--device-allowance` sets `allocation.device_allowance`, the cap on the sum of the engine fractions and on whole-device memory growth during startup. The small templates set it to `0.8`.

`init` requires free VRAM of at least the device allowance times total VRAM plus the template's `gpu.reserve_mib` (512 MiB in the small templates). During `up`, each engine starts only when free VRAM covers its fraction of total VRAM, and startup fails when whole-device memory use grows by more than the device allowance times total VRAM.

The small templates set development SLO targets of 5 seconds for time to first token (TTFT) in `slo.ttft_s` and 500 ms for time per output token (TPOT) in `slo.tpot_s`. Replace these placeholder targets with values measured on your card.

A custom `--template` sets the model, runtime, context length, profiling sweep, and reserve. Its `gpu.product` field pins the GPU product name, and `gpu.minimum_total_mib` pins the minimum total VRAM. An SGLang template with the `mooncake` connector also sets `runtime.decode_cuda_graph_memory_gb`, the memory a prefill-launched engine reserves when it switches to decode.

`runtime.max_num_seqs` sets the most requests each engine runs at once. A vLLM engine receives it as `--max-num-seqs`. An SGLang engine receives it as `--max-running-requests`, from 1 to 256, or 256 when the template omits it. SGLang can serve fewer requests than that, for example when the mamba state cache that it sizes from the engine's memory fraction holds fewer. `up` then stops at attestation and reports how many running requests the engine serves. Lower `runtime.max_num_seqs` to that number in your template, and initialize a fresh instance from it.

Create a custom template:

1. Export an installed template:

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

## Initializing and verifying an instance

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
3. Checks the runtime package versions, imports the engine runtime, and checks the GGUF plugin hashes that a vLLM template pins.
4. Checks the network interface and ports.
5. Writes the private instance.

`up` runs these steps:

1. Checks the model and tokenizer hashes, and the GGUF plugin hashes that a vLLM template pins.
2. Checks that the ports are free.
3. Checks and starts the engines.
4. Captures attestations and starts the attestation sidecars.
5. Profiles the engines. A template with neighbour load settings in `profile`, such as the vLLM templates and the four-engine SGLang template, profiles every role split under that load. A template without them, such as the small SGLang template, profiles only the opening split.
6. Starts the router.

`verify` runs these steps:

1. Runs preflight over every eligible directed KV path.
2. Sends a routed arithmetic request.
3. Saves the router and engine metrics.
4. Checks router readiness, and reports `ready` when every check passes.

Run every later lifecycle command in the Python environment that ran `init`.

Each command writes the [lifecycle result](cli/Dev.md) to stdout as one JSON document, and preparation, profiling, and error diagnostics to stderr.

For scripts, `--format json` returns [versioned command results and automation exit codes](Command-Results.md).

Read the router's state and metrics at its default address:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

The [WSL2 monitoring setup](observability/04-WSL2.md) forwards these metrics to Prometheus and Grafana on a separate host.

`--port-base` on `narwhal dev init` selects a different port layout, and `--instance` on any command targets another instance.

## Running SGLang engines

The installed `small-cuda-sglang-v1.json` template runs one prefill and one decode SGLang engine with the Qwen3.5-0.8B checkpoint, each running at most 4 requests at once. The engines transfer KV through Mooncake over TCP. The checkpoint mixes linear-attention and full-attention layers, so the template launches the engines with `--mamba-radix-cache-strategy no_buffer` and `--disable-overlap-schedule`, as [SGLang runtime fields](configuration/05-Engine-Launch.md#sglang-runtime-fields) describes.

1. Install the libraries that Mooncake's native module links, `libibverbs.so.1`, `libmlx5.so.1` and `libnuma.so.1`:

    ```bash
    sudo apt-get update
    sudo apt-get install -y libibverbs1 ibverbs-providers libnuma1
    ```

2. Install a CUDA 13 toolkit from [NVIDIA's CUDA installation guide for Linux](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/), or from the [CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/) under WSL2. On a GPU where SGLang compiles its kernels when an engine starts, SGLang needs the toolkit's `bin/nvcc` and `lib64/libcudart.so`, which the CUDA packages that pip installs do not provide. Export the toolkit root in each shell that runs the lifecycle commands, and check that `nvcc` reports release 13:

    ```bash
    export CUDA_HOME=/usr/local/cuda  # replace with the CUDA 13 toolkit root
    "$CUDA_HOME/bin/nvcc" --version
    ls "$CUDA_HOME/lib64/libcudart.so"
    ```

3. Install Narwhal and the pinned packages in a fresh virtual environment:

    ```bash
    python3.12 -m venv .venv-sglang
    source .venv-sglang/bin/activate
    python -m pip install .
    python -m pip install 'sglang==0.5.21' 'mooncake-transfer-engine-cuda13==0.3.13' \
      'torch==2.13.0' 'transformers==5.12.1'
    ```

4. Download the checkpoint into the Hugging Face cache:

    ```bash
    hf download Qwen/Qwen3.5-0.8B --revision 2fc06364715b967f1860aea9cf38778875588b17
    ```

5. Export the template and start an instance from it:

    ```bash
    mkdir -p runs
    python - <<'PYTHON' > runs/small-cuda-sglang-template.json
    from importlib.resources import files
    print(files('narwhal.dev').joinpath('small-cuda-sglang-v1.json').read_text())
    PYTHON
    narwhal dev init --template runs/small-cuda-sglang-template.json \
      --instance runs/dev-sglang --interface "$interface"
    narwhal dev up --instance runs/dev-sglang
    narwhal dev verify --instance runs/dev-sglang
    ```

    Under WSL2, when an engine fails to start, lower each engine's memory fraction as [Template and GPU allocation](#template-and-gpu-allocation) describes.

## Four SGLang engines and the role cycle

The installed `four-engine-cuda-sglang-v1.json` template runs two prefill and two decode SGLang engines with the checkpoint and packages of `small-cuda-sglang-v1.json`, each engine with a memory fraction of 0.15 and a context limit of 4,096 tokens. Its profile sets neighbour load, so `narwhal dev up` profiles every engine at 1P:3D, 2P:2D and 3P:1D under [shared-GPU neighbour traffic](cli/Profile.md#shared-gpu-neighbour-traffic).

1. Complete steps 1 to 4 of [Running SGLang engines](#running-sglang-engines).
2. Export the template and start an instance from it:

    ```bash
    mkdir -p runs
    python - <<'PYTHON' > runs/four-engine-sglang-template.json
    from importlib.resources import files
    print(files('narwhal.dev').joinpath('four-engine-cuda-sglang-v1.json').read_text())
    PYTHON
    narwhal dev init --template runs/four-engine-sglang-template.json \
      --instance runs/dev-sglang4 --interface "$interface"
    narwhal dev up --instance runs/dev-sglang4
    narwhal dev verify --instance runs/dev-sglang4
    ```

3. From the Narwhal checkout, replay the role cycle and stop the instance:

    ```bash
    python -m tools.measurement.dev_cycle --instance runs/dev-sglang4
    narwhal dev down --instance runs/dev-sglang4
    ```

The replay has the split sequence, pass conditions, records and exit codes of the [reference role cycle](dev/RTX-5090-Reference.md#replaying-all-three-role-splits). Each phase of this template sets its load in engine-equivalents instead of a fixed request rate. Before the first phase, the replay reads the run's `profiles.json` and takes, for each phase, the profiles that the role controller prices at the split where the phase starts: each engine's profile measured at that split in its role there. It prices the phase's requests by the mean prefill time at their input length and by the mean decode requests per second that one engine serves within the TPOT budget, and from those prices sets the phase's request rate, request count and in-flight cap.

| Phase | Starts at | Input / output tokens | Target | Duration |
| --- | :---: | :---: | --- | :---: |
| Decode | 2P:2D | 256 / 128 | Decode demand of 1.8 engines | 48 s |
| Prefill steady | 1P:3D | 3,840 / 1 | Prefill demand of 0.22 engines | 35 s |
| Prefill burst | 2P:2D | 3,840 / 1 | 3 seconds of prefill work, offered at 100 requests/s and all in flight at once | |

The role controller measures a pool's load as its demand divided by its engine count times `controller.reactive.utilization`, 0.8 in this template, and takes the larger pool load as its objective. It moves one engine when the move lowers the objective by at least `controller.reactive.movement_margin`, 0.05, and leaves the pool that gives up the engine at or below `controller.thresholds.shrink`, 0.5. A move into a pool whose load is at or above `controller.thresholds.expand`, 1.0, needs one confirming step instead of three. The controller holds while demand stays below `controller.reactive.demand_floor`, 0.2 engines.

- Decode: 1.8 engines of decode demand load two decode engines to 1.8 / (2 × 0.8) = 1.125 and three to 0.75, so the move to 1P:3D lowers the objective by 0.375.
- Prefill steady: when every split prices prompts alike and prefill load sets the objective, a steady prefill demand P lowers the objective by P / (2 × 0.8) on the move from 1P:3D to 2P:2D and by P / (6 × 0.8) on a further move to 3P:1D. The controller moves to 2P:2D when P is at least 2 × 0.8 × 0.05 = 0.08 and holds there while P stays below 6 × 0.8 × 0.05 = 0.24. The phase's prefill and decode demand together must also reach the demand floor. The target of 0.22 meets all three bounds.
- Prefill burst: 3 seconds of prefill work queues 1.5 seconds on each of two prefill engines, above the 1-second TTFT budget.

The replay computes each steady phase's target range from the profiles at its starting split, the split it moves to and the split beyond, and stops with an error when the target falls outside that range. It also stops with an error when:

- the Decode or Prefill steady phase would offer fewer than `controller.reactive.min_arrivals` requests, 10, in one 30-second demand window
- the burst, capped at the router's in-flight limit of `serving.max_connections` plus `serving.queue_capacity` requests, would queue no more than the TTFT budget on each prefill engine
- two phases would repeat a prompt: a phase's requests and its warm-up request take their prompts from consecutive seeds starting at the phase's `seed`

The replay saves the sized phases in `phases.json`. Each phase there, and its entry in `summary.json`, holds a `sizing` object:

| Field | Value |
| --- | --- |
| `split` | Prefill and decode engine counts where the phase starts |
| `prefill_s` | Mean prefill seconds of one request at that split |
| `decode_rps` | Mean decode requests per second that one engine serves within the TPOT budget, for a steady phase |
| `prefill_engines`, `decode_engines` | Offered demand in engine-equivalents, for a steady phase |
| `target_band` | Lowest and highest target at which the controller makes the phase's move and holds, for a steady phase |
| `prefill_backlog_s` | Prefill seconds the burst queues |

## Inspecting and stopping the instance

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

`dev up` and `dev verify` run subprocess stages with time budgets. `NARWHAL_STAGE_TIMEOUT_SECONDS` sets the budget for every stage.

`NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS` sets the budget for one stage. `<NAME>` is the stage name in uppercase, with hyphens replaced by underscores.

Stages and default budgets:

| Stage | Work | Default budget |
| --- | --- | --- |
| `engine-<n>` | Runtime check for engine `n` | 300 seconds |
| `native-start-shared` | Native shared startup | 180 seconds per engine |
| `attest-<n>` | Attestation capture for engine `n` | 300 seconds |
| `profile-<p>p<d>d` | Profiling a role split with `p` prefill and `d` decode engines | 300 seconds |
| `profile-merge` | Merging the role-split profiles, when `up` profiles more than one split | 300 seconds |
| `preflight` | Directed KV preflight during `verify` | 300 seconds |

Override the startup and preflight budgets:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=900 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

A stage budget covers the stage's work, helper imports, and subprocess startup. Each profiled role split gets a fresh stage budget.

Native shared startup allows 180 seconds for each engine health check. Each HTTP health request times out after 2 seconds, and the routed verification request times out after 30 seconds.

`narwhal dev` stops the stage's supervised processes when:

- a stage budget expires
- `narwhal dev` receives SIGINT (Ctrl-C) during a stage
- `narwhal dev` receives SIGTERM during a stage

Cleanup runs in two steps:

1. `narwhal dev` sends SIGTERM and waits up to `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS`, 10 seconds by default.
2. `narwhal dev` sends SIGKILL to surviving processes and waits up to `NARWHAL_STAGE_KILL_GRACE_SECONDS`, 5 seconds by default.

Both grace periods add to the execution budget.

Files next to each stage log:

| File | Content |
| --- | --- |
| `*.command.json` | The stage's command line |
| `*.stdout`, `*.stderr` | The stage's private output |
| `*.stage.json` | The budget, wall-clock start, elapsed time, exit status, process identities, cleanup escalation, and surviving PIDs |

When `up` fails, startup rolls back and `lifecycle.json` records the original failure and teardown errors.

When `verify` fails, the attempt directory stays. `status` reports `degraded` with the reason until a later `verify` succeeds or `down` completes.

Recover from a failed stage, with `PATH` as the instance directory:

1. Read the failed stage's output.
2. Run `narwhal dev status --instance PATH`.
3. Stop the process generation with `narwhal dev down --instance PATH`.
4. Run `narwhal dev up --instance PATH`.

If `status` or `down` lists surviving PIDs, [inspect each one by hand](dev/Recovery-and-Qualification.md) against its recorded boot ID, start tick, and process group.

## Contributing another GPU recipe

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
