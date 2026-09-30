# Stage deadlines and recovery

`narwhal dev up` and `narwhal dev verify` split their work into stages. Each
stage runs as its own subprocess with its own time limit, so a stage that
hangs gets stopped instead of leaving the whole command stuck.

## Time limits

Every stage gets 300 seconds by default. You can change that for all stages
with `NARWHAL_STAGE_TIMEOUT_SECONDS`, or for a single stage with
`NARWHAL_STAGE_<NAME>_TIMEOUT_SECONDS`. For the per-stage variable, write
the stage name in uppercase and replace any punctuation with underscores:
`native-start-shared` becomes `NATIVE_START_SHARED`.

| Stage                     | Command  | What it does                            | Per-stage variable                                  |
| ------------------------- | -------- | --------------------------------------- | --------------------------------------------------- |
| `engine-1`, `engine-2`, … | `up`     | Checks the runtime for one engine       | `NARWHAL_STAGE_ENGINE_1_TIMEOUT_SECONDS`            |
| `native-start-shared`     | `up`     | Starts the engines                      | `NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS` |
| `attest-1`, `attest-2`, … | `up`     | Captures the attestation for one engine | `NARWHAL_STAGE_ATTEST_1_TIMEOUT_SECONDS`            |
| `profile-1p1d`, …         | `up`     | Profiles one prefill/decode split       | `NARWHAL_STAGE_PROFILE_1P1D_TIMEOUT_SECONDS`        |
| `profile-merge`           | `up`     | Merges the profiles                     | `NARWHAL_STAGE_PROFILE_MERGE_TIMEOUT_SECONDS`       |
| `preflight`               | `verify` | Tests the directed KV paths             | `NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS`           |

Set a limit for a single run by putting the variable in front of the
command:

```bash
NARWHAL_STAGE_NATIVE_START_SHARED_TIMEOUT_SECONDS=720 narwhal dev up
NARWHAL_STAGE_PREFLIGHT_TIMEOUT_SECONDS=120 narwhal dev verify
```

The clock starts when the subprocess is launched, so helper imports and
process startup count against the budget, not just the stage's own work.
Inside `native-start-shared` there's a second, separate deadline: each
engine has 180 seconds to pass its health check.

When a stage runs out of time, or the command receives SIGINT or SIGTERM,
Narwhal sends the stage SIGTERM and waits `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS`
(10 by default). If the stage is still running after that, it gets SIGKILL,
followed by another wait of `NARWHAL_STAGE_KILL_GRACE_SECONDS` (5 by
default). In the worst case, then, a stage lasts its budget plus both grace
periods, which is 15 seconds with the defaults.

## What a failed stage leaves behind

| File                   | Contents                                                                                                               |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `*.command.json`       | The command the stage ran                                                                                              |
| `*.stdout`, `*.stderr` | Whatever the stage printed before it stopped; kept private                                                             |
| `*.stage.json`         | Budget, start time, elapsed time, exit status, process identities, which signals were sent, and any PIDs that survived |
| `lifecycle.json`       | The failure that started the rollback, plus any errors during teardown                                                 |
| `verify-*/`            | The verification attempt, kept for inspection                                                                          |

## Recovering from a failed stage

1. Read the failed stage's `*.stdout`, `*.stderr`, and `*.stage.json` to
   find out what went wrong.
2. Run `narwhal dev status --instance PATH`.
3. Run `narwhal dev down --instance PATH`.
4. If any process is still running after `down`, check its boot ID, start
   tick, and process group against the recorded values before touching it.
   PIDs get reused, and these checks make sure it's really one of yours.
5. Stop each process you've confirmed.
6. Fix the cause you found in step 1.
7. Run `narwhal dev up --instance PATH`.

## Docker engine launches

When engines run in Docker, the same stage limits apply to the Docker
client calls and to the native runtime checks. Narwhal labels every
container it creates or runs. `io.narwhal.launch` holds the launch token
from `docker-owner.json`, and `io.narwhal.operation` holds a token unique to
that particular create or run call.

If a Docker client call times out or is canceled, Narwhal reconciles what
it left behind, with a limit of `NARWHAL_DOCKER_RECONCILE_SECONDS` (30 by
default). It removes any container the interrupted call created, and the
container an interrupted start was aimed at. Other containers from the same
launch are left alone, and their IDs are recorded. With the default grace
periods added on, reconciliation takes at most 45 seconds.

Each reconciliation writes a `docker-reconcile-*.json` file. It records the
operation token, which containers were targeted, removed, kept or found
still running, any refusals and inspection errors, and when the observation
was made.

### When reconciliation says `inspection_required`

This means the Docker daemon stopped responding before reconciliation could
finish, so Narwhal couldn't tell what state the containers were in.

1. Leave the original launch directory as it is.
2. Wait for the Docker daemon to come back.
3. List the containers carrying the recorded operation token:

    ```bash
    docker ps -a --filter "label=io.narwhal.operation=OPERATION_TOKEN"
    ```

4. Compare the list with the IDs in `docker-reconcile-*.json`.
5. Remove the containers the interrupted operation created.
6. Retry the launch.
