# Reference GPU recovery checks

These opt-in checks qualify vLLM worker exit, CUDA cleanup and port release on
the reference GPU. Use the pinned template and runtime with an
operator-selected instance.

## Record the baseline

1. Run `narwhal dev up`.
2. Run `narwhal dev verify`.
3. Retain the startup logs and `native-start-shared.log.*.stage.json`.
4. Run `narwhal dev down`.
5. Record idle device memory, the process tree, and the engine, attestation
   and NIXL ports.
6. Record the vLLM, driver, CUDA and GPU versions.
7. Start a separately identified process to keep running through every check.

## Stage timeout check

1. Select `STARTUP_BUDGET_SECONDS` from the baseline stage timestamps: after
   the first engine's CUDA allocation and before shared startup completes.
2. Run startup with that budget:

    ```bash
    NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS="$STARTUP_BUDGET_SECONDS" \
      narwhal dev up --instance runs/dev-timeout
    narwhal dev status --instance runs/dev-timeout
    narwhal dev down --instance runs/dev-timeout
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv
    ```

3. Record the allocation before expiry, command duration, worker exit, device
   memory after cleanup and port state.
4. Compare the observations with the expected values.
5. Run `narwhal dev up` with the default budget.
6. Run `narwhal dev verify`.

| Observation                        | Expected value                                                        |
| ---------------------------------- | --------------------------------------------------------------------- |
| Command duration                   | At most stage budget + cleanup grace + kill grace + rollback time     |
| Device memory after cleanup        | Idle baseline                                                         |
| Engine, attestation and NIXL ports | Released                                                              |
| Separately identified process      | Running                                                               |

## Controller interruption check

### SIGINT during startup

1. Run `narwhal dev up` on the stopped instance.
2. Wait for a committed engine identity and an observed CUDA allocation.
3. Send SIGINT to the `narwhal dev up` controller PID.
4. Record the controller exit, retained startup failure, worker exits, device
   memory and port state.
5. Run `narwhal dev status` from a fresh shell.
6. Run `narwhal dev down` from the same fresh shell.

### SIGKILL during verification

1. Run `narwhal dev up`.
2. Run `narwhal dev verify`.
3. Launch another `narwhal dev verify`.
4. Wait for its preflight stage process record.
5. Send SIGKILL to that controller PID.
6. Record `narwhal dev status`.
7. Run `narwhal dev down` twice.
8. Compare surviving process identities, device memory and bound ports with
   the baseline.

## After each check

1. Confirm the separately identified process is running.
2. Investigate the PID behind any surviving CUDA allocation or occupied engine
   port.
3. Start the next generation.

## Evidence to retain

| Check                   | Artifacts                                                                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Stage timeout           | `lifecycle.json`, teardown, startup, memory and stage artifacts                                                                             |
| Controller interruption | Controller output, `lifecycle.json`, stage and teardown documents, vLLM logs, GPU process listings, timestamped memory and port observations |
| Every check             | Exercised signal and interruption point, vLLM, driver, CUDA and GPU versions                                                                |
