---
description: Take a clean Linux or WSL2 machine to a routed reply from four llama.cpp engines with narwhal dev setup, on a GPU or on the CPU alone.
---

# Quick start on one workstation

`narwhal dev up` downloads a llama.cpp engine build and a small model, then starts four engines and a router on one machine. The engines run on an NVIDIA GPU, or on the CPU when the machine has none. The fleet hands each request's KV cache from a prefill engine to a decode engine and moves engines between the two roles as the load changes.

## Requirements

| | ERNIE-4.5-0.3B on the CPU | ERNIE-4.5-0.3B on a GPU | Qwen3.5-0.8B on the CPU | Qwen3.5-0.8B on a GPU |
| --- | --- | --- | --- | --- |
| Download | 0.3 GB | 1.0 GB | 0.6 GB | 1.3 GB |
| Disk after setup | 0.3 GB | 2.1 GB | 0.6 GB | 2.3 GB |
| Memory four engines use | 1.5 GB of host memory | 4.4 GB of GPU memory | 2.4 GB of host memory | 5.5 GB of GPU memory |
| Available host memory `setup` requires for four engines | 1,800 MiB | No check | 2,800 MiB | No check |
| `setup`, downloads included, on a fast connection | 4 s | 16 s | 5 s | 8 to 26 s |
| First `up` after `setup` | 2 s | 24 s | 3 s | 62 s |
| Later `up` | 2 s | 4 s | 3 s | 5 s |

ERNIE-4.5-0.3B is the default model. Every figure was measured on Linux hosts, the GPU columns on an NVIDIA GPU with the CUDA 12 build of the engine. The steps on this page also ran end to end under WSL2, on the CPU and on an NVIDIA GPU. Both model files are 4-bit GGUF quantizations. While the fleet serves, each run keeps handed-over KV state in files under the instance directory, a few megabytes for ERNIE-4.5-0.3B and up to 0.5 GB for Qwen3.5-0.8B.

The host needs:

- Ubuntu or a comparable Linux distribution on x86-64, or Windows with [WSL2](https://learn.microsoft.com/windows/wsl/install) and an Ubuntu distribution
- Python 3.11.4 or newer with `venv`
- For a GPU, an NVIDIA driver that supports CUDA 12. `setup` then downloads the CUDA 12 build of the engine together with its runtime libraries.

A request's prefill computes the KV cache of its prompt, and its decode generates the reply's tokens from that cache. Narwhal runs the two on different engines.

## Installing

Run every command in a Linux shell. On Windows, use a WSL2 Ubuntu shell.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
```

Run every later command from this directory with the virtual environment active. The instance directory `runs/dev` is relative to it.

To skip installing, [open a codespace](https://codespaces.new/athrael-soju/Narwhal?quickstart=1) on the repository. It installs Narwhal from the checkout into `.venv` and puts it on the terminal's `PATH`, so the commands on this page run as written from the repository root. A codespace has no GPU, so the engines run on the CPU. In the dev container on two CPU cores, four engines served 100 requests at a concurrency of 8 with no failures and a 95th-percentile time to first token of 3.4 s.

## Starting and removing in one command each

```bash
narwhal dev up
```

On its first run, `up` sets up the instance in `runs/dev` and then starts the fleet. It accepts the options of `setup`. The [`narwhal dev` reference](../cli/Dev.md) lists every subcommand and option.

```bash
narwhal dev down --remove
```

`down --remove` stops the fleet and deletes `runs/dev` with its downloads and logs. `python -m pip uninstall narwhal-inference` then removes Narwhal itself.

## Watching roles change in one command

```bash
narwhal dev demo
```

`demo` starts the fleet when it is not up, setting up the instance first on its first run. It then sends long replies for 30 seconds and long prompts for 30 seconds, eight requests at a time, while the live view shows the engines move to decode and back to prefill. When the load ends, it prints what each phase sent and the roles it left. This run used eight engines on a CPU-only machine:

```json
{
  "status": "completed",
  "concurrency": 8,
  "phase_seconds": 30.0,
  "phases": [
    {"name": "long replies", "requests": 134, "failed": 0},
    {"name": "long prompts", "requests": 355, "failed": 0}
  ],
  "requests": 489,
  "failed": 0,
  "role_switches": 10,
  "split": {"prefill": 6, "decode": 2}
}
```

The fleet keeps running afterwards. `--phase-seconds` and `--concurrency` change the load, and [Reading the live view](Live-View.md) explains each figure.

## Setting up the instance

```bash
narwhal dev setup
```

`setup` writes the instance to `runs/dev`. It downloads the engine build and the model file into `runs/dev/assets`, checks each file's SHA-256 against the value the template pins, and checks the host:

```json
{
  "status": "initialized",
  "instance": "/home/you/runs/dev",
  "model": "ERNIE-4.5-0.3B",
  "engine_build": "none",
  "downloaded": [
    "llama-b12365-bin-ubuntu-x64.tar.gz",
    "ERNIE-4.5-0.3B-PT-Q4_K_M.gguf"
  ]
}
```

`engine_build` is `cuda12` when `setup` finds an NVIDIA driver, and `none` for the CPU build.

| To change | Pass |
| --- | --- |
| The model | `--model-name Qwen3.5-0.8B` |
| The engine build | `--accelerator none` for the CPU, or `--accelerator cuda` for the CUDA 13.5 build, for a driver with CUDA 13.5 support |
| The number of engines, two to eight | `--engine-count 2` |
| The ports, when 18000 or 18101 to 18104 are taken | `--port-base 19000` |

A second `setup` on the same directory with the same settings reports `reused` and downloads nothing.

## Changing a setting

To change the number of engines, the model, the engine build or the ports, stop the fleet and start it with the option. The instance keeps the files it has downloaded and downloads a file only when the new setting needs one. An option left out keeps its value. `setup` with the same option changes the instance without starting it, and reports `reconfigured`.

```bash
narwhal dev down
narwhal dev up --engine-count 8
```

One fleet runs at a time. `up` on a second instance directory while a fleet is up reports `a fleet is already up in …` and starts nothing, because a second fleet takes its own share of memory beside the first. `narwhal dev up --alongside` starts it anyway.

## Starting the fleet

```bash
narwhal dev up
```

`up` starts the engines, sends each one short request to measure its speed, and starts the router at `http://127.0.0.1:18000`. Half of the engines, rounded down, start in the prefill role and the rest in the decode role.

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
  "reply": "The capital of France is Paris.",
  "ttft_s": 0.0697,
  "output_tokens": 8,
  "tokens_per_second": 116.6,
  "engines": {
    "prefill": "n2",
    "decode": "n3"
  }
}
```

`engines` names the engine that computed the prompt and the engine that generated the reply from the handed-over KV cache. This reply came from four engines on the CPU. `ask` decodes a single prompt greedily, with temperature 0, so the same prompt gets the same reply. Both models have fewer than one billion parameters. Their replies are fluent, and their answers to simple factual questions are sometimes wrong.

The router serves the OpenAI completion and chat routes, so most OpenAI clients that use those routes work against it:

```bash
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model": "ERNIE-4.5-0.3B", "messages": [{"role": "user", "content": "Say hello."}]}'
```

For `Qwen3.5-0.8B`, add `"chat_template_kwargs": {"enable_thinking": false}` to the body, as `ask` does, to turn that model's reasoning output off.

## Sending many prompts and watching the fleet

In a second shell, change to the same directory, activate the same virtual environment and open the live view, which runs until you press Ctrl+C:

```bash
narwhal dev watch
```

In the first shell, send 2,000 built-in prompts with eight in flight:

```bash
narwhal dev ask --repeat 2000 --concurrency 8 --max-tokens 200
```

The live view shows:

- each engine's role and requests
- the latency
- the throughput

Long replies load the decode engines, and the router decides role changes from the last 30 seconds of load. [Reading the live view](Live-View.md) explains each figure.

When the prompts finish, `ask` prints their latency spread and throughput:

```json
{
  "status": "completed",
  "requests": 2000,
  "failed": 0,
  "concurrency": 8,
  "seconds": 91.2,
  "ttft_s": {"p50": 0.074, "p95": 0.485},
  "tpot_s": {"p50": 0.005, "p95": 0.006},
  "requests_per_second": 21.9,
  "output_tokens_per_second": 876.6,
  "replies_without_token_count": 0
}
```

This output is from the measured CPU host, where the run took 91 seconds. On the measured GPU host it took 78 seconds.

## Stopping

```bash
narwhal dev down
```

`down` stops the engines and the router. The instance directory keeps the downloaded files and the last run's `run-…` directory with the request journal and, under `launch/stages`, each stage's logs, such as `engine-N.log` and `router.log`, and the slot files. The next `up` removes the runs before it. `narwhal dev up` starts the fleet again. `narwhal dev down --remove` also deletes `runs/dev`.

## Troubleshooting

| Message | Action |
| --- | --- |
| `port 18000 is unavailable on 127.0.0.1` from `setup` | Repeat `setup` with `--port-base` and a free port that has four free ports after it. The downloaded files are kept. |
| `host memory check failed` | Repeat `setup` with `--engine-count 2`. A `Qwen3.5-0.8B` instance can instead be set up in a new `--instance` directory with the default model. |
| `holds other files; name an empty or new instance directory` | Pass `--instance` with a new directory. |
| `SHA-256 … differs from the pinned` | Run `setup` again. It removed the file and downloads it once more. |
| `process exited before http://127.0.0.1:18101/health became healthy` during `up` | Run `narwhal dev status` and read `engine-N.log` in the directory its `run` field names. N is the port in the message less 18100, so 18101 is `engine-1.log`. When the log reports `CUDA error`, as the `cuda` build does on a driver without CUDA 13.5 support, run `narwhal dev down`, then `narwhal dev setup --accelerator cuda12` or `--accelerator none`. |
| `a fleet is already up in …` during `up` | Stop that fleet with the `narwhal dev down --instance …` command the message names, then repeat `up`. |
| `run narwhal dev down … before changing the instance's settings` | Run `narwhal dev down`, then repeat the `setup` command. |
| `no instance here; run narwhal dev up or narwhal dev init` | Run `narwhal dev up`, or change to the directory where you ran it. |
| `the fleet is already up` during `up` | The fleet is running. Use it, or run `narwhal dev down` to stop it. |
| `run this instance with the Python environment used by dev init` | Activate the virtual environment that ran `setup`. |

## Differences from a deployed fleet

The quick-start fleet gives each engine a profile from a few short requests. Its admission is `open`: the router queues a request that a deployed fleet's predictive admission would refuse with HTTP 429 because its predicted latency exceeds the targets.

The [deployment stages](../Deploy.md) add engine attestation, a profiling sweep and KV transfer evidence. [llama.cpp engines](Llama-Cpp-Engines.md) describes the engine settings, the KV handoff and the metrics.
