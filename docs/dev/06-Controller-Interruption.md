# Controller interruption recovery

Recover an interrupted instance from a fresh shell:

```bash
narwhal dev status
narwhal dev down
```

The Linux recovery suite covers these interruption cases:

| Interruption point                                     | Signal          | Recovery                                                                                              |
| ------------------------------------------------------ | --------------- | ----------------------------------------------------------------------------------------------------- |
| Child created, before identity capture                 | SIGINT          | Startup stops the child tree. Spawn cleanup evidence remains. `down` reports `stopped`.               |
| Child created, before identity capture                 | SIGKILL         | `down` uses the last committed process records. Identify the new service from its command and log.   |
| Process record awaiting atomic replacement             | SIGKILL         | Readers see the previous complete record. Inspect the service named in the pending write.             |
| Process record committed                               | SIGINT          | Startup rolls back.                                                                                   |
| Process record committed                               | SIGTERM         | The controller exits. `down` terminates the recorded group.                                           |
| Service readiness wait                                 | SIGKILL         | `status` reports `degraded`. `down` terminates the recorded group.                                    |
| Profiling helper active                                | SIGINT, SIGTERM | The helper stops. Startup rolls back.                                                                 |
| Profiling helper active                                | SIGKILL         | `down` recovers the helper and service records.                                                       |
| Verification helper active                             | SIGTERM         | Verification records `degraded` with services running. `down` terminates the helpers and services.   |
| Verification helper active                             | SIGKILL         | Interrupted helper evidence remains. `down` terminates the helpers and services.                      |
| Recorded service leader exited, delayed worker running | SIGKILL         | `status` lists the surviving group. Teardown requests inspection of the worker's identity.            |

## Interrupted helpers

Stage documents record the boot ID and process start ticks of every observed
helper descendant.

| After an abrupt controller exit | Result                                                                   |
| ------------------------------- | ------------------------------------------------------------------------ |
| `status`                        | Lists interrupted helpers in `stage_processes`                           |
| `down`                          | Signals the processes whose boot ID and start tick match the stage records |
| Stage evidence                  | Stdout, stderr and command evidence remain                               |

| Stage record status after `down` | Meaning                                      |
| -------------------------------- | -------------------------------------------- |
| `recovered`                      | `down` stopped every recorded helper process |
| `recovery_required`              | The record lists the surviving PIDs          |

## Manual recovery

| Case                                                     | Evidence                                           |
| -------------------------------------------------------- | -------------------------------------------------- |
| SIGKILL before the process record commits                | Service command log with an unrecorded process     |
| Service leader exited before teardown recorded its workers | Surviving group in the status and teardown documents |
| Stage record at `recovery_required`                      | Surviving PIDs in the stage record                 |

1. Inspect the command log, kernel start ticks and listening ports.
2. Stop the confirmed process tree.
3. Run `narwhal dev down` again.
