# Controller interruption recovery

If a `narwhal dev` command was stopped partway through (SIGINT, SIGTERM or
SIGKILL), open a new shell and run:

```bash
narwhal dev status
narwhal dev down
```

These two commands recover most interruptions. Cases that need manual steps
are listed under "Cleaning up by hand".

## What happens in each case

The Linux recovery tests cover the behavior in this table. `down` finds a process only
if Narwhal recorded it before the interruption.

| The controller was…                                             | Signal          | What happens                                                                                                             |
| --------------------------------------------------------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Starting a child, before recording its identity                 | SIGINT          | Startup stops the child's process tree and leaves spawn cleanup evidence. `down` reports `stopped`.                      |
| Starting a child, before recording its identity                 | SIGKILL         | `down` only knows about the last committed records. Find the new service from its command and log.                       |
| Partway through replacing a process record                      | SIGKILL         | Records are replaced atomically, so readers see the previous complete one. Check the service named in the pending write. |
| Just after committing a process record                          | SIGINT          | Startup rolls back.                                                                                                      |
| Just after committing a process record                          | SIGTERM         | The controller exits, and `down` terminates the recorded group.                                                          |
| Waiting for a service to become ready                           | SIGKILL         | `status` reports `degraded`, and `down` terminates the recorded group.                                                   |
| Running a profiling helper                                      | SIGINT, SIGTERM | The helper stops and startup rolls back.                                                                                 |
| Running a profiling helper                                      | SIGKILL         | `down` uses the helper and service records to clean up.                                                                  |
| Running a verification helper                                   | SIGTERM         | Verification is recorded as `degraded` and the services keep running. `down` stops the helpers and services.             |
| Running a verification helper                                   | SIGKILL         | The helper's output stays on disk. `down` stops the helpers and services.                                                |
| Running after a service leader exited but its worker kept going | SIGKILL         | `status` lists the surviving group, and teardown asks you to check the worker's identity.                                |

## Helpers that outlive the controller

Stage records store the boot ID and start ticks of every helper process,
including its children. After an abrupt exit, `status` lists interrupted
helpers under `stage_processes`. `down` stops only the processes whose boot ID
and start ticks match those records and leaves their stdout, stderr, and
command files in place.

After `down`, each interrupted helper's stage record is `recovered` (every
recorded helper was stopped) or `recovery_required` (a process survived; the
record lists the PIDs).

## Cleaning up by hand

Manual cleanup is required when:

- The controller received SIGKILL before it recorded a new process. The
  service's command log shows a process that is not in the records.
- A service leader exited before teardown could record its workers. The
  `status` and `down` output shows a surviving group.
- A stage record is `recovery_required`. It lists the surviving PIDs.

In each case, compare the command log, the kernel start ticks, and the
listening ports against the process to confirm it belongs to Narwhal. Stop its
process tree, then run `narwhal dev down` again to update the records.
