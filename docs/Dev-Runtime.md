# Narwhal dev

Narwhal dev starts, profiles, verifies and stops independent engines sharing
one GPU. The current native backend uses NVIDIA CUDA on Ubuntu or Ubuntu under
WSL2. Each instance keeps its model, runtime, GPU allocation, ports, profiles
and process identities under `runs/dev` by default.

The installed template starts two engines, one prefill and one decode, with
Qwen3.5-0.8B GGUF. It selects a CUDA GPU by UUID and checks available VRAM
against its allocation and 512 MiB reserve. The target is an NVIDIA GPU with
**8 GB of VRAM or less**; a successful `up` and `verify` establishes fit and
directed KV transfer on the selected card. The separate
[RTX 5090 reference](dev/RTX-5090-Reference.md) records the measured
four-engine configuration.

## Prepare Ubuntu or WSL2

On native Ubuntu, use an NVIDIA driver compatible with the selected CUDA
runtime. On WSL2, install the NVIDIA driver on Windows and run Ubuntu inside
WSL2. [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/)
describes the Windows driver setup. Keep the checkout, model, virtual
environment and instance directory on the Linux filesystem in either case.

In the Ubuntu shell, inspect the available GPU and IPv4 interfaces:

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

WSL2 exposes `nvidia-smi` at `/usr/lib/wsl/lib/nvidia-smi` when that path is
outside the shell's `PATH`. Choose an interface with one IPv4 address for
NIXL/UCX; its name may differ from the default `eth0`. The commands below
run in the Ubuntu shell on both hosts.

## Install the runtime and model

Follow the [CUDA runtime and model steps](dev/CUDA-Runtime.md) in the Ubuntu
shell. The installed template pins vLLM, Torch, NIXL, Transformers, the GGUF
loader, model and tokenizer. CUDA support currently covers NVIDIA GPUs;
contributions can add discovery, launch and transfer support for other
vendors.

The default two-engine allocation assigns each vLLM process 0.35 of total
VRAM, caps whole-device startup growth at 0.8 and reserves 512 MiB of free
memory at initialization. Model weights, runtime overhead and KV cache
consume that budget. Tune `--gpu-memory-utilization` and `--device-allowance`
for a fresh instance after checking the card's available memory. Its initial
development budgets are 5 seconds TTFT and 500 ms TPOT; set targets from
measurements on the selected card. A custom
`--template` can change the model, runtime, context length, profiling sweep
and reserve. A recipe can set `gpu.product` to pin a card and
`gpu.minimum_total_mib` to require a measured capacity.

Export the installed template when preparing a new recipe:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/small-cuda-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
PYTHON
```

Edit the file, then initialize a fresh instance with
`narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom`.

## Initialize and verify an instance

Select the IPv4 interface found above:

```bash
interface=eth0  # replace with the interface reported by ip
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

`init` writes the private instance after checking the model, runtime and GPU
allocation. `up` checks ports, starts and profiles the engines, captures
attestations and starts the router. `verify` runs preflight across the
eligible directed KV paths, sends a routed arithmetic request and retains
the response and metrics before reporting `ready`. Keep the same Python
environment active for subsequent lifecycle commands; the instance records
its interpreter.

Lifecycle results are one JSON document on stdout. Preparation, profiling
and error diagnostics go to stderr. `status` reports `degraded` with the
retained reason when verification fails; a successful `verify` restores
`ready` after the failing input is repaired.

The default router listens at `http://127.0.0.1:18000`. Inspect its state
and metrics after verification:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

Select another port layout with `narwhal dev init --port-base`, or another
instance with `--instance` on each command. The [CLI reference](cli/Dev.md)
lists the flags, lifecycle results and exit codes. The optional
[four-engine role cycle](dev/RTX-5090-Reference.md#replay-all-three-role-splits)
uses the RTX 5090 reference's workload and split targets.

## Inspect and stop the instance

`status` prints the current run directory. Its `engine-*/startup.log` files
record model loading and engine requests, `profile-*.log` files record probe
and fit outcomes, and `verify-*/preflight.log` records the runtime, profile
and transfer checks. The run also retains the routed response, memory
samples, request journal and `teardown.json`.

```bash
narwhal dev down
narwhal dev status
```

`down` stops process groups bound to the recorded boot ID and start ticks,
then reports `stopped`. A later `up` creates a fresh run directory and
profiles the new engine processes. On WSL2, the optional
[monitoring example](observability/04-WSL2.md) forwards the local metrics to
a separate Prometheus and Grafana host.

## Contribute another GPU recipe

For another small CUDA GPU, record the GPU, driver, model and runtime
versions, free-memory reserve, peak startup memory, both directed KV
transfers and routed completion. Share its working template and qualification
evidence so the allocation can be reproduced.

Contributions for other GPU vendors can add discovery, memory accounting,
engine launch and a compatible transfer runtime alongside a measured
template. [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
describes the checks and pull request workflow.
