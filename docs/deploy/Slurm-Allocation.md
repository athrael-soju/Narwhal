---
description: Start a Narwhal fleet inside one Slurm allocation with narwhal-slurm, from `srun` to a ready router, and set serving fields for load runs.
---

# Running Narwhal in a Slurm allocation

`narwhal-slurm` takes one Slurm allocation to a ready router. It starts the engines on every node, writes the fleet file, profiles every engine and starts the router on the head node. [`narwhal-slurm`](../cli/Slurm.md) lists every option.

## Prepare each node

Each node runs one `narwhal-slurm` task. The task needs Narwhal's environment and the engine's Python on the same node:

1. Install Narwhal in a virtual environment that every node can use.
2. Run each task where the engine runs, for example in a container from the engine image with Narwhal's environment mounted at the path it was created at.
3. Give `--engine-python` the engine's Python when it is not `python3` on the `PATH`. The task starts each engine with that Python, without Narwhal's environment.

Choose a run directory that every node reads and writes. Each allocation writes its files to `slurm-<job id>` inside it. The head node refuses a `slurm-<job id>` directory that already holds `fleet.json`.

## Start the allocation

Run one task on each node:

```bash
srun --ntasks-per-node=1 --kill-on-bad-exit=1 narwhal-slurm \
  --model <model-path> --served-model-name <served-name> \
  --run-dir <shared-dir> --slo-ttft-s 2 --slo-tpot-s 0.05 \
  --engines-per-node 8 --max-sequences 64
```

Each task reads its rank and the step's nodes from Slurm, and refuses to start unless the step runs one task on each node. Engines are numbered `n1`, `n2`, ... in node order. The first `--prefill` engines open as prefill and the rest as decode. By default, half the engines, rounded down, open as prefill.

Pass engine settings with `--engine-arg` and `--engine-env`, for example `--engine-arg=--max-model-len=32768` or `--engine-env UCX_NET_DEVICES=<device>`. With `--kill-on-bad-exit=1`, a failed stage on one node ends the tasks on every node.

## Follow the head node

The head node, rank 0, first prints `narwhal-slurm: allocation: job <id>, node 0 of <N> (head), run directory <path>`, then one `narwhal-slurm: engines: started <engine> at http://<address>:<port> on devices <devices> (pid <pid>)` line for each engine it starts. It then prints `narwhal-slurm: <stage>: ...` lines as it runs these stages:

1. `fleet` writes `fleet.json` with each engine's URL and opening role, the served model name, the SLOs, the backend and the paths of the profile store and router state, and prints `fleet: wrote <path> with <N> engines`.
2. `health` waits until every engine answers `/health`, up to `--health-timeout-s`, and prints `health: <N> engines answer /health`.
3. `profile` prints `profile: profiling every engine`, runs `narwhal-profile` on the fleet and prints its wall time as `profile: done in <N> seconds`. It writes `profiles.json` and `profiles.samples.json`. The profiler log is `logs/profile.log`.
4. `router` starts `narwhal-serve` with the fleet file and the journal `journal.jsonl`, and prints `router: started (pid <pid>)`. The router log is `logs/router.log`.
5. `ready` waits for the router's `/ready`, up to `--ready-timeout-s`, and prints `ready: the router serves at http://<address>:<port>`, with the head node's IPv4 address and `--router-port`.

Send requests to that URL once the `ready` line appears. A failed stage prints `narwhal-slurm: <stage>: <detail>` to standard error. Each engine logs to `logs/<engine>.log`, such as `logs/n1.log`, in the allocation's directory.

## Shorten profiling

Every allocation profiles its engines again, because profiles bind to each engine's process start time. To pass `narwhal-profile` options, use `--profile-arg`, for example a shorter sweep:

```bash
--profile-arg=--prefill-lens=256,1300,4096,8300 --profile-arg=--decode-input-lens=512,4096
```

With `--max-sequences`, the head node writes `profiling-limits.json` and passes it to `narwhal-profile --limits`, so the decode cohorts stay within each engine's sequence limit. [Profiling](../measure/01-Profile.md) defines the sweep and the fit checks.

## Set serving fields for a load run

The generated fleet file uses Narwhal's serving defaults. To set another fleet field, pass `--fleet-setting PATH=VALUE`, repeatable. `VALUE` is read as JSON, or else as a string. [`narwhal-slurm`](../cli/Slurm.md#router-and-run-files) lists the fields the launch writes, which `--fleet-setting` cannot set:

```bash
--fleet-setting serving.admission=open --fleet-setting serving.max_connections=512
```

[Settings for a load run](../operate/07-Admission-Queue-and-Retry-Settings.md#settings-for-a-load-run) gives the admission mode and in-flight limit that a fixed-concurrency client needs.

## Stop the launch

Send SIGTERM, SIGINT or SIGHUP to each task. Each node stops its profiler and router, then each engine it started, and exits `0`.
