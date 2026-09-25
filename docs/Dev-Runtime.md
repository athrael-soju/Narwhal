# Set up the WSL2 GPU runtime

Use Ubuntu under WSL2, Python 3.12 and an RTX 5090 with 32 GB VRAM.
Install the NVIDIA Windows driver and run these commands in the WSL2 shell:

```bash
nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv
uname -r
```

Keep the model, virtual environment and instance directory on the WSL2
Linux filesystem. The default allocation uses four engines, a 4,096-token
context limit, four active sequences per engine and a 0.1 vLLM memory
fraction. Launch checks reserve another 2,048 MiB of free VRAM and enforce
an aggregate device allowance of 0.5.

## Install the runtime

From the Narwhal checkout:

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install .
python -m pip install 'vllm==0.29.0' 'torch==2.13.0' \
  'transformers==5.17.0' 'nixl==1.4.1' 'nixl-cu13==1.4.1'
python -m pip install \
  'https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl'
```

Apply the pinned GGUF loader sources over the wheel's Python files,
keeping its CUDA extension:

```bash
mkdir -p runs
git clone https://github.com/vllm-project/vllm-gguf-plugin.git runs/gguf-plugin
git -C runs/gguf-plugin checkout d4c1f0d082fc7cd4350da56689109a01c1f29d6c
python - <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import shutil

source = Path('runs/gguf-plugin/vllm_gguf_plugin')
target = Path(distribution('vllm-gguf-plugin').locate_file('vllm_gguf_plugin'))
for path in source.rglob('*.py'):
    destination = target / path.relative_to(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
PY
```

`dev init` checks the plugin's Python tree and CUDA extension hashes against
the installed template. Reapply these pinned sources after reinstalling the
plugin wheel.

## Download the model and tokenizer

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The default template resolves these revisions in the Hugging Face cache.
For a custom cache location, pass the GGUF file with `--model` and the
configuration/tokenizer directory with `--model-dir`.

## Launch and verify

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
```

`up` starts and profiles the engines, then starts the router and reports
`launched`. `verify` runs all 12 eligible directed KV transfers, checks the
current engine profiles, and sends a routed arithmetic request before
reporting `ready`.

Keep this virtual environment active for every lifecycle command. The
instance records its interpreter and requires the same runtime on restart.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. Select
another port layout with `dev init --port-base`, or another instance with
`--instance` on each command.

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

Expect the response content `5`.

## Connect Prometheus and Grafana

Follow [Monitor a WSL2 development fleet](observability/04-WSL2.md) to connect
the local metrics endpoints to the homelab's canonical monitoring stack.
Check all five scrape targets and the Narwhal Orchestrator dashboard before
running workloads.

## Replay all three role splits

The installed template's `role_cycle` section fixes the token pool, random
seeds and workload order. From the matching Narwhal checkout, with the
instance's virtual environment active:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

Start from a verified 2P:2D fleet. The runner lets the 30-second demand
window expire, then sends one warmup request before each phase:

| Phase | Input / output tokens | Requests | Requests/s | Maximum in flight |
| --- | --- | --- | --- | --- |
| Decode | 256 / 128 | 24 | 0.5 | 8 |
| Prefill steady | 3,840 / 1 | 35 | 1 | 8 |
| Prefill burst | 3,840 / 1 | 12 | 100 | 12 |

Narwhal chooses roles from the current profiles and resident work throughout
the sequence. The runner checks controller-selected
**2P:2D → 1P:3D → 2P:2D → 3P:1D → 2P:2D** transitions and requires every
steady-phase request to meet the template's TTFT and TPOT budgets. The burst
accepts completed requests and TTFT-budget HTTP 429 responses, and records
its latency attainment separately. Engine profiles and concurrent GPU work
can change the resulting transitions and latency.

Allow about two minutes for the workload sequence after `up` and `verify`.
Each replay creates `cycle-*` beneath the instance, or a fresh directory
selected with `--out`. Its `summary.json` contains the observed splits,
per-phase latency, acceptance result and `grafana_range` timestamps for the
dashboard's `from` and `to` URL parameters. The directory also preserves
the template, effective fleet, source hashes, request rows and router state.
Exit code 0 means the cycle and steady-phase budgets passed; 2 means a
completed replay failed those checks; 1 means setup or execution failed.
Use `dev down` after inspecting the replay to release GPU memory.

Existing instances retain their template across `init` calls. Create a new
instance from the current reference when upgrading to this recipe.

## Inspect roles and operating limits

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

The four engines open with two prefill and two decode roles. Startup
profiles 1P:3D, 2P:2D and 3P:1D so the controller can price changes in both
directions from the current processes. Prefill and decode sweeps cover
128 to 3,840 input tokens, with decode concurrency one and two and up to
128 output tokens. Use long inputs with short outputs to exercise prefill
growth, and longer outputs to exercise decode growth. The controller prices
each move from the profiles and resident work. The 4,096-token engine context
limit bounds input plus output.

The reference uses a 1-second TTFT budget and a 125-ms TPOT budget. Narwhal
samples engines every 100 ms and evaluates role changes every 250 ms, using
a 30-second demand window and three confirmations for ordinary moves. After
`verify`, fill one demand window with representative traffic before
assessing role changes.

Keep four engines for the default RTX 5090 setup. Changing engine count,
model, context length or memory fractions requires a matching template and
a fresh `up` and `verify` cycle. Export the installed reference to edit it:

```bash
python - <<'PY' > runs/dev-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PY
narwhal dev init --instance runs/dev-custom --template runs/dev-template.json
```

## Diagnose startup and inference

Inspect the current run path printed by `status`:

| File | Contents |
| --- | --- |
| `engine-*/startup.log` | Model loading, cache allocation and engine requests. |
| `profile-*.log` | Probe failures and profile fit errors. |
| `router.log` | Router startup and request errors. |
| `journal.jsonl` | Admission, placement and controller decisions. |
| `verify-*/preflight.log` | Runtime, profile and directed transfer checks. |
| `*-memory.jsonl` | Whole-device VRAM samples. |
| `teardown.json` | Stopped process groups and cleanup errors. |

Free conflicting ports or select another `--port-base` in a new instance.
For a VRAM reserve failure, stop other GPU workloads before retrying. For
a model or plugin hash mismatch, restore the pinned files above. After
repairing a startup failure, run `down`, `up` and `verify` for that instance.

## Stop or restart

```bash
narwhal dev down
narwhal dev status
```

`down` waits for the recorded process groups to exit before reporting
`stopped`, escalating owned workers that survive SIGTERM to SIGKILL and
preserving the instance logs. If a leader exited before teardown could
establish worker ownership, it reports surviving group members as
`degraded`. Inspect the PIDs in `teardown.json`, stop workers confirmed to
belong to this instance, then repeat `down`.

Run `up` and `verify` again after changing the runtime or restarting an
engine so profiles and transfer checks bind to the new processes.

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
