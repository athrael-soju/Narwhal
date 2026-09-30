# Controller interruption recovery

If a `narwhal dev` command was stopped partway through, whether by Ctrl+C,
a SIGTERM from a supervisor, or `kill -9`, open a new shell and run:

```bash
narwhal dev status
narwhal dev down
```

Most of the time that's all you need. The rest of this page explains what
Narwhal can and can't clean up on its own, and what to do in the cases
where it needs help.

## What happens in each case

These are the interruptions the Linux recovery test suite exercises. The
important distinction is whether Narwhal had already written down the
process it just started. If it had, `down` can find it. If it hadn't, you
may have to find it yourself.

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

Stage records note the boot ID and start ticks of every helper process
Narwhal saw, including the helper's own children. After an abrupt exit,
`status` lists any interrupted helpers under `stage_processes`. `down` then
signals only the processes whose boot ID and start tick match those
records, and leaves their stdout, stderr, and command files in place.

Once `down` finishes, each interrupted helper's stage record ends up in one
of two states.
`recovered` means every recorded helper was stopped. `recovery_required`
means something survived, and the record lists the PIDs.

## Cleaning up by hand

You'll need to step in yourself in three situations:

- The controller got SIGKILL before it recorded a new process. The
  service's command log will show a process that isn't in the records.
- A service leader exited before teardown could record its workers. The
  status and teardown documents will show a surviving group.
- A stage record says `recovery_required`. It lists the surviving PIDs.

In each case, use the command log, the kernel start ticks, and the listening
ports to confirm which processes belong to Narwhal. Stop that process tree,
then run `narwhal dev down` again so the records catch up.
