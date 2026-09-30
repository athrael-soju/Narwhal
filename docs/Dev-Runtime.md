# Narwhal dev

Narwhal dev runs several vLLM engines side by side on one NVIDIA GPU, on
Ubuntu or on Ubuntu under WSL2. It starts the engines, profiles them, checks
that they can hand KV cache to each other, and shuts them down cleanly
afterward.

Each instance has its own directory, `runs/dev` unless you choose another.
That directory records which model and runtime the instance uses, how GPU
memory is split between engines, which ports it listens on, the engine
profiles, and the process identities Narwhal needs to stop everything later.

The installed template is sized for small cards. It runs one prefill engine
and one decode engine with Qwen3.5-0.8B GGUF, and it's meant for NVIDIA GPUs
with 8 GB of VRAM or less. There's also a four-engine recipe for the
RTX 5090.

## Getting started

Work through these pages in order the first time you set up a machine:

1. [Prepare Ubuntu or WSL2](dev/01-Prepare-Host.md): check the driver and
   pick a network interface.
2. [Install the CUDA runtime and model](dev/02-CUDA-Runtime.md): build the
   pinned Python environment and download the model.
3. [Tune the template and memory allocation](dev/03-Templates.md): only
   needed if the defaults don't suit your card.
4. [Run and stop an instance](dev/04-Run-Instance.md)

## When something goes wrong

If a startup or verification stage hangs or times out, read
[Stage deadlines and recovery](dev/05-Stage-Recovery.md). If a `narwhal dev`
command itself was interrupted, whether by Ctrl+C or by something killing
it outright, read [Controller interruption recovery](dev/06-Controller-Interruption.md).

[Reference GPU recovery checks](dev/07-Recovery-Qualification.md) is a
separate, optional procedure for proving that cleanup works on the reference
GPU. The [RTX 5090 reference](dev/08-RTX-5090-Reference.md) walks through the
four-engine setup and its [role-cycle workload](dev/08-RTX-5090-Reference.md#replay-the-role-cycle).

## Other reference pages

- [`narwhal dev` command reference](cli/Dev.md) lists every flag, lifecycle
  result, and exit code.
- [Command results](Command-Results.md) describes the versioned JSON output
  used for scripting.
- [Monitor a WSL2 development fleet](observability/04-WSL2.md) sets up
  Prometheus and Grafana on a second machine.

## Contributing a recipe for another GPU

We'd like recipes for more hardware. For another small CUDA card, send a
working template along with the GPU, driver, model, and runtime versions you
used. Include the free-memory reserve you settled on, the peak memory you saw
during startup, and evidence that both directed KV transfers and a routed
completion succeeded.

Supporting a different GPU vendor is a bigger job. Narwhal would need device
discovery and memory accounting for that hardware, a way to launch engines on
it, a transfer runtime that works with it, and a template built from real
measurements.

Either way, open a pull request following the
[contribution workflow](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md).
