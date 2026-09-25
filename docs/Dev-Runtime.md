# Narwhal dev

Narwhal dev starts, profiles, verifies and stops independent engines sharing
one GPU. The current native backend uses NVIDIA CUDA on Ubuntu or Ubuntu under
WSL2. Each instance keeps its model, runtime, GPU allocation, ports, profiles
and process identities under `runs/dev` by default.

The installed template starts two engines, one prefill and one decode, with
Qwen3.5-0.8B GGUF. It selects a CUDA GPU by UUID and checks available VRAM
against its allocation and 512 MiB reserve. The target is an NVIDIA GPU with
**8 GB of VRAM or less**; a successful `up` and `verify` establishes fit and
directed KV transfer on the selected card. The separate
[RTX 5090 reference](dev/RTX-5090-Reference.md) records the measured
four-engine configuration.

## Prepare Ubuntu or WSL2

On native Ubuntu, use an NVIDIA driver compatible with the selected CUDA
runtime. On WSL2, install the NVIDIA driver on Windows and run Ubuntu inside
WSL2. [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/)
describes the Windows driver setup. Keep the checkout, model, virtual
environment and instance directory on the Linux filesystem in either case.

In the Ubuntu shell, inspect the available GPU and IPv4 interfaces:

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

WSL2 exposes `nvidia-smi` at `/usr/lib/wsl/lib/nvidia-smi` when that path is
outside the shell's `PATH`. Choose an interface with one IPv4 address for
NIXL/UCX; its name may differ from the default `eth0`. The commands below
run in the Ubuntu shell on both hosts.

## Install the runtime and model

Follow the [CUDA runtime and model steps](dev/CUDA-Runtime.md) in the Ubuntu
shell. The installed template pins vLLM, Torch, NIXL, Transformers, the GGUF
loader, model and tokenizer. CUDA support currently covers NVIDIA GPUs;
contributions can add discovery, launch and transfer support for other
vendors.

The default two-engine allocation assigns each vLLM process 0.35 of total
VRAM, caps whole-device startup growth at 0.8 and reserves 512 MiB of free
memory at initialization. Model weights, runtime overhead and KV cache
consume that budget. Tune `--gpu-memory-utilization` and `--device-allowance`
for a fresh instance after checking the card's available memory. Its initial
development budgets are 5 seconds TTFT and 500 ms TPOT; set targets from
measurements on the selected card. A custom
`--template` can change the model, runtime, context length, profiling sweep
and reserve. A recipe can set `gpu.product` to pin a card and
`gpu.minimum_total_mib` to require a measured capacity.

Export the installed template when preparing a new recipe:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/small-cuda-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
PYTHON
```

Edit the file, then initialize a fresh instance with
`narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom`.

## Initialize and verify an instance

Select the IPv4 interface found above:

```bash
interface=eth0  # replace with the interface reported by ip
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

`init` writes the private instance after checking the model, runtime and GPU
allocation. `up` checks ports, starts and profiles the engines, captures
attestations and starts the router. `verify` runs preflight across the
eligible directed KV paths, sends a routed arithmetic request and retains
the response and metrics before reporting `ready`. Keep the same Python
environment active for subsequent lifecycle commands; the instance records
its interpreter.

Lifecycle results are one JSON document on stdout. Preparation, profiling
and error diagnostics go to stderr. `status` reports `degraded` with the
retained reason when verification fails; a successful `verify` restores
`ready` after the failing input is repaired. Select `--format json` for
[versioned command results and automation exit codes](Command-Results.md).

The default router listens at `http://127.0.0.1:18000`. Inspect its state
and metrics after verification:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

Select another port layout with `narwhal dev init --port-base`, or another
instance with `--instance` on each command. The [CLI reference](cli/Dev.md)
lists the flags, lifecycle results and exit codes. The optional
[four-engine role cycle](dev/RTX-5090-Reference.md#replay-all-three-role-splits)
uses the RTX 5090 reference's workload and split targets.

## Inspect and stop the instance

`status` prints the current run directory. Its `engine-*/startup.log` files
record model loading and engine requests, `profile-*.log` files record probe
and fit outcomes, and `verify-*/preflight.log` records the runtime, profile
and transfer checks. The run also retains the routed response, memory
samples, request journal and `teardown.json`.

```bash
narwhal dev down
narwhal dev status
```

`down` stops process groups bound to the recorded boot ID and start ticks,
then reports `stopped`. A later `up` creates a fresh run directory and
profiles the new engine processes. On WSL2, the optional
[monitoring example](observability/04-WSL2.md) forwards the local metrics to
a separate Prometheus and Grafana host.

After a runtime change or engine restart, stop the generation and repeat
`up` and `verify` so profiles and transfer checks bind to the new processes.

## Stage deadlines and recovery

`dev up` and `dev verify` run runtime checks, native shared startup, attestation,
profiling and preflight as separate subprocess stages. Each stage gets 300 seconds
of execution by default. `NARWHAL_STAGE_TIMEOUT_SECONDS` overrides that default;
`NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS` overrides one stage, with its name converted
to uppercase and punctuation replaced by underscores. For example:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=720 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

`*.command.json` identifies each dev stage. The command writes partial output to
private `*.stdout` and `*.stderr` artifacts and records its budget, wall-clock
start, elapsed time, exit status and process identities in `*.stage.json`.
The enclosing budget includes helper imports, subprocess startup and execution;
each engine health check retains its own 180-second limit inside native shared
startup. Profile splits each receive a fresh stage budget. HTTP health requests
and the routed verification request retain their existing request deadlines.

On expiry, SIGINT or SIGTERM during a helper stage, the controller signals owned
processes with SIGTERM, waits up to `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS` (10 seconds),
then uses SIGKILL and waits up to `NARWHAL_STAGE_KILL_GRACE_SECONDS` (5 seconds).
These periods follow the execution budget; stage evidence records escalation
and any surviving owned PIDs.

Startup rollback records the initiating failure alongside teardown errors in
`lifecycle.json`. Verification failures retain the attempt directory and keep
status degraded until a successful verification. Inspect the failed stage's
output, run `narwhal dev status --instance PATH`, then use `narwhal dev down
--instance PATH` to stop the generation before retrying startup. A surviving
process with expired ownership requires operator inspection of its recorded
boot ID, start tick and process group. All evidence stays under the instance.

The engine deployment wrapper uses these budgets for Docker clients and native
runtime checks. Each Docker create/run carries the launch token from
`docker-owner.json` in `io.narwhal.launch` and a unique operation token in
`io.narwhal.operation`. A timed out or cancelled client triggers daemon inspection
with a separate `NARWHAL_DOCKER_RECONCILE_SECONDS` budget (30 seconds).
Reconciliation removes containers created by the interrupted operation, or the
explicit target of an interrupted start, then queries daemon state again. It
retains the launch directory's other containers and records their IDs as
preserved resources. Each reconciliation client shares the remaining
reconciliation budget and receives the same termination grace periods, so final
client cleanup can extend reconciliation by 15 seconds with defaults.

`docker-reconcile-*.json` records the operation token, targeted, removed, preserved
and surviving IDs, refusal decisions,
inspection errors and observation time. A stalled daemon leaves the result at
`inspection_required`; inspect the persisted ownership label and recorded IDs
before retrying. The report describes the daemon at observation time: repeat
inspection after daemon recovery to catch a create operation that completed after
client cancellation. Keep the original launch directory through that inspection.

### Reference GPU timeout check

Run this qualification on the reference GPU with the pinned template and an
operator-selected instance. Retain a successful generation's startup logs and
`native-start-shared.log.*.stage.json`, then run `dev down` and record idle device
memory, process IDs and the configured engine, attestation and NIXL ports.
Use those timestamps to select a native-start budget after the first engine's
CUDA allocation and before the complete shared startup finishes:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS="$STARTUP_BUDGET_SECONDS" \
  narwhal dev up --instance runs/dev-timeout
narwhal dev status --instance runs/dev-timeout
narwhal dev down --instance runs/dev-timeout
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

Record the observed allocation before expiry, command duration, worker exit,
post-cleanup device memory and port release. Compare duration against the stage
budget plus termination grace and rollback time, and compare memory against the
idle baseline. Retain `lifecycle.json`, teardown, startup, memory and stage
artifacts. Confirm an unrelated process survives, then start and verify a fresh
generation with the normal budget. This opt-in qualification exercises vLLM
workers and CUDA cleanup; the automated deadline suite uses CPU helpers and a
fake Docker daemon executable.

## Controller interruption

The Linux recovery suite starts synthetic services and subprocess helpers,
interrupts the controller at synchronised barriers, then executes `status` and
`down` in fresh processes. Services create workers that delay termination;
helpers create workers in separate sessions. The suite checks complete committed
JSON documents, lock release, retained output, idempotent teardown and the
survival of an unrelated process across these twelve cases:

| Interruption barrier | Signals exercised | Controller cleanup and fresh `down` |
| --- | --- | --- |
| Child created, before identity capture | SIGINT | Startup stops the child tree and retains spawn cleanup evidence; `down` reports stopped. |
| Child created, before identity capture | SIGKILL | Recovery uses the last committed ownership set; the operator identifies the newly created service from its command and log. |
| Ownership document awaiting atomic replacement | SIGKILL | Readers see the previous complete document; operator inspection covers the service described by the pending write. |
| Ownership committed | SIGINT, SIGTERM | SIGINT rolls startup back; SIGTERM ends the controller and fresh `down` terminates the recorded group. |
| Service readiness wait | SIGKILL | Fresh `status` reports degraded and `down` terminates the recorded group. |
| Profiling helper active | SIGINT, SIGTERM, SIGKILL | SIGINT/SIGTERM stop the helper and roll startup back; after SIGKILL, fresh `down` recovers the helper and service records. |
| Verification helper active | SIGTERM, SIGKILL | SIGTERM records degraded verification and retains services; SIGKILL leaves interrupted helper evidence. Fresh `down` terminates the helpers and services. |
| Recorded service leader exited, delayed worker surviving | SIGKILL | Status lists the surviving group; teardown requests operator inspection of the worker's identity. |

Each helper runs beneath a dedicated Linux subreaper that holds its process
identity until cleanup finishes. A helper that forks, starts another session
and exits immediately leaves its worker attached to that supervisor. The
controller signals workers before removing the supervisor, then reaps its
adopted children. Successful native shared startup releases the supervisor
explicitly so the launched engines continue into attestation and profiling.
Release failures take the bounded cleanup path and retain their initiating error.

Stage documents retain the boot ID and process start ticks, updating atomically
when the controller observes another descendant. After abrupt controller death,
`status` lists matching interrupted helpers in `stage_processes`; `down` uses
those records and the still-live supervisor to discover and terminate its tree.
A mismatched boot or start tick protects a replacement process from signalling.
Recovery updates the stage record to `recovered`, or `recovery_required` with the
surviving PIDs, and preserves stdout, stderr and command evidence.

Ownership begins with a committed process or stage record. A SIGKILL before that
commit can leave a service whose command log predates its ownership entry.
Inspect the log, kernel start ticks and listening ports, stop the confirmed
process tree, then repeat `down`. Its stopped status covers the persisted
ownership set. A service leader that exits before teardown establishes worker
ownership also requires operator inspection; the status and teardown documents
identify the surviving group. The dedicated helper supervisor covers helper
leader exit while that supervisor remains alive.

### Reference GPU interruption check

Use an operator-selected instance with the pinned runtime and a completed normal
`up`/`verify` cycle. Retain its generation, device-memory baseline, process tree
and engine, attestation and NIXL port assignments. Qualify these two interruption
points on that host:

1. Start a fresh generation, wait for a committed engine identity and an observed
   CUDA allocation, then send SIGINT to the `narwhal dev up` controller PID.
   Record the controller's exit, retained startup failure, worker exits, device
   memory and port state; run `status` and `down` from a fresh shell.
2. Start and verify a fresh generation, launch another `dev verify`, wait for its
   preflight stage ownership record, then send SIGKILL to that controller PID.
   Record `status`, run `down` twice, and compare surviving process identities,
   device memory and bound ports against the baseline.

Keep a separately identified process alive during both checks and confirm its
identity after teardown. Retain controller output, `lifecycle.json`, stage and
teardown documents, vLLM logs, GPU process listings and timestamped memory/port
observations. A surviving CUDA allocation or occupied engine port identifies the
PID to investigate before another generation starts. Record the vLLM, driver,
CUDA and GPU versions alongside the exercised signal and barrier. The automated
suite qualifies Linux ownership using CPU processes and substituted HTTP health
responses; this opt-in procedure adds actual vLLM, CUDA and port-release evidence.

## Contribute another GPU recipe

For another small CUDA GPU, record the GPU, driver, model and runtime
versions, free-memory reserve, peak startup memory, both directed KV
transfers and routed completion. Share its working template and qualification
evidence so the allocation can be reproduced.

Contributions for other GPU vendors can add discovery, memory accounting,
engine launch and a compatible transfer runtime alongside a measured
template. [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
describes the checks and pull request workflow.
