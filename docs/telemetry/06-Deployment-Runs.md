---
description: The run record a deployment stage run keeps, its run directory, its lock and its fields.
---

# Deployment run record

The stage runner keeps one run record, `run.json`, in each run directory. It records every stage attempt, the digests that decide whether a stage still holds, and the files each attempt wrote. The [rerun rules](../Deploy.md#rerunning-after-a-change) read it to decide what a run repeats.

A run directory holds:

| Path | Contents |
| --- | --- |
| `run.json` | The run record, `narwhal.deployment-run` version 1, at mode 0600 |
| `run.lock` | The lock one runner holds while it uses the directory |
| `stages/<stage>/attempt-<n>/` | The files attempt `n` of a stage that runs once per run wrote |
| `stages/<stage>/<instance>/attempt-<n>/` | The files attempt `n` of one stage instance wrote, such as one engine role |

Each attempt writes into a new directory, so a rerun never meets a file an earlier attempt left. The runner replaces `run.json` by writing a temporary file, flushing it to disk and renaming it over the record, so a crash leaves either the old record or the new one. The next runner removes a temporary file a crash left.

The run directory must belong to the user who runs it and give no access to other users. The runner creates it at mode 0700 and refuses an existing directory with wider permissions.

## Locking

A runner holds an exclusive `flock` on `run.lock` for the whole run and names itself in the record's `lock` field. A second runner on the same directory is refused. The refusal names the owner from `lock` when that owner is a live process on this host. Otherwise the holder has not recorded itself yet, and the refusal says so.

When a runner ends without clearing `lock`, for example after `SIGKILL` or a power loss, the next runner reports the stale lock with its owner and adds it to `stale_locks`. It stops any helper processes the interrupted stage left, using the process identities the stage recorded, and records that stage as failed before it continues. If helpers survive, it refuses to run until they are stopped. A recorded owner that still runs on this host without holding `run.lock` is refused too.

## Signals

`SIGINT` and `SIGTERM` end the current stage. The stage's cleanup runs, the attempt is recorded as `cancelled` with its failure context and the processes it had started, and the run stops. The next run starts again at that stage. A signal that arrives while the runner writes `run.json` waits until the write completes.

## Record fields

| Field | Content |
| --- | --- |
| `schema`, `schema_version` | `narwhal.deployment-run` and `1` |
| `run_id` | 32 hexadecimal characters, fixed for the life of the run directory |
| `target` | `dev`, `ssh` or `kubernetes`. A runner for another target refuses the directory |
| `narwhal_version` | Narwhal version of the last runner |
| `revision` | Approved revision the last runner deployed |
| `graph_sha256` | Digest of the stage graph and rerun rules the last runner read |
| `created_at`, `updated_at` | Unix seconds |
| `lock` | `null`, or `owner` and `acquired_at`. `owner` holds `pid`, `boot_id`, `start_ticks`, `hostname`, `user` and `argv` |
| `stale_locks` | Every stale lock a runner found in this directory: its `owner`, `acquired_at`, and `found_at` |
| `inputs` | Each run input's `digest` and `source`. Credentials enter only as digests |
| `until` | The last stage the last run was asked to reach, or `null` for the whole graph |
| `stages` | One record per stage instance, keyed by stage name, such as `discover`, or by stage and instance, such as `engine-check[engine-2]` |

Each stage record:

| Field | Content |
| --- | --- |
| `stage`, `scope`, `instance` | Stage name, scope letter from the stage graph, and instance. A stage that runs once per run has the empty instance |
| `status` | `running`, `passed`, `failed`, `cancelled`, `timeout`, or `invalidated` for a passed stage that must rerun because an output before it changed or, for a stage whose product reruns its other instances, because another instance's product changed |
| `attempt` | Attempt number in this run directory, from 1 |
| `directory` | The attempt's directory, relative to the run directory |
| `definition` | Digest of the stage's place in the graph and the rules that rerun it. A changed definition reruns the stage |
| `input_digests` | Digest of each fact whose change reruns this stage, as the passed attempt saw it, keyed like `slo` or `engine_identity[engine-2]` |
| `outputs` | Each regular file a passed attempt wrote: `path` relative to the run directory, `sha256`, `size` and `mode` |
| `products` | Each value the stage produces for later stages, such as an engine's binding digest or a process identity. A running or failed attempt keeps the values of the last passed one |
| `processes` | Identities of the long-lived processes the stage started. A running attempt keeps those of the attempt before it until it returns its own |
| `command` | `null` for a stage that runs in the runner. For a command stage, `argv`, `cwd`, `exit_code` and `log`, with paths relative to the run directory |
| `started_at`, `ended_at` | Unix seconds. A running stage has no `ended_at` |
| `invalidation` | Why an `invalidated` stage must rerun, such as `output of engine-check[engine-1] changed`, or `null` |
| `failure` | `null` for a running, passed or invalidated stage. Otherwise `message`, `recovery`, the gate section that covers the stage, and `context`, such as the stage timeout and the cleanup of its helper processes |

A stage is skipped when its record shows `passed`, its `definition` and `input_digests` match the current graph and inputs, every file in `outputs` still has its recorded SHA-256, size and mode, and every process or route the stage produced is still there. When an output changes, the runner marks every passed stage after it `invalidated` before it reruns anything, so those stages rerun even if the run stops first.
