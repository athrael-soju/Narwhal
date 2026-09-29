# Narwhal dev

Narwhal dev starts, profiles, verifies and stops independent engines sharing
one NVIDIA GPU on Ubuntu or Ubuntu under WSL2. The default instance directory
`runs/dev` holds the model, runtime, GPU allocation, ports, profiles and
process identities.

The installed template starts one prefill and one decode engine with
Qwen3.5-0.8B GGUF on an NVIDIA GPU with **8 GB of VRAM or less**.

## Setup sequence

1. [Prepare Ubuntu or WSL2](dev/01-Prepare-Host.md)
2. [Install the CUDA runtime and model](dev/02-CUDA-Runtime.md)
3. [Tune the template and memory allocation](dev/03-Templates.md)
4. [Run and stop an instance](dev/04-Run-Instance.md)

## Recovery and qualification

- [Stage deadlines and recovery](dev/05-Stage-Recovery.md)
- [Controller interruption recovery](dev/06-Controller-Interruption.md)
- [Reference GPU recovery checks](dev/07-Recovery-Qualification.md)
- [RTX 5090 four-engine reference](dev/08-RTX-5090-Reference.md)

## Related reference material

- [`narwhal dev` command reference](cli/Dev.md): flags, lifecycle results and exit codes.
- [Command results](Command-Results.md): versioned JSON results and automation exit codes.
- [Monitor a WSL2 development fleet](observability/04-WSL2.md): Prometheus and Grafana on a separate host.
- [Four-engine role cycle](dev/08-RTX-5090-Reference.md#replay-all-three-role-splits): the RTX 5090 reference's workload and split targets.

## Contribute another GPU recipe

| Recipe             | Contents                                                                                                                                              |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Small CUDA GPU     | Working template, GPU, driver, model and runtime versions, free-memory reserve, peak startup memory, both directed KV transfers, routed completion |
| Another GPU vendor | Discovery, memory accounting, engine launch, a compatible transfer runtime and a measured template                                                   |

Open a pull request with the recipe through the
[contribution workflow](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md).
