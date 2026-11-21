---
description: Start engines on every node of a Slurm allocation, then profile them and start the router on the head node, with narwhal-slurm.
---

# `narwhal-slurm`

`narwhal-slurm` starts a Narwhal fleet inside one Slurm allocation. Run it as one task per node:

```bash
srun --ntasks-per-node=1 --kill-on-bad-exit=1 narwhal-slurm \
  --model <model-path> --served-model-name <served-name> \
  --run-dir <shared-dir> --slo-ttft-s 2 --slo-tpot-s 0.05
```

Each task reads its node's rank from `SLURM_NODEID` and the step's node names from `SLURM_STEP_NODELIST`, or from `SLURM_JOB_NODELIST` when the step variable is unset, and resolves each node name to its IPv4 address. The task refuses to start unless the step runs one task on each node.

Every node starts its engines with the engine image's Python, outside Narwhal's own environment. The head node, rank 0, then runs these stages:

1. `fleet`: writes the fleet file with each engine's literal URL.
2. `health`: waits for `/health` 200 from every engine.
3. `profile`: runs `narwhal-profile` on the fleet.
4. `router`: starts `narwhal-serve`.
5. `ready`: waits for the router's `/ready` 200, then prints the router URL.

The other nodes keep their engines running until the launch stops. A failed stage ends that node's task with exit code `1` and prints `narwhal-slurm: <stage>: <detail>` to standard error. With `--kill-on-bad-exit=1`, `srun` then ends the task on every node.

SIGTERM, SIGINT or SIGHUP stops the launch. Each node stops its profiler and router first, then each engine it started, and exits `0`.

## Engines

Engines are numbered `n1`, `n2`, ... in node order, then by their index on the node. The engine at index `i` on a node listens on `--engine-port` plus `i` and uses `--transfer-port` plus `i` for its KV side channel. The first `--prefill` engines open as prefill and the rest as decode.

Each engine takes `--tensor-parallel` devices from the list in the `--device-env` variable, in order, and the launch sets that variable for the engine. Without the variable, the node's devices are numbered from `0`. A vLLM engine runs `python3 -m vllm.entrypoints.openai.api_server` with the NIXL connector in `kv_both` mode, and its NIXL side channel binds the node address.

| Option | Default | Description |
| --- | --- | --- |
| `--model PATH` | required | Model path or name the engines load. |
| `--served-model-name NAME` | required | Model name clients request. |
| `--backend NAME` | `vllm` | Engine backend. |
| `--engines-per-node N` | `1` | Engines on each node. |
| `--tensor-parallel N` | `1` | Devices for each engine. |
| `--prefill N` | half of the engines | Engines that open as prefill. |
| `--engine-port PORT` | `8100` | Port of the first engine on each node. |
| `--transfer-port PORT` | `5600` | KV side-channel port of the first engine on each node. |
| `--max-sequences N` | engine default | Each engine's sequence limit. The profiler keeps its decode cohorts within it. |
| `--engine-arg ARG` | none | Extra engine argument, repeatable. Write an option with its value as `--engine-arg=--max-model-len=32768`. |
| `--engine-env NAME=VALUE` | none | Engine environment variable, repeatable, such as `UCX_TLS` or `UCX_NET_DEVICES`. |
| `--engine-python PATH` | `python3` | The engine image's Python, found on the `PATH` without Narwhal's environment. |
| `--device-env NAME` | `CUDA_VISIBLE_DEVICES` | Variable that lists a node's devices and selects each engine's devices. |

## Router and run files

| Option | Default | Description |
| --- | --- | --- |
| `--run-dir PATH` | required | Directory every node reads and writes. Each allocation uses `slurm-<job id>` inside it. |
| `--slo-ttft-s SECONDS` | required | Fleet time-to-first-token SLO. |
| `--slo-tpot-s SECONDS` | required | Fleet time-per-output-token SLO. |
| `--router-host HOST` | `0.0.0.0` | Router bind address. |
| `--router-port PORT` | `8000` | Router port. |
| `--profile-arg ARG` | none | Extra `narwhal-profile` argument, repeatable. |
| `--fleet-setting PATH=VALUE` | none | Fleet field by dotted path, repeatable, such as `serving.admission=open`. `VALUE` is read as JSON, or else as a string. |
| `--health-timeout-s SECONDS` | `1800` | Time limit for every engine to answer `/health`. |
| `--ready-timeout-s SECONDS` | `600` | Time limit for the router's `/ready`. |
| `--stop-grace-s SECONDS` | `30` | Time each process has to exit after SIGTERM before SIGKILL. |
| `--version` | | Print the installed distribution version. |

The head node writes these files to `<run-dir>/slurm-<job id>/`:

| File | Contents |
| --- | --- |
| `fleet.json` | Fleet file with each engine's URL and opening role. |
| `profiling-limits.json` | Each engine's `--max-sequences`, passed to `narwhal-profile --limits`. Written only with `--max-sequences`. |
| `profiles.json`, `profiles.samples.json` | The profile store and its samples. |
| `journal.jsonl` | Router journal. |
| `router-state.json` | Router recovery state. |
| `logs/` | One log for each engine, and `profile.log` and `router.log`. |

`--fleet-setting` cannot set a field the launch writes: `schema`, `schema_version`, `model`, `engines`, `slo.ttft_s`, `slo.tpot_s`, `engine.backend`, `profiles.path` or `recovery.state_path`. The `fleet` stage fails when the fleet file does not load, and names the field.

The launch refuses a run directory that already holds `fleet.json`. Profiles bind to each engine's process start time, so every allocation profiles its engines again.

## Exit codes

| Exit code | Outcome |
| :-------: | --- |
| `0` | The launch stopped on a signal. |
| `1` | A stage failed. The message names the stage. |
| `2` | Invalid arguments, or the task does not run in a Slurm allocation with one task per node. |
