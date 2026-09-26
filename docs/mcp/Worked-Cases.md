# MCP contract worked cases

These **proposed, unreleased version 1 cases** define the observable behaviour
required by [issue #148](https://github.com/athrael-soju/Narwhal/issues/148).
They are specification examples, not measured deployments. The
[tool catalogue](Tools.md), [operation lifecycle](Operations.md) and
[deployment contract](Deployment.md) define the complete field contracts.

The examples use registered target alias `trial-fleet` and these illustrative UUIDs:

| Alias in the prose | UUID |
| --- | --- |
| Preparation request | `10000000-0000-4000-8000-000000000002` |
| Preparation operation | `10000000-0000-4000-8000-000000000003` |
| Original plan | `10000000-0000-4000-8000-000000000004` |
| Execution request | `10000000-0000-4000-8000-000000000005` |
| Execution operation | `10000000-0000-4000-8000-000000000006` |
| Corrected plan | `10000000-0000-4000-8000-000000000007` |
| Resumption request | `10000000-0000-4000-8000-000000000008` |
| Resumed operation | `10000000-0000-4000-8000-000000000009` |

`trial-v1` is an operator-registered deployment recipe with resolved inputs,
finite budgets and a fixed acceptance workload. It is an example identifier,
not a shipped recipe. Each case starts with its stated conditions; the cases
do not describe one continuous deployment.

## 1. Successful deployment

The target permits the deployment action, its recipe and required capabilities.
The agent calls `plan_prepare` with this complete request:

```json
{
  "target_id": "trial-fleet",
  "action": "fleet_deploy",
  "parameters": {"recipe_id": "trial-v1"},
  "request_id": "10000000-0000-4000-8000-000000000002"
}
```

The complete accepted tool result is:

```json
{
  "schema": "narwhal.management-result",
  "schema_version": 1,
  "tool": "plan_prepare",
  "target_id": "trial-fleet",
  "observed_at": "2026-09-26T10:00:00Z",
  "outcome": "accepted",
  "data": {"operation_id": "10000000-0000-4000-8000-000000000003"},
  "errors": [],
  "artifacts": [],
  "command_result": null
}
```

Preparation transitions `queued → running → succeeded`. Its inspected summary
has `result_status: "success"` and `result_data.plan_id` naming the original
plan. The agent calls `plan_inspect` and reads its input/snapshot exports through
`artifact_read` to inspect recorded hosts, GPU allocations and pinned artifacts.
The plan supplies the action, stages and budgets. The agent reads these before
execution. It then calls `plan_execute`:

```json
{
  "target_id": "trial-fleet",
  "plan_id": "10000000-0000-4000-8000-000000000004",
  "request_id": "10000000-0000-4000-8000-000000000005"
}
```

The accepted result contains the execution operation ID. The executor rechecks
the plan and records Gates A through G in order. It may start engines on
disjoint GPUs concurrently, measures fabric one directed edge at a time, and
starts service only after the required profile and preflight evidence passes.

Execution transitions `queued → running → succeeded`, with terminal
`result.status: "success"`. `result.data.summary_artifact_id` identifies the
action result, including the completed gates and retained services. Evidence
includes discovery, source identity, engine launch and live cache captures,
fabric comparisons, attestation, profiles, preflight and the reconciled
workload trial. Gate G leaves the recorded engines, sidecars, router and
monitoring services running. The agent can inspect status and scrape health;
teardown requires a separate cleanup plan.

## 2. Failed gate, correction and resumption

The execution reaches Gate D and one directed sample is below its source
budget. The executor stops at that gate, removes its temporary measurement
listener and records the running engines as retained resources. The operation
transitions `running → failed`, with `result.status: "failed_gate"` and the
underlying gate error retained. The failed sample remains an artifact.

The operator corrects the observed routing fault. Fresh preparation records the
new route fingerprint in the corrected plan. The agent calls
`operation_resume` with this complete request:

```json
{
  "target_id": "trial-fleet",
  "operation_id": "10000000-0000-4000-8000-000000000006",
  "plan_id": "10000000-0000-4000-8000-000000000007",
  "request_id": "10000000-0000-4000-8000-000000000008"
}
```

The accepted response names the resumed operation. The following is a
projection of its stored operation document:

```json
{
  "operation_id": "10000000-0000-4000-8000-000000000009",
  "parent_operation_id": "10000000-0000-4000-8000-000000000006",
  "plan_id": "10000000-0000-4000-8000-000000000007",
  "state": "queued",
  "result": null
}
```

The child reuses only stages whose evidence remains valid, collects a new
sample for the changed route and continues through Gates E to G. Its
transition is `queued → running → succeeded`. The parent stays `failed`.
Both route fingerprints, both samples, the corrected plan and the evidence
reuse decisions remain inspectable.

## 3. Duplicate and conflicting submissions

The agent resends the exact `plan_execute` request from case 1 after losing
its response. The server finds the registered target and execution request
in its deduplication store. It returns `outcome: "accepted"` with the original
execution operation ID. The operation's state does not change and no second
worker starts.

If the agent substitutes the corrected plan ID while keeping the same request
ID, admission returns `request_id_conflict`. This is a request error with no
new operation or external effect. The canonical requests differ in `plan_id`.
The same rule applies if a caller changes the tool or a defaulted action
parameter. A different response `timeout_s` does not change the operation.

Submitting the original plan through the same tool with a new request ID also
returns the existing operation: a plan permits one execution. That new request
key is bound to the same operation. Changing tool or parent/source scope for
an already consumed plan returns `plan_scope_mismatch`.

The existing canonical request and operation evidence remain retained. The
agent may inspect that operation, resend its original request, or submit the
different plan with a new request ID. Deduplication remains valid after the
original operation reaches a terminal state.

## 4. Client disconnect and reconnect

The execution is `running` in Gate F when the client closes its stdio
connection. The durable worker continues within the recorded budgets. Closing
the client does not change operation state.

After reconnecting, the agent calls `operation_inspect`:

```json
{
  "target_id": "trial-fleet",
  "operation_id": "10000000-0000-4000-8000-000000000006"
}
```

The response outcome is `success`; its summary reports the latest committed
state and revision, and `record_artifact_id` identifies the matching complete
snapshot. The agent may observe `running`, or a terminal state reached while it
was disconnected. It can inspect stage evidence through `artifact_read` and
request cancellation if needed. If it lost the operation ID, an identical
submission or `operation_list` recovers it.

## 5. Worker or workstation interruption

The worker dies after issuing an engine launch but before persisting its
completion receipt. On executor startup, the saved worker identity fails the
boot-ID/PID/start-tick check. The operation transitions
`running → recovery_required` and retains its resource exclusions. The
reconciliation scanner checks the adapter's launch intent and live engine
identity under a finite inspection budget.

`operation_inspect` returns a successful tool response containing state
`recovery_required`, null terminal result fields and the recovery error codes.
The full snapshot identifies the unresolved launch and retained evidence. An
unreachable host keeps this state; resubmission does not relaunch the engine.

When the host is reachable and the adapter proves that the intended engine
exists, that its supervisor has stopped the interrupted helper and that no
further action can be issued by the dead worker, reconciliation records the
engine as retained. Because later gates are incomplete, it transitions
`recovery_required → failed` with `result.status: "error"`.
The agent can prepare a fresh plan and resume. If a helper remains active,
explicit `operation_cancel` can request bounded cleanup after matching its
identity. Inspection itself never stops a process.

The retained evidence includes the pre-action intent, process identity,
reconciliation attempts, adapter observations and any missing-receipt
diagnostic. A PID reused by an unrelated process cannot authorize cleanup.

## 6. Cancellation with partial effects

The deployment has completed engine startup and is measuring Gate D. The
agent calls `operation_cancel` for the execution operation using the same
target and operation IDs as case 4. Its result returns the operation summary
and snapshot reference. The transition is `running → cancelling`.

The worker stops further stages, terminates the identified measurement helper
within its cleanup budget, and removes the temporary listener it owns. Once
the adapter confirms these effects, the operation transitions
`cancelling → cancelled` with `result.status: "interrupted"`. Completed
engine launches remain recorded as running resources. Stage logs, partial
samples, cleanup receipts and engine ownership remain retained.

If cleanup cannot prove that a helper stopped, the transition is
`cancelling → recovery_required`. The reservations remain held and the
snapshot names the unresolved resource. Repeated cancellation returns the
current state without starting another cleanup concurrently. After successful
reconciliation the operation becomes `cancelled`.

The agent may prepare a resumption plan for reconciled work, or a
`deployment_cleanup` plan identifying the recorded deployment resources.
Cancellation alone does not promise teardown of completed stages.

## 7. Stale engine generation or changed inputs

After preparation, an engine restarts with the same launch configuration.
`plan_execute` revalidation detects that its live generation differs from the
plan binding. Admission returns `stale_plan` before executing stages. If the
change happens after admission, the queued or running operation fails at the
next precondition check with `result.status: "failed_gate"`; unresolved
external work first requires reconciliation.

Fresh preparation records the new generation. The next execution captures its
live cache, obtains attestation, profiles that generation and runs full
preflight. A changed cache geometry requires a recalculated fabric budget.
Existing directed samples are reusable only when their documented link
fingerprints match and the samples meet the new budget. An engine restart by
itself does not require collecting every directed bandwidth sample again.

Changing the source revision, image, model, recipe or deployment configuration
also invalidates the corresponding plan binding. The agent must inspect and
execute a freshly prepared plan. Old plans, old profiles and invalidation
reasons remain retained; no artifact is overwritten to make it appear current.

## 8. Missing adapter prerequisite

Preparation discovers that the selected adapter lacks the verified asset
bundle required to execute the recipe's monitoring stage. The preparation
operation transitions `queued → running → failed`, with
`result.status: "failed_gate"` and `adapter_prerequisite_missing` identifying
the bundle and required version. No executable plan is returned, and no
installation, launch or monitoring mutation has started.

The action result retains the capability manifest, prerequisite observations
and missing asset reference. The agent can report the prerequisite and repeat
preparation with a new request ID after it is corrected. It cannot interpret
an undeclared source checkout as a replacement for the registered asset bundle.
If a prerequisite disappears after successful preparation, execution
revalidation fails before the first mutation.

## 9. Conflicting entry points and a busy fleet

A supported CLI operation holds the canonical GPU reservation for a local
instance. An MCP `plan_execute` targets the same physical GPU through another
registered alias. Admission returns `resource_busy` and the owner's operation
reference when visible to the caller. No deployment starts. The CLI operation
and its evidence remain available for inspection; a different target name
cannot bypass the reservation.

After that operation completes, preparation of a `fleet_profile` plan succeeds
but the router is serving requests. Execution checks admission, resident work
and transfer leases before sending probes. It returns `fleet_busy`; if already
admitted, the operation becomes `failed` with `result.status: "failed_gate"`.
It retains the observed fleet state and sends no profiling traffic.

The agent may wait for the current owner, select genuinely disjoint resources,
or prepare a permitted maintenance plan that explicitly includes admission
control and drain. It then submits a new request after the idle requirement
is satisfied. During fabric qualification, any second directed measurement
must wait within the same deployment scheduler or fail admission as a
conflicting operation. Exactly one directed edge is measured at a time.
