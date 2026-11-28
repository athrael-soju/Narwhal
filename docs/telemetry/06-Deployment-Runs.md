---
description: The run record a deployment stage run keeps, its run directory, its lock and its fields.
---

# Deployment run record

The stage runner keeps one run record, `run.json`, in each run directory. It records every stage attempt, the digests that decide whether a stage still holds, and the files each attempt wrote. The [rerun rules](../Deploy.md#rerunning-after-a-change) read it to decide what a run repeats. `narwhal dev up` keeps its run directory as `launch` in each dev run directory, with the rules in [Resuming an interrupted `up`](../cli/Dev.md#resuming-an-interrupted-up).

A run directory holds:

| Path | Contents |
| --- | --- |
| `run.json` | The run record, `narwhal.deployment-run` version 2, at mode 0600 |
| `run.lock` | The lock one runner holds while it uses the directory |
| `run.salt` | 32 random bytes at mode 0600 that key the record's digests of inputs and files. The first run makes it, and a runner refuses one of another size, owner or mode |
| `stages/<stage>/attempt-<n>/` | The files attempt `n` of a stage that runs once per run wrote |
| `stages/<stage>/<instance>/attempt-<n>/` | The files attempt `n` of one stage instance wrote, such as one engine role |

Inputs and the files an attempt writes can hold credentials, such as the engine key in a role environment. The record keeps each of their digests as `hmac-sha256:<hex>`, an HMAC-SHA256 of the value's SHA-256 under `run.salt`. A copy of `run.json` without `run.salt` cannot confirm a guessed credential. Changing a credential still changes its digest and reruns the stages it feeds. Never copy `run.salt` out of the run directory. A diagnostic bundle leaves it out.

A record that an earlier version wrote keeps unkeyed `sha256:` digests and an output `sha256`. The next run makes `run.salt` and keys those digests in place, so they match the keyed digests of the same values and no stage reruns for the change. A Narwhal older than this one cannot read a keyed record.

Each attempt writes into a new directory, so a rerun never meets a file an earlier attempt left. The runner replaces `run.json` by writing a temporary file, flushing it to disk and renaming it over the record, so a crash leaves either the old record or the new one. The next runner removes a temporary file a crash left.

The run directory must belong to the user who runs it and give no access to other users. The runner creates it at mode 0700 and refuses an existing directory with wider permissions. `status` and `down` refuse the same directories, and a `run.json` that is not a regular file the user owns, before they act on the record.

## Locking

A runner holds an exclusive `flock` on `run.lock` for the whole run and names itself in the record's `lock` field. A second runner on the same directory is refused. The refusal names the owner from `lock` when that owner is a live process on this host. Otherwise the holder has not recorded itself yet, and the refusal says so.

When a runner ends without clearing `lock`, for example after `SIGKILL` or a power loss, the next runner adds the stale lock with its owner to `stale_locks`, and `deploy run` and `dev up` print it, as in `narwhal: deploy run: this run found a stale lock of pid 4242 of operator on workstation (narwhal deploy run --run runs/first-deploy), ...`. With `--format json`, `deploy run` also puts the owner in the result's `data.stale_lock`. It stops any helper processes the interrupted stage left, using the process identities the stage recorded, and records that stage `interrupted` before it continues. If helpers survive, it refuses to run until they are stopped. A recorded owner that still runs on this host without holding `run.lock` is refused too.

An interrupted stage runs a new attempt, unless its action can take over the work the attempt recorded, such as the Jobs in `jobs`. Then the runner resumes the same attempt in its own directory, as long as the stage's `definition` and the `input_digests` the attempt started from still match, and no output of a stage before it changed. A changed output marks the interrupted stage `invalidated`, as it does a passed one.

## Signals

`SIGINT` and `SIGTERM` end the current stage. The stage's cleanup runs, the attempt is recorded as `cancelled` with its failure context and the processes it had started, and the run stops. The next run starts again at that stage. A signal that arrives while the runner writes `run.json` waits until the write completes.

## Record fields

| Field | Content |
| --- | --- |
| `schema`, `schema_version` | `narwhal.deployment-run` and `2` |
| `run_id` | 32 hexadecimal characters, fixed for the life of the run directory |
| `target` | `dev`, `ssh` or `kubernetes`. A runner for another target refuses the directory |
| `narwhal_version` | Narwhal version of the last runner |
| `revision` | Approved revision the last runner deployed |
| `graph_sha256` | Digest of the stage graph and rerun rules the last runner read |
| `created_at`, `updated_at` | Unix seconds |
| `lock` | `null`, or `owner` and `acquired_at`. `owner` holds `pid`, `boot_id`, `start_ticks`, `hostname`, `user` and `argv` |
| `stale_locks` | Every stale lock a runner found in this directory: its `owner`, `acquired_at`, and `found_at` |
| `inputs` | Each run input's `digest`, keyed under `run.salt`, and `source`. Credentials enter only as keyed digests |
| `until` | The last stage the last run was asked to reach, or `null` for the whole graph |
| `stages` | One record per stage instance, keyed by stage name, such as `discover`, or by stage and instance, such as `engine-check[engine-2]` |

Each stage record:

| Field | Content |
| --- | --- |
| `stage`, `scope`, `instance` | Stage name, scope letter from the stage graph, and instance. A stage that runs once per run has the empty instance |
| `status` | `running`, `passed`, `failed`, `cancelled`, `timeout`, `interrupted` for an attempt whose runner ended without recording its end, or `invalidated` for a passed stage that must rerun because an output before it changed or, for a stage whose product reruns its other instances, because another instance's product changed |
| `attempt` | Attempt number in this run directory, from 1 |
| `directory` | The attempt's directory, relative to the run directory |
| `definition` | Digest of the stage's place in the graph and the rules that rerun it. A changed definition reruns the stage |
| `input_digests` | Digest of each fact whose change reruns this stage, keyed like `slo` or `engine_identity[engine-2]`. An input's digest is keyed under `run.salt`; a product's is its `sha256:` digest, since the record holds the product itself. A passed attempt records them as it passes. Any other attempt keeps the digests it started from, without the facts the stage produces itself |
| `outputs` | Each regular file a passed attempt wrote: `path` relative to the run directory, `hmac_sha256`, the keyed digest of its contents, `size` and `mode` |
| `live_outputs` | Paths, relative to the run directory, of the files and directories in the attempt directory that the stage's processes keep writing, such as their logs. They have no digest, and their changes never rerun the stage |
| `products` | Each value the stage produces for later stages, such as an engine's binding digest or a process identity. A running, failed, cancelled, timed-out or interrupted attempt keeps the values of the last passed one |
| `processes` | Identities of the long-lived processes the stage started. A running attempt keeps those of the attempt before it until it returns its own |
| `command` | `null` for a stage that runs in the runner. For a command stage, `argv`, `cwd`, `exit_code` and `log`, with paths relative to the run directory |
| `started_at`, `ended_at` | Unix seconds. A running stage has no `ended_at` |
| `invalidation` | Why an `invalidated` stage must rerun, such as `output of engine-check[engine-1] changed`, or `null` |
| `jobs` | Names of the Kubernetes Jobs the attempt creates, each written to the record before its Job is created. A resumed attempt keeps them. Empty when the attempt creates no Job |
| `stale_jobs` | Jobs of earlier attempts of this stage that no attempt has deleted yet. A new attempt takes over the earlier attempt's `jobs` and `stale_jobs`, and drops each name only once its Job is deleted, so a crash at any point leaves every Job named |
| `reasons` | Why the attempt ran, such as `no attempt yet`, `slo changed` or `attempt 1 interrupted`. A resumed attempt keeps the reasons it first ran for |
| `observed_at` | Unix seconds at which each live product, such as a process identity or a route, was last found to hold. The runner checks a live product again on every run unless the target gives that fact a maximum age and this observation is younger |
| `failure` | `null` for a running, passed or invalidated stage. Otherwise `message`, `recovery`, the gate section that covers the stage, and `context`, such as the stage timeout and the cleanup of its helper processes |

A stage is skipped when its record shows `passed`, its `definition` and `input_digests` match the current graph and inputs, every file in `outputs` still has its recorded digest, size and mode, and every process or route the stage produced is still there or was observed within its maximum age. When an output changes, the runner marks every passed stage after it `invalidated` before it reruns anything, so those stages rerun even if the run stops first.

## Deployment status

After each `narwhal deploy run`, passed or failed, the command builds a summary of the run, `narwhal.deployment-status` version 1, and copies it to `runs/deployment/deployment-status.json` under the install root of each router host. The routers export it as [deployment metrics](03-Metrics-and-Control.md#deployment-and-restart-metrics). The copy is skipped until every host's `install` has passed, and a failed copy prints a warning without failing the run.

| Field | Content |
| --- | --- |
| `schema`, `schema_version` | `narwhal.deployment-status` and `1` |
| `fleet`, `target`, `run_id` | `NARWHAL_FLEET_NAME`, the deployment target such as `ssh`, and the run's ID |
| `updated_at` | When a stage of the run last ended, Unix seconds |
| `last_passed_at` | When the last run that passed every stage up to `monitoring`, or up to `trial` for a run with `--trial`, ended. A failed run keeps the previous value |
| `qualified_at`, `profiled_at`, `calibrated_at` | When `preflight`, `profile` and `calibrate` last passed, or `null` |
| `stages` | Each stage instance with `stage`, `instance` and `state`, the stage record's `status` |
| `fabric` | Each `fabric-edge` instance with `edge`, `measured_gbps`, the lowest bandwidth measured, and `budget_gbps`, the highest that one of its KV handoffs needs |
| `engines` | Each engine with `iid`, `generation`, the binding digest of its attestation, and `attested_at` |

