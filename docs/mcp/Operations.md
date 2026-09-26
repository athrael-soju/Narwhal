# MCP operation lifecycle

The management executor records long operations so that a client can inspect
their progress, reconnect after a disconnect, and recover interrupted work.
This page defines the proposed version 1 operation contract for
[MCP fleet operations](../MCP-Contracts.md). The operation store and shared
coordinator are not yet available; the existing stage runner and dev lifecycle
provide process evidence and local locks for their own commands.

## Submission and lookup

When a client calls `plan_prepare`, `plan_execute` or `operation_resume`, the
executor persists an operation before returning `outcome: "accepted"`. This
receipt confirms that the executor accepted and recorded the work. Call
`operation_inspect` with the returned operation ID to check its progress and
eventual result.

`operation_inspect(target_id, operation_id)` returns a bounded summary in
`data.operation`. It also returns `record_artifact_id`, which identifies a
frozen, redacted copy of the complete operation document. The tool reports
`success` when it reads the document, including when the operation itself
failed. Read the summary's `state` and `result_status` to distinguish these
outcomes. Use `artifact_read` to read the complete document in bounded chunks.

`operation_list(target_id, cursor, limit)` returns summaries and an opaque
`next_cursor`. The default limit is 20; accepted limits are integers from 1 to
100. The executor captures one snapshot for the list and orders its operations
by creation time, newest first, using the operation ID to break ties. The cursor
identifies that target and snapshot. An invalid cursor returns `invalid_input`.
A client that loses its submission response can use this list to recover the
operation ID.

Each summary contains
`operation_id`, `parent_operation_id`, `action`, `plan_id`, `state`,
`current_stage`, `revision`, `created_at`, `updated_at`, `finished_at`,
`result_status`, `result_data`, `error_codes`, `error_count`, `stage_count` and
`resource_count`. Before the operation reaches a terminal state, `result_status`
and `result_data` are null. The summary includes the first ten error codes in
`error_codes` and the total number in `error_count`.

Successful preparation returns `result_data: {"plan_id": "UUID"}`. Execution
and resumption return `{"summary_artifact_id": "UUID"}`, which identifies the
complete action result.

## Operation document

The executor stores operations as `narwhal.management-operation`, version `1`.
Every field below is required. A field accepts JSON `null` only where its entry
allows it. Operation, request, plan and artifact IDs are UUID strings;
registered target IDs are aliases. Timestamps are RFC 3339 UTC strings, and
plan budgets are integer milliseconds.

Readers retain unknown compatible fields. The executor rejects an unsupported
schema version before executing or recovering work.

| Field | Type and meaning |
| --- | --- |
| `schema`, `schema_version` | Literal `narwhal.management-operation`, integer `1`. |
| `operation_id` | The executor assigns this UUID atomically when it accepts the operation and never reuses it. |
| `target_id`, `request_id` | Registered target and caller's deduplication UUID. |
| `parent_operation_id` | Source operation for resumption; otherwise null. |
| `tool` | `plan_prepare`, `plan_execute` or `operation_resume`. |
| `action` | Action from the [tool catalogue](Tools.md). |
| `plan_id`, `plan_digest` | Executed plan UUID and SHA-256 digest; both null for preparation. Preparation returns its new plan identity in `result.data`. |
| `state` | One of the states in the transition table below. |
| `revision` | A positive integer that the executor increments whenever it commits a record change. |
| `created_at`, `updated_at` | Admission and latest committed update times. |
| `started_at`, `finished_at` | First execution and terminal times; null until reached. |
| `deadline_at` | Overall execution deadline; null while queued. Preparation uses its persisted preparation budgets. |
| `preparation_budgets` | The target's `preparation` object, with defaults applied. The executor stores it before starting preparation; execution and resumption use null. The overall preparation budget is the sum of its action and cleanup budgets. |
| `current_stage` | Stage ID from the plan, or null before execution and after terminal completion. Preparation uses `discovery`. |
| `stages` | Ordered records in the format below. |
| `resources` | Reservation records for canonical resources and current ownership. |
| `worker` | Worker identity, or null while unassigned. Contains `worker_id`, `host_id`, `boot_id`, `pid`, `start_ticks`, `heartbeat_at` and `fence`. |
| `cancellation` | Object with nullable `requested_at` and `reason`; initially both null. |
| `recovery` | Object with nullable `reason` and `observed_at`, plus arrays `errors`, `required_actions` and `artifacts`; initially nulls and empty arrays. |
| `result` | Terminal result below; null until terminal. |
| `artifacts` | All retained evidence references currently available to the caller. |

`worker_id` and `boot_id` are UUIDs. The worker's `host_id` identifies the
management host by its stable alias; `pid` and `start_ticks` are positive
integers. The coordinator assigns a monotonically increasing integer to
`fence`. Before starting a stage or causing an external effect, the worker must
hold the current fence. When ownership expires, the coordinator still has to
check whether remote work stopped.

Each stage record contains `stage_id`, `state`, `started_at`, `finished_at`,
`command_result`, `artifacts`, `effects` and `reused_from`. A stage's state is
`pending`, `running`, `succeeded`, `failed`, `cancelled` or `reused`.
`command_result` holds the original versioned command envelope when a stage
invokes a command. Stages performed entirely by an adapter use null.
`reused_from` is null or an object with `operation_id`, `stage_id` and the
verified evidence digests. A plan must list all required work. The executor
cannot report success for a required gate that the plan omitted.

The stage's `effects` array contains adapter receipts. Each receipt identifies
a created or changed resource, records its owner, and reports the effect as
`confirmed`, `absent` or `unknown`, following the
[site adapter contract](Deployment.md). Before invoking a command, the executor
persists the intended effect. After the command finishes, it persists the
receipt, result and evidence before advancing to another stage.

A terminal result has these fields:

| Field | Type and meaning |
| --- | --- |
| `status` | `success`, `degraded`, `failed_gate`, `invalid_input`, `error` or `interrupted`, preserving the [command outcome meanings](../Command-Results.md). |
| `data` | Successful preparation contains `plan_id`. Other terminal outcomes contain `summary_artifact_id`, identifying the complete retained action result. |
| `errors` | Structured errors, preserving underlying command codes and context. |
| `artifacts` | References to retained successful and failed-attempt evidence. |
| `command_result` | Last underlying `narwhal.command-result`, or null when none exists. Every earlier command result remains in its stage record. |

An operation in `succeeded` has result status `success` or `degraded`.
A deployment cannot succeed when it skipped a required gate, even if the
underlying command returned `degraded`. An operation in `failed` has result
status `failed_gate`, `invalid_input` or `error`; an operation in `cancelled`
has result status `interrupted`.

The executor may leave engines or services running when it records a terminal
result, provided it knows their identities and ownership. If it cannot
establish an effect, it records `recovery_required` and leaves `result: null`.

## State transitions

The executor must use the transitions below. The states `succeeded`, `failed`
and `cancelled` are terminal. Resuming work creates a separate operation.

When the executor commits a terminal result, it releases the operation's
execution reservations. It retains ownership records for services left
running. Later operations must check those services before measuring or
changing the deployment.

| From | To | Required condition |
| --- | --- | --- |
| Admission | `queued` | The executor commits the request, operation and applicable reservations together, before any external effect. Preparation does not reserve deployment resources. |
| `queued` | `running` | The worker has acquired ownership, rechecked permissions and the plan binding, and established the required resource exclusions. |
| `queued` | `failed` | The executor rejects a permission, binding, prerequisite or deadline check before execution. |
| `queued` | `cancelled` | The executor records cancellation before a worker starts. |
| `running` | `succeeded` | The executor accepts every required stage and commits the final evidence and inventory of remaining resources. |
| `running` | `failed` | A gate, input check or action fails. The executor has stopped active helpers and established their effects. |
| `running` | `cancelling` | The client requests cancellation, or the worker handles an interrupt. |
| `running` | `recovery_required` | The executor loses the worker, cannot establish ownership or an effect, or cannot complete cleanup. |
| `cancelling` | `cancelled` | The executor has stopped current work, reconciled temporary resources and reported retained resources. |
| `cancelling` | `recovery_required` | Cleanup exceeds its budget, or the executor cannot establish a process's identity or an action's effect. |
| `recovery_required` | `succeeded` | Reconciliation proves that every required stage completed and the retained evidence qualifies the result. |
| `recovery_required` | `failed` | Reconciliation establishes the effects and confirms that helpers stopped. Required work remains incomplete and cancellation was not requested. |
| `recovery_required` | `cancelled` | After cancellation, reconciliation establishes the effects and confirms that helpers stopped. |

An operation in `recovery_required` retains its reservations. The executor
records each reconciliation attempt and stops it at its recorded deadline.
When it regains connectivity to the worker or host, it retries inspection.
Clients can continue to use inspection tools while recovery is unresolved.
Retrying a plan, changing a target alias or deleting a lock file cannot clear
the operation's reservation.

## Duplicate submissions

The executor identifies a submission by
`(registry_id, target_id, request_id)`. Before accepting it, the executor
validates its types, applies documented defaults and sorts object keys to
produce a canonical request. It records that request and its operation in one
transaction.

The canonical request includes the tool name, action, normalised parameters
and plan identity where applicable. It excludes `timeout_s`, which limits the
tool response without changing the requested work. When checking a duplicate,
the executor compares the request hashes and then the canonical requests
themselves.

For an identical submission, the executor returns `accepted` with the original
operation ID, whether that operation is queued, active or terminal. It checks
the caller's authorisation before returning the record. It does not create
another worker or repeat a stage.

If the canonical request differs but uses the same key, the executor returns
`request_id_conflict` without creating work. The key does not expire while its
record is retained. A client must use a new request ID for a new attempt or
changed input.

A plan permits one execution. If a client submits it again through the same
tool, with the same parent or source operation where applicable, the executor
returns the existing operation. A new request ID is bound to that operation.
Changing the tool, parent operation or source operation returns
`plan_scope_mismatch`.

`operation_resume` requires a freshly prepared plan that no execution has
consumed. If the client repeats an identical resume request, deduplication
returns the child operation created by the original request.

The store retains operations, deduplication records and evidence indefinitely
by default. If the operator removes an operation record, cleanup must preserve
a tombstone containing its key, canonical request, request digest and original
operation ID. A later retry returns `operation_record_removed` and cannot
execute again. Removed evidence cannot qualify work for resumption.

## Deadlines and cancellation

The plan records the overall operation budget and each stage's action and
cleanup budgets. Every duration must be finite and positive. The overall
deadline starts when the worker begins execution. The executor reserves the
required resources when it accepts execution; it does not queue work behind a
conflicting operation. Before starting each stage, the worker checks that
enough time remains for its action and cleanup budgets. If those budgets do
not fit, the operation fails before that stage starts.

The action budget limits the command or adapter call. Cleanup has separate
budgets for graceful and forced termination, following the existing
[stage deadline and recovery behaviour](../Dev-Runtime.md#stage-deadlines-and-recovery).
When a deadline expires, the executor records `stage_timeout` or
`operation_timeout`. It can mark the operation `failed` only after establishing
the effects and completing cleanup. The tool response timeout limits submission
or inspection without changing these execution budgets.

`operation_cancel` records the cancellation request and returns the current
bounded summary and snapshot artifact. Before requesting cleanup, the executor
rechecks the original action's capabilities and allowlist. Cancelling
preparation requires inspection permission. Repeating a cancellation has no
additional effect; cancelling a terminal operation returns its terminal
snapshot.

When the worker receives cancellation, it stops starting stages and signals
only helpers whose identities it has verified. It uses the active stage's
recorded cleanup budget. An explicit cancellation can also request cleanup
for an operation in `recovery_required`, after verifying the affected
identities. That operation remains in `recovery_required` until the executor
establishes the effects.

Cancellation leaves engines or services retained by completed stages in place.
The result lists those resources and links to their evidence.

An underlying action may perform its own cleanup. For example, when the
current `dev up` command fails, it attempts to stop the instance's owned
processes. The executor records what that cleanup did. Resources present
before cancellation may therefore have stopped as part of the action.

To tear down retained resources, prepare a `dev_down` or `deployment_cleanup`
plan. The executor limits teardown to the plan's recorded ownership scope,
preserves evidence, and rejects any resource whose identity no longer matches
the deployment's ownership receipts.

## Disconnect, interruption and recovery

The worker runs independently of the stdio MCP process. Closing the client
connection leaves accepted work running. Cancelling an MCP request before the
executor accepts it prevents that work from starting. After the executor
records the operation, the client must use `operation_cancel` to stop it. If
the client loses its submission response, it can repeat the request with the
same key or use `operation_list` to recover the operation ID.

On startup, the coordinator reads the stored ownership records before accepting
conflicting work. If the recorded worker is alive and its identity matches,
it keeps the operation. If the worker is dead or its identity differs, the
coordinator marks active work `recovery_required`.

For local processes, the coordinator compares the host boot ID, PID and process
start ticks. For remote services, it uses the adapter's resource identities.
A reused PID or a different engine generation cannot establish ownership.

During reconciliation, the coordinator inspects recorded intentions, receipts,
helper identities and live state. It performs bounded, read-only checks on
startup and when connectivity returns. Reading an operation never starts
cleanup. Explicit cancellation can request cleanup of identified temporary
helpers under the action's current authorisation.

Before repeating an interrupted installation or launch, the coordinator must
establish whether the original action took effect. A remote action may have
completed before its receipt was written to disk. If a host remains unreachable
or the available evidence cannot establish identity, the operation stays in
`recovery_required`.

Before transferring ownership, the coordinator fences the previous worker and
confirms that it cannot cause further effects. Once reconciliation reaches a
terminal state, the client can prepare a new plan and call `operation_resume`
to continue eligible work. Recovery does not automatically repeat a command
that changes the deployment.

## Resumption and evidence reuse

`operation_resume(target_id, operation_id, plan_id, request_id)` accepts a
`failed` or `cancelled` operation whose effects have been reconciled. It creates
a queued child operation with `parent_operation_id` identifying the previous
attempt. The executor rejects preparation, successful or running operations,
and operations that still require recovery. It returns
`operation_not_resumable` or `recovery_required` for those requests.

The new plan must name the same target and action. Every resumed attempt
requires fresh preparation, including when the intended inputs have not
changed. The executor rechecks permissions, plan bindings, prerequisites and
resource state using the same checks as a new execution.

The executor may mark a completed stage `reused` only when its input
fingerprints, engine identities, artifact digests and documented qualification
rules still match. It retains failed and incomplete evidence for diagnosis;
that evidence cannot qualify a stage.

The child operation writes new stage records and artifacts for work it repeats.
For reused work, it links the evidence to the parent's immutable stage record.
If an engine generation or another input changes, the executor invalidates
dependent stages according to the [deployment invalidation rules](Deployment.md).
The parent's state and failed evidence remain unchanged.

## Resource exclusion across entry points

MCP tools and supported CLI entry points share the deployment package's
coordinator. The registry resolves their target aliases to canonical host,
physical GPU, engine, fabric and service identities. The coordinator therefore
detects conflicting use of a GPU even when callers select different target
names or instance directories.

Each reservation records `resource_id`, `mode`, `operation_id`, `fence` and
`acquired_at`. Version 1 uses reservation mode `exclusive` for measurements and
changes to a deployment. Inspection tools read committed snapshots without
reserving deployment resources, so clients can inspect state while a change
runs.

Before accepting execution, the coordinator atomically reserves all resources
resolved during preparation, in canonical order. If a resource is already
reserved, it returns `resource_busy` before starting any part of the deployment.
The error includes the owning operation's reference when the caller may
inspect it.

Within one operation, the scheduler may start engines concurrently when their
GPU allocations are disjoint. It serialises work on overlapping allocations
and measures one directed fabric edge at a time. After fabric qualification,
it may attest engines concurrently, as specified by the
[deployment runbook](../Deploy.md).

| Activity | Required coordination |
| --- | --- |
| Configuration, status, operation and bounded evidence reads | The reader uses a committed snapshot and reports its observation time and partial failures. Reads may run alongside changes. |
| Install, launch, replace, stop or cleanup | The executor reserves the affected host installation, engines, GPUs, ports and services exclusively. |
| Profiling, KV preflight and verification traffic | The executor reserves the affected fleet resources for measurement, verifies that serving is idle and controls admission for the recorded measurement. |
| Fabric qualification | The executor excludes other fleet measurements, verifies idle engines and measures one directed edge at a time. |
| Capacity trial | The executor reserves the workload exclusively. Only the recorded trial may introduce traffic. |
| Monitoring startup | The executor reserves the monitoring services, ports and configuration exclusively. Clients may continue bounded queries. |

Preparation may inspect a busy fleet. Before sending probes, the executor
checks whether serving traffic or leases violate the action's requirement for
idle engines. If they do, it returns `fleet_busy`. A plan that drains traffic
must describe that change and require the corresponding permission. A request
to profile does not itself authorise the executor to interrupt traffic.

The existing `dev` instance lock continues to protect its lifecycle document.
The executor acquires the coordinator before that lock. CLI commands invoked
within an operation inherit a verified operation context and use their
parent's reservation. Supported CLI invocations
using the
[`NARWHAL_MANAGEMENT_REGISTRY` binding](Registration.md#registry-changes-and-retention)
enter the same coordinator and receive their own operation identity. Version 1
requires one management authority for each resource set.

The coordinator cannot prevent commands from older installations or manual
changes on a host. Before measurements or changes, the adapter must check
current process ownership and serving activity. If they differ from the
expected state, it stops and retains the observation. The coordinator's
exclusion guarantee covers cooperating entry points; live checks can detect
interference from other callers.
