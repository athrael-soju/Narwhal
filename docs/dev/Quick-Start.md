---
description: Take a clean Linux or WSL2 machine to a routed reply from four llama.cpp engines with narwhal dev setup, on a GPU or on the CPU alone.
---

# Quick start on one workstation

`narwhal dev setup` downloads a llama.cpp engine build and a small model, and `narwhal dev up` starts four engines and a router on one machine. The engines run on an NVIDIA GPU, or on the CPU when the machine has none. The fleet hands each request's KV cache from a prefill engine to a decode engine and moves engines between the two roles as the load changes.

## Requirements

| | Qwen3.5-0.8B, the default | ERNIE-4.5-0.3B |
| --- | --- | --- |
| Download | 0.6 GB | 0.3 GB |
| Disk after setup | 587 MB | 308 MB |
| Memory with four engines on the CPU | 2.4 GB | 1.5 GB |
| `setup`, downloads included, on a fast connection | 5 s | 4 s |
| `up` | 3 s | 2 s |

The figures were measured on a Linux host with the model on the CPU. Both model files are 4-bit GGUF quantizations. While the fleet serves, the engines keep handed-over KV state in files under the instance directory, up to 0.5 GB for the default model.

The host needs:

- Linux on x86-64, or Windows with [WSL2](https://learn.microsoft.com/windows/wsl/install) and an Ubuntu distribution
- Python 3.11.4 or newer with `venv`
- For a GPU, an NVIDIA driver. `setup` then downloads a CUDA build of the engine together with its runtime libraries.

## Installing

Run every command from a Linux shell, inside WSL2 on a Windows host.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
```

## Setting up the instance

```bash
narwhal dev setup
```

`setup` writes the instance to `runs/dev`. It downloads the engine build and the model file into `runs/dev/assets`, checks each file's SHA-256 against the value the template pins, and checks the host:

```json
{
  "status": "initialized",
  "instance": "/home/you/runs/dev",
  "model": "Qwen3.5-0.8B",
  "engine_build": "none",
  "downloaded": [
    "llama-b12365-bin-ubuntu-x64.tar.gz",
    "Qwen3.5-0.8B-Q4_K_M.gguf"
  ]
}
```

`engine_build` is `cuda` when `setup` finds an NVIDIA driver, and `none` for the CPU build.

| To change | Pass |
| --- | --- |
| The model | `--model-name ERNIE-4.5-0.3B` |
| The engine build | `--accelerator none` for the CPU, or `--accelerator cuda12` for a driver that predates CUDA 13 |
| The number of engines, two to eight | `--engine-count 2` |
| The ports, when 18000 or 18101 to 18104 are taken | `--port-base 19000` |
| The instance directory | `--instance runs/second` |

A second `setup` on the same directory reports `reused` and downloads nothing. To change a setting, name a new directory with `--instance`. Files already in its `assets` directory with the pinned checksums are kept.

## Starting the fleet

```bash
narwhal dev up
```

`up` starts the engines, sends each one short request to measure its speed, and starts the router at `http://127.0.0.1:18000`. Half of the engines start in the prefill role and half in the decode role.

```json
{
  "status": "launched",
  "run": "/home/you/runs/dev/run-03020490194f",
  "router": "http://127.0.0.1:18000"
}
```

## Sending a prompt

```bash
narwhal dev ask "What is the capital of France?"
```

```json
{
  "status": "answered",
  "reply": "The capital of France is **Paris**.\n\nLocated in the west of the country, it sits on the banks of the Seine River and serves as the seat of the French government, the executive branch of the state, and the head of the national army.",
  "ttft_s": 0.118,
  "output_tokens": 52,
  "tokens_per_second": 105.8,
  "engines": {
    "prefill": "n1",
    "decode": "n3"
  }
}
```

`engines` names the engine that computed the prompt and the engine that generated the reply from the handed-over KV cache. Both models are under one billion parameters, so their replies read well and often get facts wrong, as this one does on where Paris lies.

The router serves the OpenAI completion and chat routes, so any OpenAI client works against it:

```bash
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model": "Qwen3.5-0.8B", "messages": [{"role": "user", "content": "Say hello."}]}'
```

## Sending many prompts and watching the fleet

In a second shell, with the same virtual environment active, open the live view:

```bash
narwhal dev watch
```

In the first shell, send 600 built-in prompts with eight in flight:

```bash
narwhal dev ask --repeat 600 --concurrency 8 --max-tokens 200
```

The live view shows each engine's role and requests, the latency and the throughput. Long replies load the decode engines, and after 15 to 25 seconds the router moves one prefill engine to the decode role. [Reading the live view](Live-View.md) explains each figure.

When the prompts finish, `ask` prints their latency spread and throughput:

```json
{
  "status": "completed",
  "requests": 600,
  "failed": 0,
  "concurrency": 8,
  "seconds": 170.0,
  "ttft_s": {"p50": 0.393, "p95": 2.553},
  "tpot_s": {"p50": 0.018, "p95": 0.019},
  "requests_per_second": 3.5,
  "output_tokens_per_second": 294.9,
  "replies_without_token_count": 0
}
```

## Stopping

```bash
narwhal dev down
```

`down` stops the engines and the router. The instance directory keeps the downloaded files, the logs and the request journal of each run, and `narwhal dev up` starts the fleet again.

## When a step fails

| Message | Action |
| --- | --- |
| `port 18000 is unavailable on 127.0.0.1` | Run `setup` with `--port-base` and a free port, on a new `--instance`. |
| `holds other files; name an empty or new instance directory` | Pass `--instance` with a new directory. |
| `SHA-256 … differs from the pinned` | Run `setup` again. It removed the file and downloads it once more. |
| `process exited before … became healthy` during `up` | Read `engine-1.log` in the run directory that the message names. With the `cuda` build on an older driver, set up a new instance with `--accelerator cuda12` or `--accelerator none`. |
| `run dev down before starting another generation` | Run `narwhal dev down`, then `narwhal dev up`. |

## Differences from a deployed fleet

The quick-start fleet gives each engine a profile from one short request. It runs without engine attestation, a profiling sweep and KV transfer evidence, which a [deployed fleet](../Deploy.md) requires. [llama.cpp engines](Llama-Cpp-Engines.md) describes the engine settings, the KV handoff and the metrics.
