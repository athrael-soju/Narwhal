# `narwhal`

[Narwhal dev](../Dev-Runtime.md) runs the native NVIDIA CUDA backend on
Ubuntu or Ubuntu under WSL2. Its installed template starts two engines on a
selected NVIDIA GPU and targets 8 GB of VRAM or less. The optional
[RTX 5090 reference](../dev/RTX-5090-Reference.md) records a measured
four-engine configuration. Templates select the model, runtime and memory
budget; `gpu.product` pins a model of card when a recipe requires one.

`narwhal dev init` writes a private instance containing its model and runtime
pins, memory budget, unique ports, engine launch records and fleet config.
The installed template assigns one prefill and one decode role. Repeating
`init` preserves the existing files and operator edits,
returning `status: reused` when explicitly supplied settings match the saved
instance. Omitted settings retain their saved values. Conflicting flags exit
2 and name the settings that differ. To change initialization settings,
select a fresh directory with `narwhal dev init --instance runs/new-instance`
and supply the desired template and flags.

`up` checks the ports and runtime, starts each engine, captures live
attestations, profiles every split with at least one prefill and one decode
engine, and starts the router. It reports `launched`.
`verify` runs full preflight across every eligible directed KV path, checks
the profiles against current processes, sends an arithmetic request through
the router, and retains router and engine metrics before reporting `ready`.

`status` reports `launched` while owned processes pass HTTP health checks,
then reports `ready` after successful verification with current transfer
evidence. A failed `verify` saves its reason, evidence directory and
failure time in `lifecycle.json` and the attempt's `failure.json`. Subsequent
`status` calls report `degraded`, include that record as `verification_failure`
and add its reason to `problems` until a successful verification or completed
`down` resolves the failure. A verification retry retains the earlier failure
while its checks execute.

`down` checks the recorded boot ID and start ticks, then waits for the group's
workers through leader exit and escalates surviving owned processes to
SIGKILL. Successful teardown reports `stopped`; a subsequent `up` creates
another run directory with fresh profiles. Earlier logs and measurements
stay beside it.

In default text mode, lifecycle commands write their returned state as one JSON
document to stdout and send preparation progress, profiling progress and error
diagnostics to stderr. Use `narwhal dev up > result.json` to retain that state
while progress remains visible. `--format json` wraps the state in the versioned
command result and maps its status to the documented exit code.

In default text mode, commands exit 0 for `initialized`, `reused`, `starting`,
`launched`, `ready` or `stopped`, 2 for argument, instance configuration or
runtime package errors, and 1 for failed lifecycle operations or a `degraded`
status. A failed `verify` and subsequent `status` both exit 1 while that failure
is retained, including when HTTP checks pass. JSON mode returns 3 for a degraded
status and 4 for an operational error.

```bash
narwhal dev init --model /path/to/model.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Flag | Default | Operation |
| --- | --- | --- |
| `--version` | Root command | Run as `narwhal --version` to print the installed distribution version. |
| `--instance` | `runs/dev` | Select the private instance for any subcommand. |
| `--template` | Installed small-GPU template | Supply versioned model, tokenizer, runtime, profiling and memory settings to `init`. |
| `--model` | Pinned Hugging Face cache file | Select the GGUF file matching the template checksum. |
| `--model-dir` | Pinned tokenizer cache directory | Select tokenizer and configuration files. |
| `--gpu` | Single discovered GPU | Select a physical GPU UUID. |
| `--engine-count` | Template value, two | Allocate independent engine processes. |
| `--port-base` | Template ports: router 18000, engine 18101, attestation 18201, NIXL 5701 | `--port-base P` sets the router port to `P`, engine HTTP start to `P+1`, attestation start to `P+101` and NIXL start to `P+201`. All selected ports must be distinct and fit 1..65535. |
| `--gpu-memory-utilization` | Template value, 0.35 | Finite per-engine fraction of total GPU memory, greater than zero and at most 1. |
| `--device-allowance` | Template value, 0.8 | Finite fraction of total GPU memory, at most 1; bounds the sum of engine fractions and the aggregate observed startup memory increase. |
| `--interface` | `"eth0"` | Select the local NIXL/UCX interface. |

Initialization accepts two to eight engines and compares the decimal total of
their per-engine fractions with the device allowance: three engines at `0.1`
fit an allowance of `0.3`; a total above the allowance rejects initialization.
The free-memory check reserves the allowance times total device memory plus
the template's `gpu.reserve_mib` (512 MiB in the installed template).

Model and runtime changes belong in a custom template, selected with
`--template`. Runtime and tokenizer checksums bind the default template to
its pinned GGUF loader. Model overrides require their matching template
checksums and serving limits.

Export `NARWHAL_ENGINE_API_KEY` before `up` to authenticate engine requests.
The generated fleet references that environment variable for profiling,
verification and routing; keep it set when using those commands.

The RTX 5090 reference also contains `role_cycle`, with deterministic
workloads for 1P:3D, 2P:2D and 3P:1D. The checkout command
`python -m tools.measurement.dev_cycle --instance runs/dev` replays these
workloads through a verified fleet and saves transition and latency checks.
See [Replay all three role splits](../dev/RTX-5090-Reference.md#replay-all-three-role-splits)
for the workload order, output files and exit codes.

Each `run-*` directory contains the fleet used by the router, effective
commands, engine logs, cache layouts, attestations, measured profiles,
whole-device VRAM samples and request journal. A `verify-*` directory adds
the preflight log, directed transfer evidence, routed response and metrics.
On startup failure, inspect the named stage's log, repair the configuration
or runtime, and run `up` again after `down` confirms teardown.

| Output option | Default | Purpose |
| --- | --- | --- |
| `--format` | `"text"` | Select `json` for [versioned command results](../Command-Results.md). |
