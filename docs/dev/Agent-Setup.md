---
description: Checks, commands and expected results for a coding agent that sets up and exercises a Narwhal development fleet of llama.cpp engines.
---

# Setup for coding agents

This page gives the [quick start](Quick-Start.md) as steps a coding agent can run and check. Each step has a command, the result that means success, and what to do otherwise. Run the commands in one Linux shell, inside WSL2 on a Windows host, and stop at the first step whose check fails.

Every `narwhal dev` command prints one JSON document on stdout and its progress on stderr. With `--format json` it prints a [versioned command result](../Command-Results.md) whose `status` and exit code follow the same table for every command.

## 1. Check the host

```bash
uname -sm
python3 -c 'import sys; print(sys.version_info >= (3, 11, 4))'
df --output=avail -m . | tail -1
awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo
```

| Check | Expected |
| --- | --- |
| `uname -sm` | `Linux x86_64` |
| Python version | `True` |
| Free disk in MiB | At least `1500` |
| Available memory in MiB | At least `2400` for the default model on the CPU, or `1500` for `ERNIE-4.5-0.3B` |

On Windows, run the commands in a WSL2 Ubuntu shell. Keep the virtual environment and the instance directory on the Linux filesystem.

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

Expected: exit code `0` and `"status": "initialized"`, or `"reused"` when `runs/dev` already holds an instance. The result's `engine_build` is `cuda` on a host with an NVIDIA driver and `none` otherwise.

| Result | Action |
| --- | --- |
| Exit code `2`, `port … is unavailable` | Repeat with a new `--instance` and `--port-base` set to a free port that has four free ports after it. |
| Exit code `2`, `holds other files` | Repeat with a new `--instance` directory. |
| Exit code `2`, `host memory check failed` | Repeat with `--model-name ERNIE-4.5-0.3B` or `--engine-count 2` on a new `--instance`. |
| Exit code `2`, `SHA-256 … differs` | Repeat the command once. It downloads the file again. |
| A network error | Repeat the command. Verified files are kept. |

To use the CPU on a host with a GPU, add `--accelerator none`.

## 4. Start the fleet

```bash
narwhal dev up --instance runs/dev
```

Expected: exit code `0` and `"status": "launched"`, within a minute. The `router` field holds the router's URL, `http://127.0.0.1:18000` unless `--port-base` changed it.

| Result | Action |
| --- | --- |
| `process exited before … became healthy` | Read the last lines of `engine-1.log` in the directory the result's failure names. For a CUDA load error, set up a new instance with `--accelerator cuda12`, then `--accelerator none`. |
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

Expected: exit code `0`, `"status": "answered"`, a nonempty `reply`, and `engines.prefill` and `engines.decode` naming two different engines such as `n1` and `n3`.

## 7. Send concurrent prompts and read the fleet

```bash
narwhal dev ask --instance runs/dev --repeat 100 --concurrency 8 --max-tokens 64
narwhal dev watch --instance runs/dev --once
```

Expected from `ask`: exit code `0`, `"status": "completed"` and `"failed": 0`. Exit code `1` means at least one request failed, and `errors` lists up to five distinct messages.

Expected from `watch --once`: exit code `0`, `"status": "observed"`, four entries in `engines` with `"state": "live"`, and `served` at least `101`.

To see a role switch, send long replies and read `role_switches` afterwards:

```bash
narwhal dev ask --instance runs/dev --repeat 600 --concurrency 8 --max-tokens 200
narwhal dev watch --instance runs/dev --once
```

Expected: `role_switches` at least `1`.

## 8. Stop

```bash
narwhal dev down --instance runs/dev
```

Expected: exit code `0` and `"status": "stopped"`. `narwhal dev status --instance runs/dev` then also reports `stopped`.

## Exit codes

| Exit code | Meaning |
| :--: | --- |
| `0` | The command reached its status. |
| `1` | A failed `up`, `verify` or `down`, a `degraded` instance, or a failed `ask` request. |
| `2` | Invalid arguments, a failed `setup` or `init` check, or an unreadable instance. |

With `--format json` the codes follow [the command result statuses](../Command-Results.md).

## Using the router from other code

The router serves `POST /v1/completions` and `POST /v1/chat/completions` in the OpenAI format at the URL `up` printed. The `model` field is the template's served name, `Qwen3.5-0.8B` by default.
