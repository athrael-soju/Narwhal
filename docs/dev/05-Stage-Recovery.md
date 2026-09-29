# Stage deadlines and recovery

`narwhal dev up` and `narwhal dev verify` run each stage as a subprocess with
a separate execution budget.

## Stage budgets

| Variable                              | Default | Applies to                                                            |
| ------------------------------------- | ------- | --------------------------------------------------------------------- |
| `NARWHAL_STAGE_TIMEOUT_SECONDS`       | 300     | Every stage                                                           |
| `NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS` |        | One stage: `<NAME>` is the stage name in uppercase with punctuation as `_` |
| `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS` | 10      | SIGTERM wait after expiry, SIGINT or SIGTERM                          |
| `NARWHAL_STAGE_KILL_GRACE_SECONDS`    | 5       | SIGKILL wait after the cleanup grace                                  |

| Stage                     | Command  | Work                                  | Override example                                    |
| ------------------------- | -------- | ------------------------------------- | --------------------------------------------------- |
| `engine-1`, `engine-2`, … | `up`     | Runtime check for one engine          | `NARWHAL_STAGE_ENGINE_1_TIMEOUT_SECONDS`            |
| `native-start-shared`     | `up`     | Native shared startup                 | `NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS` |
| `attest-1`, `attest-2`, … | `up`     | Attestation capture for one engine    | `NARWHAL_STAGE_ATTEST_1_TIMEOUT_SECONDS`            |
| `profile-1p1d`, …         | `up`     | Profiling for one role split          | `NARWHAL_STAGE_PROFILE_1P1D_TIMEOUT_SECONDS`        |
| `profile-merge`           | `up`     | Profile merge                         | `NARWHAL_STAGE_PROFILE_MERGE_TIMEOUT_SECONDS`       |
| `preflight`               | `verify` | Preflight across directed KV paths    | `NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS`           |

Set a budget for one command:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=720 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

| Deadline            | Covers                                                  |
| ------------------- | ------------------------------------------------------- |
| Stage budget        | Helper imports, subprocess startup and execution        |
| Engine health check | 180 s per engine inside `native-start-shared`           |

Maximum stage duration:

```text
stage budget + cleanup grace + kill grace
```

## Stage evidence

| File                    | Contents                                                                                           |
| ----------------------- | -------------------------------------------------------------------------------------------------- |
| `*.command.json`        | Stage command                                                                                      |
| `*.stdout`, `*.stderr`  | Private partial output                                                                             |
| `*.stage.json`          | Budget, wall-clock start, elapsed time, exit status, process identities, signal escalation, surviving PIDs |
| `lifecycle.json`        | Initiating failure and teardown errors from startup rollback                                       |
| `verify-*/`             | Retained verification attempt                                                                      |

## Recover a failed stage

1. Inspect the failed stage's `*.stdout`, `*.stderr` and `*.stage.json`.
2. Run `narwhal dev status --instance PATH`.
3. Run `narwhal dev down --instance PATH`.
4. Inspect the recorded boot ID, start tick and process group of any process
   that survives `down`.
5. Stop each confirmed surviving process.
6. Repair the failing input.
7. Run `narwhal dev up --instance PATH`.

## Docker engine launches

The engine deployment wrapper applies the stage budgets to Docker clients and
native runtime checks. Each Docker create or run carries two labels:

| Label                  | Value                                   |
| ---------------------- | --------------------------------------- |
| `io.narwhal.launch`    | Launch token from `docker-owner.json`   |
| `io.narwhal.operation` | Unique token for that create or run     |

A timed-out or cancelled Docker client starts reconciliation under
`NARWHAL_DOCKER_RECONCILE_SECONDS` (default 30).

| Container                                  | Reconciliation         |
| ------------------------------------------ | ---------------------- |
| Created by the interrupted operation       | Removed                |
| Explicit target of an interrupted start    | Removed                |
| Other containers in the launch directory   | Preserved, IDs recorded |

Maximum reconciliation time, 45 s with defaults:

```text
NARWHAL_DOCKER_RECONCILE_SECONDS + cleanup grace + kill grace
```

`docker-reconcile-*.json` records the operation token, the targeted, removed,
preserved and surviving IDs, refusal decisions, inspection errors and
observation time.

### Resolve `inspection_required`

A stalled daemon leaves reconciliation at `inspection_required`.

1. Keep the original launch directory.
2. Wait for the Docker daemon to recover.
3. List the containers carrying the recorded operation token:

    ```bash
    docker ps -a --filter "label=io.narwhal.operation=OPERATION_TOKEN"
    ```

4. Compare them with the IDs in `docker-reconcile-*.json`.
5. Remove containers created by the interrupted operation.
6. Retry the launch.
