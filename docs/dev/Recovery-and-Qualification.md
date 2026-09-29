# Narwhal dev recovery and qualification

This page covers recovery after an interrupted `narwhal dev` process or Docker
command, and the optional GPU checks.

[Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery)
lists the stage budgets and the procedure after a failed stage.

## Docker command reconciliation

The engine deployment wrapper applies the stage budgets to Docker clients and
native runtime checks.

Each Docker create or run carries two labels:

| Label                  | Value                                   |
| ---------------------- | --------------------------------------- |
| `io.narwhal.launch`    | Launch token from `docker-owner.json`   |
| `io.narwhal.operation` | Unique operation token                  |

After a Docker client timeout or cancellation, the wrapper inspects the daemon
within `NARWHAL_DOCKER_RECONCILE_SECONDS` (30 seconds). Reconciliation:

- removes the containers that the interrupted operation created, or the
  explicit target of an interrupted start;
- queries daemon state again;
- keeps the launch directory's other containers;
- records their IDs as preserved resources.

Reconciliation clients share the remaining budget and the same termination
grace periods. Final cleanup can add 15 seconds at default settings.

Each reconciliation writes a `docker-reconcile-*.json` report with:

- the operation token;
- the targeted, removed, preserved, and surviving IDs;
- refusal decisions;
- inspection errors;
- the observation time.

If the daemon stalls, the report result is `inspection_required`:

1. Inspect the persisted launch-token label and the recorded IDs before a retry.
2. After the daemon recovers, inspect it for creates completed after cancellation.
3. Keep the original launch directory until step 2 completes.

## Interrupted `narwhal dev` process

The Linux recovery suite covers the twelve barrier and signal combinations
below. Each combination checks for:

- complete committed JSON documents;
- released locks;
- retained output;
- idempotent teardown;
- survival of an unrelated process.

| Interruption barrier | Signals exercised | Cleanup and fresh `down` |
| --- | --- | --- |
| Child created, before identity capture | SIGINT | Startup stops the child tree and retains spawn cleanup evidence; `down` reports stopped. |
| Child created, before identity capture | SIGKILL | Recovery uses the last committed process set; the operator identifies the newly created service from its command and log. |
| Process record awaiting atomic replacement | SIGKILL | Readers see the previous complete document; operator inspection covers the service described by the pending write. |
| Process record committed | SIGINT, SIGTERM | SIGINT rolls startup back; SIGTERM ends the `narwhal dev` process, and a fresh `down` terminates the recorded group. |
| Service readiness wait | SIGKILL | A fresh `status` reports degraded, and `down` terminates the recorded group. |
| Profiling helper active | SIGINT, SIGTERM, SIGKILL | SIGINT and SIGTERM stop the helper and roll startup back; after SIGKILL, a fresh `down` recovers the helper and service records. |
| Verification helper active | SIGTERM, SIGKILL | SIGTERM records degraded verification and retains services; SIGKILL leaves interrupted helper evidence. A fresh `down` terminates the helpers and services. |
| Recorded service leader exited, delayed worker surviving | SIGKILL | `status` lists the surviving group; teardown requests operator inspection of the worker's identity. |

Each helper runs beneath a dedicated Linux subreaper, the helper supervisor.
The supervisor adopts the workers of exited helpers and runs until cleanup
finishes.

If the supervisor release after native shared startup fails, `narwhal dev`
takes the bounded cleanup path and retains the initiating error.

Stage documents record each observed descendant's boot ID and process start
ticks. If the `narwhal dev` process dies abruptly, the commands behave as
follows:

| Command  | Behavior                                                                                      |
| -------- | --------------------------------------------------------------------------------------------- |
| `status` | Lists matching interrupted helpers in `stage_processes`.                                      |
| `down`   | Terminates the supervisor's process tree from the stage records and the surviving supervisor. |

Recovery signals a process when its boot ID and start tick match the record.
It sets the stage record to `recovered`, or to `recovery_required` with the
surviving PIDs. The stdout, stderr, and command evidence stay in place.

Supervision begins with a committed process or stage record. A SIGKILL before
that commit can leave a service whose command log predates its record.

Recover such a service:

1. Inspect the log, kernel start ticks, and listening ports.
2. Stop the confirmed process tree.
3. Repeat `down`.

After `down` reports `stopped`, check the status and teardown documents for a
surviving worker group when the service leader exited before teardown recorded
its workers.

The helper supervisor also adopts workers when a helper leader exits.

## Reference GPU timeout check

Run this optional check in the Ubuntu shell on the reference GPU host, with the
pinned template and an instance you choose. It exercises vLLM workers and CUDA
cleanup.

1. From a successful process generation, retain the startup logs and
   `native-start-shared.log.*.stage.json`.
2. Run `narwhal dev down`.
3. Record idle device memory, process IDs, and engine, attestation, and NIXL ports.
4. Find first CUDA allocation and startup completion times in retained logs.
5. Set `STARTUP_BUDGET_SECONDS` to expire after the allocation, before startup completes.
6. Run the timeout cycle with that budget:

    ```bash
    NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS="$STARTUP_BUDGET_SECONDS" \
      narwhal dev up --instance runs/dev-timeout
    narwhal dev status --instance runs/dev-timeout
    narwhal dev down --instance runs/dev-timeout
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv
    ```

7. Record the pre-expiry allocation, command duration, and worker exit, then
   compare the duration with budget plus termination grace and rollback time.
8. Record post-cleanup device memory and port release, and compare the memory
   with the idle baseline.
9. Retain `lifecycle.json` and the teardown, startup, memory, and stage
   artifacts.
10. Confirm that an unrelated process survives.
11. Start and verify a fresh process generation with the normal budget.

## Reference GPU interruption check

This optional procedure collects vLLM, CUDA, and port-release evidence on the
reference GPU.

Prerequisites:

- an instance you choose on the reference GPU host, with the pinned runtime;
- a completed normal `up` and `verify` cycle;
- its retained process generation, device-memory baseline, process tree, and
  engine, attestation, and NIXL port assignments;
- a separately identified process that stays alive during both checks.

### Interrupt startup with SIGINT

1. Start a fresh process generation with `narwhal dev up`.
2. Wait for a committed engine identity and an observed CUDA allocation.
3. Send SIGINT to the `narwhal dev up` process.
4. Record the process exit, retained startup failure, and worker exits.
5. Record device memory and port state.
6. From a fresh shell, run `narwhal dev status` and `narwhal dev down`.

### Interrupt verification with SIGKILL

1. Start and verify a fresh process generation.
2. Launch another `narwhal dev verify`.
3. Wait for its preflight stage process record.
4. Send SIGKILL to that `narwhal dev verify` process.
5. Record the output of `narwhal dev status`.
6. Run `narwhal dev down` twice.
7. Compare surviving process identities, device memory, and bound ports against the baseline.

### Retain the evidence

1. After teardown, confirm the separately identified process's identity.
2. Record vLLM, driver, CUDA, and GPU versions with the signal and barrier.
3. Retain the evidence:
    - the `narwhal dev` output and `lifecycle.json`;
    - the stage and teardown documents;
    - vLLM logs and GPU process listings;
    - timestamped memory and port observations.

Investigate the PID of any surviving CUDA allocation or engine port before
starting another process generation.
