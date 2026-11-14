---
description: How Narwhal runs llama.cpp engines in a development fleet, with their template fields, command line, KV handoff, profiles and metrics.
---

# llama.cpp engines

The `llamacpp` backend routes requests over `llama-server` processes. `narwhal dev` starts them directly from a template with `"start": "direct"`, and [the quick start](Quick-Start.md) covers the commands. The backend serves development fleets on one host. A [deployed fleet](../Deploy.md) uses vLLM or SGLang.

## Templates

Narwhal installs two templates that start llama.cpp engines:

| Template | Model | Engines | Host memory `setup` and `init` require for each engine on the CPU |
| --- | --- | --- | --- |
| `direct-qwen35-08b-v1.json`, the default of `setup` | Qwen3.5-0.8B Q4_K_M GGUF | 4 | 700 MiB |
| `direct-ernie45-03b-v1.json` | ERNIE-4.5-0.3B Q4_K_M GGUF | 4 | 450 MiB |

`narwhal dev setup --model-name` selects one by the model it serves. `narwhal dev init --template PATH --engine-binary PATH --model PATH` initializes an instance from files already on the host.

| Field | Meaning |
| --- | --- |
| `schema`, `schema_version` | `narwhal.dev-template` and `1`. |
| `start` | `direct` for engines that start as plain processes. |
| `setup_default` | `true` on the template `setup` uses when `--model-name` is omitted. |
| `model.repository`, `model.revision`, `model.filename`, `model.sha256` | The model file `setup` downloads and the checksum `setup`, `init` and `up` require. |
| `model.served_name` | The model name the router and the engines serve. |
| `engine.url`, `engine.binary` | The download URL pattern of an engine archive, with `{name}` for the archive's name, and the server's file name inside it. |
| `engine.builds` | The archives and checksums of each engine build by machine type. The keys are the values of `--accelerator`, and `none` is the CPU build. `detect` lists the paths whose presence selects the build under `--accelerator auto`. The installed templates pin `none`, `cuda12` for CUDA 12.8 and `cuda` for CUDA 13.5, and `auto` selects `cuda12` on a host with an NVIDIA driver. |
| `runtime.backend` | `llamacpp`. |
| `runtime.build` | The llama.cpp build number the server must report. |
| `runtime.slots` | Requests one engine serves at once. |
| `runtime.context_tokens` | Context tokens of each slot. |
| `runtime.memory_mib` | Available host memory required for each engine by `setup` with the `none` build and by `init` with `--no-accelerator`. |
| `runtime.extra_args` | Further `llama-server` arguments. |
| `request` | Optional fields `narwhal dev ask` adds to each request body. |
| `allocation.engine_count`, `ports`, `slo`, `controller` | The engine count, the router and first engine port, and the router's [SLO and role-control settings](../configuration/02-Serving-and-Role-Control.md). |
| `serving` | Optional [serving settings](../configuration/02-Serving-and-Role-Control.md#request-admission-and-bounded-serving) of the fleet. The installed templates set `admission` to `open`. |

## Engine command line

`up` starts each engine with these `llama-server` arguments:

| Argument | Value |
| --- | --- |
| `--model`, `--alias` | The model file and `model.served_name` |
| `--host`, `--port` | `127.0.0.1` and the engine's port |
| `--parallel` | `runtime.slots` |
| `--ctx-size` | `runtime.context_tokens` times `runtime.slots`, which the server divides among its slots |
| `--threads` | The host's usable CPUs less one, divided among the engines and rounded down, from 1 to 8 |
| `--n-gpu-layers` | `all`, or `0` with `--no-accelerator` or the CPU build |
| `--cache-ram` | `0`, which turns off the server's host prompt cache |
| `--slot-save-path` | The run's `slot-files` directory, shared by every engine |
| `--metrics`, `--slots`, `--no-webui` | Set |

The server's shared libraries load from the directory of the engine binary.

## KV handoff

A request's prefill and decode legs each take one server slot through `id_slot` and wait while every slot is taken. After the prefill reply, the router saves the producer's slot to a file with `POST /slots/<id>?action=save`. Before the decode stream opens, it restores that file into the consumer's slot with `action=restore`, and the consumer computes any prompt tokens the restored state lacks and generates the reply.

A producer keeps as many slot files as it has had handoffs in flight at once and reuses their names. `down` leaves them in the run directory.

One `llama-server` process serves both roles, so a role switch changes the router's placement and sends the engine nothing.

## Profiles

`up` writes each engine a [nominal profile](../telemetry/02-Profiles.md#nominal-profiles). It sends every engine `runtime.slots` requests of about a hundred prompt tokens and sixteen output tokens concurrently, so each engine is measured while its neighbours on the shared device serve. An engine's profile holds the mean prefill time per token and the mean token interval of its replies, the context of all its slots as KV capacity, and `runtime.slots` as the request limit.

## Token counts and metrics

The router counts a prompt with the engine's `/tokenize`, after rendering chat messages with `/apply-template`. Stream chunks carry text without token IDs, so the router reads each reply's output token count from the last chunk and reports [`token_accounting: reported`](../telemetry/01-Journal.md).

The engines publish `llamacpp:` series on `/metrics`. The router reads `llamacpp:prompt_tokens_cached_total` for cache hits. The engines publish no KV capacity, cache block size or cache events, so [prefix-cache pricing](../configuration/02-Serving-and-Role-Control.md#prefix-cache-pricing) stays off for this backend.

## Limits

- Engine attestation, the profiling sweep of `narwhal-profile` and KV transfer evidence apply to vLLM and SGLang fleets.
- `narwhal-engine` and the [deployment stages](../Deploy.md) launch vLLM and SGLang engines.
- The backend runs on x86-64 Linux and under WSL2, with every engine of a fleet on one host.
