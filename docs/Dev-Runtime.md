# Narwhal dev

Narwhal dev starts, profiles, verifies and stops a local fleet of independent
engines sharing one GPU. Its native backend currently uses NVIDIA CUDA and
runs on Ubuntu or Ubuntu under WSL2. Each instance keeps its model, runtime,
GPU allocation, ports, profiles and process identities together under
`runs/dev` by default.

The intended small-GPU target is **8 GB of VRAM or less**. The repository
currently ships a measured [RTX 5090 reference](dev/RTX-5090-Reference.md)
with four engines, a pinned model and a 30,000 MiB minimum. That reference
establishes the command and transfer workflow; a smaller GPU needs a
separately measured model and runtime template with at least one prefill and
one decode engine. The current native backend selects NVIDIA GPUs through
`nvidia-smi`.

## Prepare Ubuntu or WSL2

On native Ubuntu, use an NVIDIA driver compatible with the selected CUDA
runtime. On WSL2, install the NVIDIA driver on Windows and run Ubuntu inside
WSL2. [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/)
describes the Windows driver setup. Keep the checkout, model, virtual
environment and instance directory on the Linux filesystem in either case.

In the Ubuntu shell, inspect the available GPU and IPv4 interfaces:

```bash
nvidia-smi --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

WSL2 exposes `nvidia-smi` at `/usr/lib/wsl/lib/nvidia-smi` when that path is
outside the shell's `PATH`. Choose an interface with one IPv4 address for
NIXL/UCX; its name may differ from the default `eth0`. Narwhal dev uses the
same Linux process lifecycle and commands on both hosts.

## Select a runtime and template

The installed reference pairs the RTX 5090 with Qwen3.5-0.8B GGUF and pinned
vLLM, Torch, NIXL, Transformers and GGUF loader versions. Follow its
[installation and model steps](dev/RTX-5090-Reference.md#install-the-reference-runtime)
when using that template. A different NVIDIA GPU needs a template whose
`gpu.product`, `gpu.minimum_total_mib`, `gpu.reserve_mib`, model checksums,
runtime pins and memory allocation describe its tested recipe. `--template`
selects that file; `--engine-count`, `--gpu-memory-utilization` and
`--device-allowance` can adjust its allocation for a fresh instance.

The engine-count floor is two, allowing one prefill and one decode engine.
Narwhal checks the summed vLLM fractions and observed whole-device startup
growth against the template's allowance, while leaving its free-memory
reserve available. Model weights, runtime overhead and KV cache also consume
VRAM. A smaller template must establish its own measured fit and directed
KV transfer result on the target GPU.

## Initialize and verify an instance

Select the IPv4 interface found above. Use `--template /path/to/template.json`
for a recipe other than the installed RTX 5090 reference:

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

The reference router listens at `http://127.0.0.1:18000`. Inspect its state
and metrics after verification:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

Select another port layout with `narwhal dev init --port-base`, or another
instance with `--instance` on each command. The [CLI reference](cli/Dev.md)
lists the flags, lifecycle results and exit codes. The optional
[four-engine role cycle](dev/RTX-5090-Reference.md#replay-all-three-role-splits)
uses the installed reference's workload and split targets.

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

The first small-GPU milestone is an NVIDIA CUDA recipe that launches and
verifies two engines within 8 GB of VRAM or less. Record the GPU, driver,
model and runtime versions, free-memory reserve, peak startup memory, both
directed KV transfers and routed completion in its qualification evidence.

Contributions for other GPU vendors can add discovery, memory accounting,
engine launch and a compatible transfer runtime alongside a measured
template. [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
describes the checks and pull request workflow.
