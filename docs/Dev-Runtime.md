# Narwhal dev

Narwhal dev lets you run several independent inference engines on a single GPU. You can start them, profile them, check that they work, and shut them down again, all from one CLI. The native backend uses NVIDIA CUDA and runs on Ubuntu, either directly or under WSL2.

Everything about an instance lives in one directory (`runs/dev` unless you say otherwise): the model choice, the runtime, the GPU allocation, the ports, the profiles, and the process identities. Engines load model files from the Hugging Face cache, or from paths you pass to `narwhal dev init`.

The template that ships with Narwhal starts two engines, one prefill and one decode, running the Qwen3.5-0.8B GGUF model. It's meant for NVIDIA GPUs with 8 GB of VRAM or less. If you want a bigger setup, the [four-engine reference template](dev/RTX-5090-Reference.md) has been measured on an RTX 5090 and is pinned to that card.

## Prepare Ubuntu or WSL2

Start by installing the NVIDIA driver on the host. On native Ubuntu, pick a driver that's compatible with the CUDA runtime you plan to use. On WSL2, install the driver on Windows instead, following [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/), then work from an Ubuntu shell inside WSL2. In both cases, keep the checkout, the model, the virtual environment, and the instance directory on the Linux filesystem, and run every command from that Ubuntu shell.

Next, look at your GPU and your IPv4 interfaces:

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

If `nvidia-smi` isn't on your `PATH`, the commands fall back to the WSL2 binary at `/usr/lib/wsl/lib/nvidia-smi`. Note the interface name, because Narwhal dev runs NIXL/UCX over `eth0` by default. Pick an interface that has exactly one IPv4 address.

## Install the runtime and model

The [CUDA runtime and model steps](dev/CUDA-Runtime.md) walk through installing the versions the template pins: vLLM, Torch, NIXL, Transformers, the GGUF loader, the model, and the tokenizer.

Then decide how much GPU memory to hand out. Two `init` flags control it:

- `--gpu-memory-utilization` (default 0.35) is the share of total VRAM each vLLM process gets, covering model weights, runtime overhead, and KV cache.
- `--device-allowance` (default 0.8) caps two things: the sum of the engine fractions, and how much whole-device memory use can grow during startup.

`init` refuses to continue unless free VRAM covers the device allowance plus the template's 512 MiB reserve. On a fresh instance, tune both flags to whatever your card actually has free.

The shipped template also sets development SLO targets of 5 seconds for time to first token (TTFT) and 500 ms for time per output token (TPOT). Treat those as placeholders and set your own from measurements on your card.

If you need something different, a custom `--template` can change the model, runtime, context length, profiling sweep, and reserve. Its `gpu.product` field pins a specific card, and `gpu.minimum_total_mib` sets a floor on total VRAM.

The easiest way to start a new recipe is to export the installed template:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/small-cuda-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
PYTHON
```

Edit that file, then initialize a fresh instance with
`narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom`.

## Initialize and verify an instance

With the interface name from earlier, four commands take you from nothing to a verified instance:

```bash
interface=eth0  # replace with the interface reported by ip
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

Here's what they do:

- `init` picks a CUDA GPU by UUID, checks the model, runtime, and GPU allocation, and writes the private instance.
- `up` checks that the ports are free, starts and profiles the engines, captures attestations, and brings up the router.
- `verify` runs preflight over every eligible directed KV path, sends a routed arithmetic request, and reports `ready` if everything passes.

One thing to know: the instance remembers which Python interpreter ran `init`, and every later lifecycle command has to run in that same environment.

Each command prints its [lifecycle result](cli/Dev.md) as a single JSON document on stdout. Preparation, profiling, and error diagnostics go to stderr. If you're scripting against the CLI, `--format json` gives you [versioned command results and automation exit codes](Command-Results.md).

By default the router listens at `http://127.0.0.1:18000`. After verification you can look at its state and metrics:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

On WSL2, there's an optional [monitoring example](observability/04-WSL2.md) that forwards those metrics to a separate Prometheus and Grafana host. To change the defaults, pass `--port-base` to `narwhal dev init` for a different port layout, or `--instance` to any command to target another instance.

## Inspect and stop the instance

`status` prints the current run directory. Inside it you'll find the routed response, memory samples, the request journal, and `teardown.json`, plus three kinds of logs:

- `engine-*/startup.log` covers model loading and engine requests.
- `profile-*.log` covers probe and fit outcomes.
- `verify-*/preflight.log` covers the runtime, profile, and transfer checks.

When you're done, stop the instance and confirm:

```bash
narwhal dev down
narwhal dev status
```

`down` stops the process groups tied to the recorded boot ID and start ticks, then reports `stopped`. The next `up` starts and profiles a brand-new set of processes in a new run directory. So after any runtime change or engine restart, run `down`, `up`, and `verify` again.

## Stage deadlines and recovery

`dev up` and `dev verify` split their work into separate subprocess stages, each with its own time budget: runtime checks, native shared startup, attestation, profiling, and preflight.

By default every stage gets 300 seconds, set by `NARWHAL_STAGE_TIMEOUT_SECONDS`. To change one stage, use `NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS`, where `<NAME>` is the stage name in uppercase with hyphens turned into underscores. The stage names are:

- `engine-<n>`: the runtime check for engine `n`
- `native-start-shared`
- `attest-<n>`: attestation capture for engine `n`
- `profile-<p>p<d>d`: profiling a role split with `p` prefill and `d` decode engines
- `profile-merge`: used when there are three or more engines
- `preflight`: runs during `verify`

For example, to give startup and preflight more room:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=720 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

A stage's budget includes helper imports and subprocess startup, not just the work itself. Each profiled role split gets a fresh budget. Inside native shared startup, every engine health check has its own 180-second limit, and HTTP health requests and the routed verification request have their own deadlines too.

If a budget runs out, or you hit Ctrl-C (SIGINT) or the process gets SIGTERM during a helper stage, `narwhal dev` shuts down that stage's supervised processes in two steps. It sends SIGTERM and waits up to `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS` (10 seconds). Anything still alive gets SIGKILL, followed by up to `NARWHAL_STAGE_KILL_GRACE_SECONDS` (5 seconds) of waiting. These grace periods are added on top of the execution budget.

Every stage leaves evidence next to its log:

- `*.command.json`: the stage's command line
- `*.stdout` and `*.stderr`: the private partial output
- `*.stage.json`: the budget, wall-clock start, elapsed time, exit status, process identities, any cleanup escalation, and surviving PIDs

What happens after a failure depends on where it occurred. If `up` fails, startup rolls back, and `lifecycle.json` records the original failure along with any errors from teardown. If `verify` fails, the attempt directory is kept and `status` reports `degraded` along with the reason. It stays that way until you fix the problem and a later `verify` succeeds.

To recover from a failed stage, let `PATH` be the instance directory and work through these steps:

1. Read the failed stage's output.
2. Run `narwhal dev status --instance PATH`.
3. Stop the process generation with `narwhal dev down --instance PATH`.
4. Try startup again.

If `status` or `down` lists surviving PIDs, you'll need to [inspect them by hand](dev/Recovery-and-Qualification.md), checking each one's recorded boot ID, start tick, and process group.

## Contribute another GPU recipe

Have a small CUDA GPU that Narwhal doesn't cover yet? Follow the [checks and pull request workflow](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) and submit your working template with qualification evidence. That evidence should record the GPU, driver, model, and runtime versions; the free-memory reserve and peak startup memory; both directed KV transfers; and the routed completion.

Support for a GPU from another vendor takes more. Along with a measured template, the contribution can add discovery, memory accounting, engine launch, and a compatible transfer runtime.
