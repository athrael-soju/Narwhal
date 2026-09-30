# Narwhal dev

Narwhal dev runs several vLLM engines on one NVIDIA GPU under Ubuntu or
Ubuntu on WSL2. It starts and profiles the engines, verifies KV-cache
transfer between them, and stops them cleanly.

Each instance has its own directory, `runs/dev` by default. The directory
records:

- the model and runtime
- the GPU memory split between engines
- the ports
- the engine profiles
- the process identities used to stop the instance

The installed template runs one prefill engine and one decode engine with
Qwen3.5-0.8B GGUF on NVIDIA GPUs with 8 GB of VRAM or less. A four-engine
recipe for the RTX 5090 is also provided.

## Getting started

Follow these pages in order for a first setup.

1. [Prepare Ubuntu or WSL2](dev/01-Prepare-Host.md): check the driver and
   pick a network interface.
2. [Install the CUDA runtime and model](dev/02-CUDA-Runtime.md): build the
   pinned Python environment and download the model.
3. [Tune the template and memory allocation](dev/03-Templates.md): optional;
   use it when the defaults do not fit your card.
4. [Run and stop an instance](dev/04-Run-Instance.md): start the instance,
   verify it, and shut it down.

## Troubleshooting

- If a startup or verification stage hangs or times out, see
  [Stage deadlines and recovery](dev/05-Stage-Recovery.md).
- If a `narwhal dev` command was interrupted (Ctrl+C or killed), see
  [Controller interruption recovery](dev/06-Controller-Interruption.md).

## Reference GPU pages

- [Reference GPU recovery checks](dev/07-Recovery-Qualification.md): optional
  procedure that checks cleanup on the reference GPU.
- [RTX 5090 reference](dev/08-RTX-5090-Reference.md): the four-engine setup
  and its [role-cycle workload](dev/08-RTX-5090-Reference.md#replay-the-role-cycle).

## Other reference pages

- [`narwhal dev` command reference](cli/Dev.md) lists every flag, lifecycle
  result, and exit code.
- [Command results](Command-Results.md) describes the versioned JSON output
  used for scripting.
- [Monitor a WSL2 development fleet](observability/04-WSL2.md) sets up
  Prometheus and Grafana on a second machine.

## Contributing a recipe for another GPU

Recipes for other CUDA GPUs are welcome. For another small CUDA card, send a
working template with:

- GPU, driver, model, and runtime versions
- the free-memory reserve
- peak memory at startup
- evidence that both directed KV transfers and a routed completion succeeded

Supporting another GPU vendor requires device discovery and memory
accounting, an engine launch path, a compatible transfer runtime, and a
template built from real measurements.

Open a pull request using the
[contribution workflow](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md).
