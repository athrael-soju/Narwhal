# Narwhal dev recovery and qualification

| Situation | Procedure |
| --- | --- |
| Failed stage | [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery) |
| Interrupted Docker command | [Docker command reconciliation](#docker-command-reconciliation) |
| Interrupted `narwhal dev` process | [Interrupted `narwhal dev` process](#interrupted-narwhal-dev-process) |
| Optional qualification on the reference GPU | [Reference GPU timeout check](#reference-gpu-timeout-check) and [Reference GPU interruption check](#reference-gpu-interruption-check) |

## Docker command reconciliation

The engine deployment wrapper applies the stage budgets to Docker clients and
native runtime checks.

Each Docker create or run carries two labels:

| Label                  | Value                                   |
| ---------------------- | --------------------------------------- |
| `io.narwhal.launch`    | Launch token from `docker-owner.json`   |
| `io.narwhal.operation` | Unique operation token                  |

After a Docker client timeout or cancellation, the wrapper reconciles with the
daemon within `NARWHAL_DOCKER_RECONCILE_SECONDS`, 30 seconds by default:

| Resource | Reconciliation result |
| --- | --- |
| Containers the interrupted operation created | Removed |
| Explicit target of an interrupted start | Removed |
| Other containers in the launch directory | Kept, with their IDs recorded as preserved resources |

At default settings, final cleanup adds up to 15 seconds.

Each reconciliation writes a `docker-reconcile-*.json` report with:

- the operation token;
- the targeted, removed, preserved, and surviving IDs;
- refusal decisions;
- inspection errors;
- the observation time.

For an `inspection_required` report result from a stalled daemon:

1. Inspect the persisted launch-token label and the recorded IDs before a retry.
2. After the daemon recovers, inspect it for creates completed after cancellation.
3. Keep the original launch directory until step 2 completes.

## Interrupted `narwhal dev` process

The Linux recovery suite checks twelve barrier and signal combinations for:

- complete committed JSON documents;
- released locks;
- retained output;
- idempotent teardown;
- survival of an unrelated process.

| Interruption barrier | Signals exercised | Cleanup and fresh `down` | Fresh `status` | Operator action |
| --- | --- | --- | --- | --- |
| Child created, before identity capture | SIGINT | A fresh `down` reports stopped. | | |
| Child created, before identity capture | SIGKILL | Recovery uses the last committed process set. | | Identify the newly created service from its command and log. |
| Process record awaiting atomic replacement | SIGKILL | Readers see the previous complete document. | | Inspect the service described by the pending write. |
| Process record committed | SIGINT | Startup rolls back. | | |
| Process record committed | SIGTERM | A fresh `down` terminates the recorded group. | | |
| Service readiness wait | SIGKILL | A fresh `down` terminates the recorded group. | Reports degraded. | |
| Profiling helper active | SIGINT, SIGTERM | Startup rolls back. | | |
| Profiling helper active | SIGKILL | A fresh `down` recovers the helper and service records. | | |
| Verification helper active | SIGTERM | A fresh `down` terminates the retained services. | Reports degraded verification. | |
| Verification helper active | SIGKILL | A fresh `down` terminates the helpers and services. | | |
| Recorded service leader exited, delayed worker surviving | SIGKILL | Teardown requests operator inspection. | Lists the surviving group. | Inspect the worker's identity. |

Stage documents record each observed descendant's boot ID and process start
ticks.

When the `narwhal dev` process dies abruptly:

| Command  | Behavior                                                 |
| -------- | -------------------------------------------------------- |
| `status` | Lists matching interrupted helpers in `stage_processes`. |
| `down`   | Terminates the helper supervisor's process tree.         |

The stdout, stderr, and command evidence stay in place.

| Recovery outcome | Stage record |
| --- | --- |
| Processes stopped | `recovered` |
| Processes survive | `recovery_required`, with the surviving PIDs |

A SIGKILL before the process or stage record commits can leave a service whose
command log predates its record.

Recover such a service:

1. Inspect the log, kernel start ticks, and listening ports.
2. Stop the confirmed process tree.
3. Repeat `down`.

If the service leader exited before teardown recorded its workers:

1. Wait for `down` to report `stopped`.
2. Check the status and teardown documents for a surviving worker group.

## Reference GPU timeout check

Run this check in the Ubuntu shell on the reference GPU host, with the pinned
template and an instance you choose.

1. From a successful process generation, retain the startup logs and
   `native-start-shared.log.*.stage.json`.
2. Run `narwhal dev down`.
3. Record idle device memory, process IDs, and engine, attestation, and NIXL ports.
4. Find the first CUDA allocation time and the startup completion time in the
   retained logs.
5. Set `STARTUP_BUDGET_SECONDS` to a budget that expires between the first CUDA
   allocation and startup completion.
6. Run the timeout cycle with that budget:

    ```bash
    NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS="$STARTUP_BUDGET_SECONDS" \
      narwhal dev up --instance runs/dev-timeout
    narwhal dev status --instance runs/dev-timeout
    narwhal dev down --instance runs/dev-timeout
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv
    ```

7. Record the pre-expiry allocation, command duration, and worker exit.
8. Compare the duration with the budget plus termination grace and rollback
   time.
9. Record post-cleanup device memory and port release.
10. Compare the device memory with the idle baseline.
11. Retain `lifecycle.json` and the teardown, startup, memory, and stage
    artifacts.
12. Confirm that an unrelated process survives.
13. Start and verify a fresh process generation with the normal budget.

## Reference GPU interruption check

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
4. Investigate the PID of each surviving CUDA allocation or engine port before
   you start another process generation.
