# MCP operation lifecycle

This is the **proposed, unreleased version 1 contract** for durable management
operations. It specifies the execution and recovery behaviour to implement for
[MCP Fleet Operations v1](../MCP-Contracts.md). The existing stage runner and
local development lifecycle supply process evidence and local locking; they do
not yet implement this operation store or shared resource coordination.

## Submission and lookup

`plan_prepare`, `plan_execute` and `operation_resume` persist an operation before
returning an accepted result. An accepted result proves admission and durable
recording. Read its operation to learn whether execution succeeded.

`operation_inspect(target_id, operation_id)` returns a bounded summary in
`data.operation` and a `record_artifact_id` for a frozen, redacted snapshot of
the complete document. Its tool outcome is `success` when the snapshot was read,
including when the stored operation failed. The summary's `state` and
`result_status` report execution progress and outcome. Use `artifact_read` to
read the complete snapshot in bounded chunks.

`operation_list(target_id, cursor, limit)` returns summaries and an opaque
`next_cursor`. The default limit is 20; accepted limits are integers from 1 to
100. Results use a stable snapshot ordered by creation time, newest first, with
operation ID as the tie breaker. A cursor is bound to the target and snapshot;
an invalid cursor returns `invalid_input`. Each summary contains
`operation_id`, `parent_operation_id`, `action`, `plan_id`, `state`,
`current_stage`, `revision`, `created_at`, `updated_at`, `finished_at`,
`result_status`, `result_data`, `error_codes`, `error_count`, `stage_count` and
`resource_count`. `result_status` and `result_data` are null before terminal
completion. `error_codes` contains the first ten codes; `error_count` reports
the full count. Successful preparation has `result_data: {"plan_id": "UUID"}`;
execution and resumption use `{"summary_artifact_id": "UUID"}` to identify
their complete action result. A client can recover an operation ID through the
list after losing its submission response.

## Operation document

The document identifier is `narwhal.management-operation`, version `1`. All
fields below are required; explicitly nullable fields use JSON `null`. Operation, request, plan and artifact IDs are UUID strings; registered target
IDs are aliases. Timestamps are RFC 3339 UTC strings. Recorded plan budgets
use integer milliseconds.
Unknown compatible fields are retained; an unsupported schema version is
rejected before any execution or recovery action.

| Field | Type and meaning |
| --- | --- |
| `schema`, `schema_version` | Literal `narwhal.management-operation`, integer `1`. |
| `operation_id` | UUID assigned atomically with admission. Never reused. |
| `target_id`, `request_id` | Registered target and caller's deduplication UUID. |
| `parent_operation_id` | Source operation for resumption; otherwise null. |
| `tool` | `plan_prepare`, `plan_execute` or `operation_resume`. |
| `action` | Action from the [tool catalogue](Tools.md). |
| `plan_id`, `plan_digest` | Executed plan UUID and SHA-256 digest; both null for preparation. Preparation returns its new plan identity in `result.data`. |
| `state` | One of the states in the transition table below. |
| `revision` | Positive integer incremented for every committed record change. |
| `created_at`, `updated_at` | Admission and latest committed update times. |
| `started_at`, `finished_at` | First execution and terminal times; null until reached. |
| `deadline_at` | Overall execution deadline; null while queued. Preparation uses its persisted preparation budgets. |
| `preparation_budgets` | For preparation, the default-populated target's `preparation` object, persisted before worker start. Null for execution/resumption. Preparation's overall budget is the sum of these action and cleanup budgets. |
| `current_stage` | Stage ID from the plan, or null before execution and after terminal completion. Preparation uses `discovery`. |
| `stages` | Ordered records in the format below. |
| `resources` | Reservation records for canonical resources and current ownership. |
| `worker` | Worker identity, or null while unassigned. Contains `worker_id`, `host_id`, `boot_id`, `pid`, `start_ticks`, `heartbeat_at` and `fence`. |
| `cancellation` | Object with nullable `requested_at` and `reason`; initially both null. |
| `recovery` | Object with nullable `reason` and `observed_at`, plus arrays `errors`, `required_actions` and `artifacts`; initially nulls and empty arrays. |
| `result` | Terminal result below; null until terminal. |
| `artifacts` | All retained evidence references currently available to the caller. |

`worker_id` and `boot_id` are UUIDs. Worker `host_id` is the stable management
host alias; `pid` and `start_ticks` are positive integers. `fence` is a monotonically increasing integer assigned by the
coordinator. A worker must hold the current fence before every new stage or
external effect. Expired ownership alone never proves that remote work stopped.

Each stage record contains `stage_id`, `state`, `started_at`, `finished_at`,
`command_result`, `artifacts`, `effects` and `reused_from`. Stage state is
`pending`, `running`, `succeeded`, `failed`, `cancelled` or `reused`.
`command_result` is the original versioned command envelope when available;
adapter-only stages use null. `reused_from` is null or an object with
`operation_id`, `stage_id` and the verified evidence digests. A plan must list
all required work; an omitted required gate cannot be represented as a
successful stage.

Stage `effects` contains adapter receipts identifying created or changed
resources, their ownership and whether the effect is `confirmed`, `absent` or
`unknown`. Each receipt follows the [site adapter contract](Deployment.md).
Starting a stage persists its intention before invoking a command. Completion
persists the receipt, result and evidence before advancing to another stage.

A terminal result has these fields:

| Field | Type and meaning |
| --- | --- |
| `status` | `success`, `degraded`, `failed_gate`, `invalid_input`, `error` or `interrupted`, preserving the [command outcome meanings](../Command-Results.md). |
| `data` | Successful preparation contains `plan_id`. Other terminal outcomes contain `summary_artifact_id`, identifying the complete retained action result. |
| `errors` | Structured errors, preserving underlying command codes and context. |
| `artifacts` | References to retained successful and failed-attempt evidence. |
| `command_result` | Last underlying `narwhal.command-result`, or null when none exists. Every earlier command result remains in its stage record. |

`succeeded` uses `success` or `degraded`. A strict deployment cannot succeed with
a skipped required gate, even if an underlying command returned `degraded`.
`failed` uses `failed_gate`, `invalid_input` or `error`. `cancelled` uses
`interrupted`. Known retained engines or services are compatible with a terminal
state when their identities and ownership are recorded. Unknown effects require
`recovery_required` with `result: null`.

## State transitions

Only the transitions below are legal. `succeeded`, `failed` and `cancelled` are
terminal; resumption creates a different operation.

A terminal commit releases the operation's execution reservations. Retained
services keep their deployment ownership records, and their observed activity
remains a precondition for subsequent measurements or mutations.

| From | To | Required condition |
| --- | --- | --- |
| Admission | `queued` | Durable request, operation and applicable reservations committed together. Preparation does not reserve deployment resources. No external effect has started. |
| `queued` | `running` | Worker acquired, permissions and plan binding rechecked, required exclusions established. |
| `queued` | `failed` | Permission, binding, prerequisite or deadline validation failed before execution. |
| `queued` | `cancelled` | Cancellation persisted before a worker started. |
| `running` | `succeeded` | Every required stage accepted; final evidence and residual-resource inventory committed. |
| `running` | `failed` | A gate, input check or action failed; active helpers are stopped and effects are known. |
| `running` | `cancelling` | Cancellation requested, including a worker handling an interrupt. |
| `running` | `recovery_required` | Worker lost, ownership uncertain, cleanup incomplete or an effect cannot be reconciled. |
| `cancelling` | `cancelled` | Current work stopped, temporary resources reconciled, retained resources reported. |
| `cancelling` | `recovery_required` | Cleanup exceeded its budget or an owned process/effect remains uncertain. |
| `recovery_required` | `succeeded` | Reconciliation proves every required stage completed and persisted evidence qualifies the result. |
| `recovery_required` | `failed` | Reconciliation establishes known effects and stopped helpers; work is incomplete without a cancellation request. |
| `recovery_required` | `cancelled` | Reconciliation establishes known effects and stopped helpers after cancellation. |

An operation in `recovery_required` retains its reservations. The runner records
every reconciliation attempt and stops each attempt at its recorded deadline.
It retries inspection after worker or host connectivity is restored. Inspection
tools remain available. A client cannot clear a reservation by retrying a plan,
changing target aliases or deleting a lock file.

## Duplicate submissions

The deduplication namespace is `(registry_id, target_id, request_id)`. Admission
stores the canonical request together with its operation in one transaction.
Canonicalization validates types, applies documented defaults and sorts object
keys. The request includes the tool name, action, normalized parameters and
plan identity where applicable. It excludes the tool response `timeout_s`, which
does not change the work. Hash comparison must be followed by comparison of the
canonical request to avoid treating different requests as identical.

An identical submission returns the original operation ID in an accepted
response, whether the operation is queued, active or terminal. It never creates
another worker or repeats a stage. A changed canonical request using the same
key returns `request_id_conflict` without creating work. The key has no time
expiry while records are retained. A new attempt or changed input needs a new
request ID. Authorization is checked before returning the existing record.

A plan is consumed by one execution. Submitting that plan again through the
same tool and parent/source scope with a new request ID returns the existing
operation and binds the new key to it. It cannot launch another attempt. A
different tool or parent/source scope returns `plan_scope_mismatch`.
`operation_resume` requires a fresh, unconsumed plan; resending its identical
request still resolves through deduplication to its original child operation.

The store retains operations, deduplication records and evidence indefinitely by
default. Operator cleanup must preserve a tombstone containing the key,
canonical request, its digest and original operation ID; a retry whose record was removed returns
`operation_record_removed` and cannot execute again. Removal of evidence does
not make that evidence valid for resumption.

## Deadlines and cancellation

The recorded plan fixes the overall operation budget and each stage's action
and cleanup budgets. All durations must be finite and positive. The overall
deadline starts when a worker begins execution; execution admission reserves
the required resources and does not queue behind a conflicting operation.
Before each stage, the runner verifies sufficient remaining time for its action
and cleanup budgets. It fails before starting a stage that cannot fit.

The action budget bounds the command or adapter call. Cleanup has separate
grace and forced-stop budgets, following existing
[stage deadline and recovery behaviour](../Dev-Runtime.md#stage-deadlines-and-recovery). A
deadline failure has code `stage_timeout` or `operation_timeout`; it becomes
`failed` only after effects are known and cleanup is complete. The command
response timeout bounds admission or inspection and never changes those budgets.

`operation_cancel` records cancellation idempotently and returns the current
bounded summary and snapshot artifact. It rechecks the original action's
capabilities and allowlist before requesting cleanup; preparation cancellation
requires inspection permission. Repeated cancellation has no additional effect. A request received
after terminal completion returns that terminal snapshot. A running worker
stops admitting new stages, signals only its identified helpers, and spends
the recorded cleanup budget on the active stage. For `recovery_required`, an
explicit cancellation can request bounded cleanup after identity verification;
the state stays `recovery_required` until effects are known. A cancellation request does
not remove engines or services retained by completed stages. The result lists
those resources and links their evidence.

An underlying action may already include its own cleanup. For example, the
current `dev up` failure path attempts to stop the instance's owned processes.
The executor records the observed outcome of that cleanup; it cannot promise
that every resource present before cancellation remains running.

Explicit `dev_down` and `deployment_cleanup` plans perform teardown within their
recorded ownership scope. They preserve evidence and reject resources whose
identity no longer matches the deployment's ownership receipts.

## Disconnect, interruption and recovery

The durable worker runs independently of the stdio MCP process. Closing the
client connection does not cancel admitted work. Cancellation of an MCP request
before admission prevents work; after durable admission the client must use
`operation_cancel` to stop the operation. Loss of the admission response is
resolved through the deduplication key or operation list.

On startup, the coordinator reads stored ownership before accepting conflicting
work. A matching live worker keeps its operation. A dead or mismatched worker
puts active work in `recovery_required`. Local identities include boot ID, PID
and process start ticks; remote services use the adapter's resource identities.
A reused PID or a changed engine generation cannot establish ownership.

Reconciliation inspects persisted intentions, receipts, helper identities and
live state. The startup and restored-connectivity scanner performs bounded
read-only checks; operation inspection never starts cleanup. Explicit
cancellation can recover identified temporary helpers under the action's
current authorization. Reconciliation must establish whether an interrupted installation or launch
took effect before any replay. A receipt absent from disk is not proof that a
remote action did not happen. Unreachable hosts and incomplete identity evidence
keep the operation in `recovery_required`.

The coordinator fences the previous worker and confirms it cannot issue more
effects before transferring ownership. After reconciliation reaches a terminal
state, a fresh plan and `operation_resume` may continue eligible work. Recovery
does not automatically repeat a mutating command.

## Resumption and evidence reuse

`operation_resume(target_id, operation_id, plan_id, request_id)` accepts a
`failed` or `cancelled` operation with reconciled effects. It creates a queued
child with `parent_operation_id` identifying the previous attempt. Preparation,
successful operations, running operations and unresolved recovery cannot be
resumed; they return `operation_not_resumable` or `recovery_required`.

The new plan must name the same target and action. Every resumed attempt
requires fresh preparation, including when the intended inputs are unchanged. Execution revalidates permissions, bindings, prerequisites and
resource state exactly as a new plan execution does. A retained successful
stage may become `reused` only when its input fingerprints, engine identities,
artifact digests and documented qualification rules still match. Failed and
incomplete evidence is retained for diagnosis and cannot qualify a stage.

The child writes fresh stage records and artifacts for repeated work. It links
reused evidence to the parent's immutable stage record. Generation changes and
other input changes invalidate dependent stages according to the
[deployment invalidation rules](Deployment.md). An execution never rewinds the
parent's state or overwrites its failed evidence.

## Resource exclusion across entry points

The deployment package owns a coordinator shared by MCP tools and supported
CLI entry points. Target registration resolves aliases to canonical host,
physical GPU, engine, fabric and service identities. A different target name or
instance directory does not permit conflicting use of the same GPU. Each
reservation records `resource_id`, `mode`, `operation_id`, `fence` and
`acquired_at`; v1 reservation `mode` is `exclusive` for mutation or measurement.
Bounded inspections read committed snapshots without reserving deployment
resources, and can therefore observe state while a mutation runs.

Before execution admission, the coordinator atomically reserves the complete
resource set resolved during preparation, in canonical order. A conflict returns `resource_busy` with the owning
operation reference when the caller may inspect it. It does not start a partial
deployment. Within one operation the scheduler may start engines on disjoint
GPU allocations concurrently; overlapping allocations are serialized. Fabric
measurement runs one directed edge at a time. Attestation can run concurrently
after fabric qualification, as specified by the [deployment runbook](../Deploy.md).

| Activity | Required coordination |
| --- | --- |
| Configuration, status, operation and bounded evidence reads | Read the committed snapshot. Reads can run alongside mutations; observation time and partial failures remain visible. |
| Install, launch, replace, stop or cleanup | Exclusive ownership of affected host installation, engine, GPU, port and service resources. |
| Profiling, KV preflight and verification traffic | Exclusive measurement ownership of affected fleet resources; verify idle serving state and control admission for the recorded measurement. |
| Fabric qualification | Fleet-wide measurement exclusion, idle engines and one directed edge at a time. |
| Capacity trial | Exclusive workload ownership; only the recorded trial may introduce traffic. |
| Monitoring startup | Exclusive ownership of its services, ports and configuration; bounded queries remain readable. |

Measurement preparation may inspect a busy fleet. Execution returns
`fleet_busy` before probes when serving traffic or leases violate the action's
idle requirement. A plan that includes draining must describe that mutation and
require its permission. The executor cannot infer permission to interrupt
traffic from a request to profile.

The existing `dev` instance lock continues to protect its lifecycle document.
The proposed coordinator is acquired before that lock, and nested CLI work
inherits a verified operation context to avoid conflicting with its own parent.
Direct supported CLI invocations using the proposed
[`NARWHAL_MANAGEMENT_REGISTRY` binding](Registration.md#registry-changes-and-retention)
enter the same coordinator and receive their own operation identity. V1 requires
one management authority per resource set. This requires changes in the owning
packages during implementation; the current CLI does not provide that
fleet-wide exclusion.

Commands from older installations or manual host changes cannot be prevented by
the coordinator. The adapter must check current process ownership and serving
activity before measurements and effects, stop on a mismatch, and retain the
observation. The guarantee covers cooperating entry points; live precondition
checks identify interference outside that boundary.
