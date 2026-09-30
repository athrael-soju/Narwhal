# Reference GPU recovery checks

These checks are optional. They show, on the reference GPU, that when a run
is cut short, vLLM workers really exit, CUDA memory is released, and ports
are freed. Use the pinned template and runtime, and pick an instance
directory just for this work.

The examples use `runs/dev-timeout`. Create it with
`narwhal dev init --instance runs/dev-timeout` and the `--template` and
`--interface` flags you normally use. Then pass `--instance runs/dev-timeout`
to every `narwhal dev` command on this page, including the ones shown
without it.

## Record a baseline

You need a known-good picture to compare against.

1. Run `narwhal dev up`, then `narwhal dev verify`.
2. Keep the startup logs and `native-start-shared.log.*.stage.json`. You'll
   use their timestamps in the timeout check.
3. Run `narwhal dev down`.
4. Note the idle GPU memory, the process tree, and which engine, attestation,
   and NIXL ports are in use.
5. Write down the vLLM, driver, CUDA, and GPU versions.
6. Start a sentinel process of your own, unrelated to Narwhal, and note its
   PID. Leave it running through every check. If a cleanup ever kills it,
   Narwhal stopped something it didn't own.

## Stage timeout check

This check makes shared startup run out of time on purpose, at a point
where the GPU already holds memory, and confirms everything is released.

1. Choose a value for `STARTUP_BUDGET_SECONDS` that expires partway through
   shared startup: after the first engine has allocated CUDA memory, but
   before shared startup finishes. The baseline's startup logs and stage
   record show when each of those happened.
2. Run startup with that budget, then clean up:

    ```bash
    NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS="$STARTUP_BUDGET_SECONDS" \
      narwhal dev up --instance runs/dev-timeout
    narwhal dev status --instance runs/dev-timeout
    narwhal dev down --instance runs/dev-timeout
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv
    ```

3. Record the memory allocated before the budget ran out, how long the
   command took, whether the workers exited, GPU memory after cleanup, and
   the state of the ports.
4. Compare what you saw with the table below.
5. Confirm the instance still works with the normal budget:

    ```bash
    narwhal dev up --instance runs/dev-timeout
    narwhal dev verify --instance runs/dev-timeout
    ```

| What to check                      | What you should see                                                   |
| ---------------------------------- | --------------------------------------------------------------------- |
| How long the command took          | No longer than the stage budget plus both grace periods plus rollback |
| GPU memory after cleanup           | Back to the idle baseline                                             |
| Engine, attestation and NIXL ports | All free                                                              |
| Your sentinel process              | Still running                                                         |

## Controller interruption checks

### SIGINT during startup

1. Run `narwhal dev up` on a stopped instance.
2. Wait until an engine's identity has been recorded and it has allocated
   CUDA memory.
3. Send SIGINT to the PID of the `narwhal dev up` controller.
4. Record how the controller exited, the startup failure it left behind,
   whether the workers exited, GPU memory, and port state.
5. From a new shell, run `narwhal dev status` and then `narwhal dev down`.

### SIGKILL during verification

1. Run `narwhal dev up`, then `narwhal dev verify`.
2. Start a second `narwhal dev verify`.
3. Wait until its preflight stage has written a process record.
4. Send SIGKILL to that second controller's PID.
5. Record the output of `narwhal dev status`.
6. Run `narwhal dev down` twice. The second run should find nothing left
   to do.
7. Compare surviving processes, GPU memory, and bound ports with the
   baseline.

## Between checks

Before you move on, make sure your sentinel process is still running. If
any CUDA memory is still allocated or an engine port is still bound, find
the PID responsible and work out why it survived. Only then start the next
check.

## What to keep

For every check, keep a note of which signal you sent and where the run was
when you sent it, along with the vLLM, driver, CUDA, and GPU versions.

For the stage timeout check, also keep `lifecycle.json` and the teardown,
startup, memory, and stage files. For the interruption checks, keep the
controller's output, `lifecycle.json`, the stage and teardown documents, the
vLLM logs, GPU process listings, and your timestamped memory and port
observations.
