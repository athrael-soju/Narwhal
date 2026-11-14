---
description: Checks, commands and expected results for a coding agent that sets up and exercises a Narwhal development fleet of llama.cpp engines.
---

# Setup for coding agents

Each step of the [quick start](Quick-Start.md) appears here with its command, expected result and recovery action. Run the commands in one Linux shell and one working directory. On Windows, use a WSL2 Ubuntu shell. Stop at the first step whose check fails after its recovery action.

A successful `narwhal dev` command prints one JSON document on stdout and its progress on stderr. A failed one prints `narwhal: dev <action> <instance>: <message>` on stderr and nothing on stdout. `watch` prints JSON only with `--once`. With `--format json` a command prints a [versioned command result](../Command-Results.md) whose `status` and exit code follow the same table for every command.

## 1. Check the host

```bash
uname -sm
python3 -c 'import sys; print(sys.version_info >= (3, 11, 4))'
python3 -c 'import ensurepip' && echo venv-ok
df --output=avail -m . | tail -1
awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo
```

| Check | Expected |
| --- | --- |
| `uname -sm` | `Linux x86_64` |
| Python version | `True` |
| `venv` support | `venv-ok`. Otherwise run `sudo apt install python3-venv`. |
| Free disk in MiB | At least `1500` for the CPU build, or `4000` for the CUDA 12 build |
| Available memory in MiB | At least `2800` for the default model with the CPU build, `1800` for `ERNIE-4.5-0.3B`, or half of either with `--engine-count 2`. The GPU build skips this check. |

Under WSL2, keep the virtual environment and the instance directory on the Linux filesystem. A host with Python older than 3.11.4 needs a newer Python before step 2.

## 2. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install narwhal-inference
narwhal --version
```

Expected: `narwhal-inference <version>` and exit code `0`. Keep the virtual environment active for every later command.

## 3. Set up the instance

```bash
narwhal dev setup --instance runs/dev
```

Expected: exit code `0` and `"status": "initialized"`, or `"reused"` when `runs/dev` already holds an instance. An `initialized` result's `engine_build` is `cuda12` on a host with an NVIDIA driver and `none` otherwise.

| Result | Action |
| --- | --- |
| Exit code `2`, `port … is unavailable` | Repeat with the same `--instance` and `--port-base` set to a free port that has four free ports after it. The downloaded files are kept. |
| Exit code `2`, `holds other files` | Repeat with a new `--instance` directory. |
| Exit code `2`, `host memory check failed` | Repeat with `--engine-count 2`. To change the model, use a new `--instance` with `--model-name ERNIE-4.5-0.3B`. |
| Exit code `2`, `SHA-256 … differs` | Repeat the command once. It downloads the file again. |
| A network error | Repeat the command. Verified files are kept. |

To use the CPU on a host with a GPU, add `--accelerator none`.

## 4. Start the fleet

```bash
narwhal dev up --instance runs/dev
```

Expected: exit code `0` and `"status": "launched"`, usually within seconds, and about a minute for the first start of a CUDA build. Allow five minutes. The `router` field holds the router's URL, `http://127.0.0.1:18000` unless `--port-base` changed it.

| Result | Action |
| --- | --- |
| `process exited before http://127.0.0.1:18101/health became healthy` | Run `narwhal dev status --instance runs/dev` and read the last lines of `engine-N.log` in the directory its `run` field names, where N is the port in the message less the first engine port plus one. For a `CUDA error` in the log, set up a new instance with `--accelerator none`. |
| `run dev down before starting another generation` | Run `narwhal dev down --instance runs/dev`, then repeat. |

## 5. Verify

```bash
narwhal dev verify --instance runs/dev
```

Expected: exit code `0`, `"status": "ready"`, an empty `problems` list and a nonempty `reply`.

## 6. Send a prompt

```bash
narwhal dev ask --instance runs/dev "What is the capital of France?"
```

Expected: exit code `0`, `"status": "answered"`, a nonempty `reply`, and `engines.prefill` and `engines.decode` each naming an engine, usually two different ones such as `n1` and `n3`.

## 7. Send concurrent prompts and read the fleet

```bash
narwhal dev ask --instance runs/dev --repeat 100 --concurrency 8 --max-tokens 64
narwhal dev watch --instance runs/dev --once
```

Expected from `ask`: exit code `0`, `"status": "completed"` and `"failed": 0`. Exit code `1` means at least one request failed, and `errors` lists up to five distinct messages.

Expected from `watch --once`: exit code `0`, `"status": "observed"`, one entry in `engines` for each engine, each with `"state": "live"`, and `served` at least `101`.

To see a role switch, send long replies and read `role_switches` afterwards:

```bash
narwhal dev ask --instance runs/dev --repeat 600 --concurrency 8 --max-tokens 200
narwhal dev watch --instance runs/dev --once
```

Expected on most hosts: `role_switches` at least `1`. The run took about three minutes on the measured host, so give the command a ten-minute limit. A result of `0` is not a failure.

## 8. Stop

```bash
narwhal dev down --instance runs/dev
```

Expected: exit code `0` and `"status": "stopped"`. `narwhal dev status --instance runs/dev` then also reports `stopped`.

## Exit codes

| Exit code | Meaning |
| :--: | --- |
| `0` | The command succeeded. |
| `1` | A failed `up`, `verify`, `status`, `watch` or `down`, a `degraded` instance, a failed `ask` request, or `ask` or `watch` on a stopped instance. |
| `2` | Invalid arguments, a failed `setup` or `init` check, an unreadable instance, or a Python environment other than the one that ran `setup`. |

With `--format json` the codes follow [the command result statuses](../Command-Results.md).

## OpenAI-compatible routes

The router serves `POST /v1/completions` and `POST /v1/chat/completions` in the OpenAI format at the URL `up` printed. The `model` field is the template's served name, `Qwen3.5-0.8B` by default. `ask` adds `"chat_template_kwargs": {"enable_thinking": false}` to its requests for that model, and a chat request without it returns the model's reasoning before its reply.
