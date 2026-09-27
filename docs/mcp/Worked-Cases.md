# MCP contract worked cases

These cases specify how the version 1 management executor responds to
successful work, failures, retries and interruptions. The unreleased server
installs `local-dev-v1` and `ssh-v1`. These cases describe the SSH fleet workflow
and the shared operation store. They provide inputs, expected states and retained
evidence for implementation checks; they contain no live fleet measurements.

Use the [tool catalogue](Tools.md) for argument and result fields, the
[operation lifecycle](Operations.md) for state transitions, and the
[deployment contract](Deployment.md) for plan bindings and gate requirements.

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

The illustrative recipe `trial-v1` has resolved inputs, finite budgets and a
fixed acceptance workload. It is not a shipped recipe; an operator would
register the recipe before calling the tools. Each case starts with its own
stated conditions.

## 1. Successful deployment

The operator has registered `trial-fleet` with the deployment action,
`trial-v1` recipe and required capabilities. The agent asks the executor to
prepare a plan:

```json
{
  "target_id": "trial-fleet",
  "action": "fleet_deploy",
  "parameters": {"recipe_id": "trial-v1"},
  "request_id": "10000000-0000-4000-8000-000000000002"
}
```

After persisting the preparation operation, the executor returns this receipt:

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

The executor moves preparation through `queued → running → succeeded`.
`operation_inspect` then returns `result_status: "success"`, with
`result_data.plan_id` identifying the prepared plan.

The agent calls `plan_inspect` to read the action, stages and budgets. It uses
`artifact_read` to read the exported inputs and preparation snapshot, including
the host identities, GPU allocations and pinned artifacts. After reviewing
those inputs, it submits the plan for execution:

```json
{
  "target_id": "trial-fleet",
  "plan_id": "10000000-0000-4000-8000-000000000004",
  "request_id": "10000000-0000-4000-8000-000000000005"
}
```

The executor returns the execution operation ID, rechecks the plan and records
Gates A through G in order. The SSH adapter currently starts engines serially.
It measures one directed fabric edge at a time and starts service only after
the required profile and preflight evidence passes.

The operation moves through `queued → running → succeeded` and finishes with
`result.status: "success"`. The client can read the completed gates and
retained services from the action result identified by
`result.data.summary_artifact_id`.

The executor retains the discovery and source records, engine launch and live
cache captures, fabric comparisons, attestations, profiles and preflight
result. Gate G adds the workload trial and its reconciliation evidence. The
engines, sidecars, router and monitoring services remain running. The agent
can inspect their status and scrape health; teardown requires a separate
cleanup plan.

## 2. Failed gate, correction and resumption

At Gate D, one measured directed link falls below its source budget. The
executor stops at that gate, removes its temporary measurement listener and
records the running engines as retained resources. It changes the operation
from `running` to `failed` and records `result.status: "failed_gate"`. The
underlying gate error and failed sample remain available for inspection.

The operator first prepares and executes `deployment_cleanup` for the failed
operation, removing its retained engines. After correcting the routing fault,
the agent prepares a fresh `fleet_deploy` plan. Preparation requires the old
deployment to be removed before it accepts another deployment. The agent then
asks the executor to resume the failed operation with the new plan:

```json
{
  "target_id": "trial-fleet",
  "operation_id": "10000000-0000-4000-8000-000000000006",
  "plan_id": "10000000-0000-4000-8000-000000000007",
  "request_id": "10000000-0000-4000-8000-000000000008"
}
```

The receipt identifies the new child operation. Its stored document includes
these fields:

```json
{
  "operation_id": "10000000-0000-4000-8000-000000000009",
  "parent_operation_id": "10000000-0000-4000-8000-000000000006",
  "plan_id": "10000000-0000-4000-8000-000000000007",
  "state": "queued",
  "result": null
}
```

The current core runs every stage in the corrected plan again. It collects a
new sample for the changed route and completes Gates A to G with new evidence.
The child operation moves through `queued → running → succeeded`; the parent
stays `failed`.

The client can inspect both route fingerprints, both samples and the corrected
plan. The child's stage records leave `reused_from` null; the parent retains
the evidence from the failed attempt.

## 3. Duplicate and conflicting submissions

The agent loses the response to the `plan_execute` request from case 1 and
resends the same request. The executor finds its target and request ID in the
deduplication store. It returns `outcome: "accepted"` with the original
operation ID. The operation keeps its current state, and the executor does
not start another worker.

If the agent substitutes the corrected plan ID while keeping the same request
ID, the executor returns `request_id_conflict` before creating work. The
canonical requests differ in `plan_id`. The same rule applies if the caller
changes the tool or an action parameter after defaults have been applied.
Changing the response `timeout_s` does not change the requested operation.

If the agent submits the original plan through the same tool with a new request
ID, the executor returns the existing operation and binds the new key to it.
A plan permits one execution. Changing the tool, parent operation or source
operation for a consumed plan returns `plan_scope_mismatch`.

The executor retains the canonical request and operation evidence. The agent
can inspect the operation, resend its original request, or submit a different
plan with a new request ID. Deduplication continues after the original
operation reaches a terminal state.

## 4. Client disconnect and reconnect

The operation is `running` in Gate F when the client closes its stdio
connection. The worker continues within the recorded budgets. The disconnect
does not change the operation's state.

After reconnecting, the agent calls `operation_inspect`:

```json
{
  "target_id": "trial-fleet",
  "operation_id": "10000000-0000-4000-8000-000000000006"
}
```

`operation_inspect` returns `success` with the latest committed state and
revision. Its `record_artifact_id` identifies the complete snapshot used for
that response. The operation may still be `running`, or it may have reached a
terminal state while the client was disconnected.

The agent can inspect stage evidence with `artifact_read` and request
cancellation if needed. If it also lost the operation ID, it can recover it
by repeating the original submission or calling `operation_list`.

## 5. Worker or workstation interruption

The worker dies after issuing an engine launch but before persisting its
completion receipt. On startup, the coordinator compares the saved worker's
boot ID, PID and process start ticks with the live host. The identity check
fails, so it moves the operation from `running` to `recovery_required` and
retains its resource reservations.

The adapter's recovery workflow then calls the core's reconciliation procedure
to inspect the recorded launch intention and live engine identity within a
finite budget. Startup and operation inspection schedule this read-only work
in a detached recovery worker; the frontend checks local worker identity.

`operation_inspect` returns `success`, with state `recovery_required`, null
terminal result fields and the recovery error codes. The full snapshot
identifies the unresolved launch and retained evidence. If the host is
unreachable, the operation stays in that state. Repeating the submission
does not relaunch the engine.

Once the host is reachable, the adapter confirms that the intended engine
exists and that its supervisor stopped the interrupted helper. The coordinator
also confirms that the dead worker cannot issue further actions. It records
the engine as retained and changes the operation from `recovery_required` to
`failed`, with `result.status: "error"`, because later gates remain incomplete.

The agent can prepare cleanup for the failed deployment, then prepare a new
deployment plan and resume. A remote helper may continue until its recorded
deadline after its management worker disappears. `operation_cancel` can stop
verified local helpers; explicit `deployment_cleanup` handles selected remote
resources. Inspection itself never stops a process.

The executor retains the intention recorded before launch, the process
identity, reconciliation attempts and adapter observations. It also retains
any diagnostic reporting the missing receipt. A PID reused by an unrelated
process cannot authorise cleanup.

## 6. Cancellation with partial effects

The executor has started the engines and is measuring Gate D. The agent calls
`operation_cancel` using the target and operation IDs from case 4. The tool
returns the operation summary and snapshot reference, and the operation moves
from `running` to `cancelling`.

The worker stops starting stages, terminates the identified measurement helper
within its cleanup budget, and removes its temporary listener. After the
adapter confirms those effects, the executor moves the operation from
`cancelling` to `cancelled` and records `result.status: "interrupted"`.

The engines started by completed stages remain running. The executor retains
their ownership records together with stage logs, partial samples and cleanup
receipts.

If the adapter cannot prove that a helper stopped, the executor moves the
operation from `cancelling` to `recovery_required`. It keeps the reservations
and identifies the unresolved resource in the snapshot. Repeating cancellation
returns the current state without starting another cleanup concurrently.
After reconciliation establishes that the helper stopped and its effects are
known, the operation becomes `cancelled`.

The agent can prepare a new plan to resume reconciled work after satisfying
that action's prerequisites. Repeating `fleet_deploy` first requires cleanup
of its retained deployment. To remove the
resources retained from completed stages, it must prepare a
`deployment_cleanup` plan that identifies them. Cancellation alone leaves
those resources in place.

## 7. Stale engine generation or changed inputs

After preparation, an engine restarts with the same launch configuration.
The `plan_execute` call checks retained inputs and the local binding, reserves
resources and returns an accepted operation. Before performing the first
stage, the worker detects that the live generation differs from the plan
binding. It records `stale_plan` and finishes with
`result.status: "failed_gate"` without performing that stage.

If the generation changes during execution, the next precondition check fails.
The operation reaches the same failed result after reconciliation, if any
external work remains unresolved.

The agent inspects the ownership mismatch before preparing more work. The
adapter does not adopt an unrecorded replacement process. The operator must
resolve that process and clean up the affected deployment before preparing
a new fleet deployment. A planned replacement of a still-owned engine uses
`engine_replace`, which refreshes cache, fabric, attestation, profiles and
preflight evidence.

The current core runs the new plan's stages again. A future fleet adapter may
reuse a directed sample only when its documented link fingerprint still
matches and the sample meets the new budget, following the
[evidence reuse contract](Operations.md#resumption-and-evidence-reuse).

Changes to the source revision, image, model, recipe or deployment
configuration also invalidate the corresponding plan binding. The agent must
inspect and execute a freshly prepared plan. The executor preserves the old
plans and profiles together with the reasons they became invalid.

## 8. Missing adapter prerequisite

During preparation, the adapter finds that its verified asset bundle is
missing. The recipe needs that bundle for its monitoring stage. The executor
moves the preparation operation through `queued → running → failed` and
records `result.status: "failed_gate"`. The `prerequisite_failed`
error records the missing or invalid source prerequisite.

The executor returns no executable plan. It has not installed software,
launched services or changed monitoring.

The operation retains its preparation error and any evidence collected before
the failed prerequisite. After the operator supplies the required bundle,
the agent can repeat preparation with a new request ID. An undeclared source
checkout cannot satisfy the registered bundle requirement.

If a prerequisite disappears after successful preparation, the executor's
revalidation fails before it makes the first change.

## 9. Conflicting entry points and a busy fleet

A supported CLI operation holds a GPU reservation for a local instance. The
agent calls MCP `plan_execute` through another registered alias that resolves
to the same physical GPU. The coordinator returns `resource_busy` before
starting the deployment. The caller can use `operation_list` and
`operation_inspect` to examine operations its registration permits.

The CLI operation and its evidence remain available for inspection. Selecting
a different target name cannot bypass the reservation.

After the CLI operation completes, the agent prepares a `fleet_profile` plan
while the router is serving requests. Preparation succeeds. Before sending
probes, the SSH idle gate checks each engine's running and waiting request
counters. Nonzero counters fail the gate before profiling starts. The accepted
operation becomes `failed` with `result.status: "failed_gate"` and
`command_failed`; the operation
and gate evidence retain the failure. Operators must keep client traffic
stopped because this observation does not prevent later requests.

The agent can wait for the current owner, select resources that do not overlap,
or stop client traffic and wait for the engines to become idle. Once the
engines satisfy the idle requirement, it submits a new request.

During fabric qualification, the deployment scheduler measures exactly one
directed edge at a time. A second measurement in that deployment waits for
the first. A conflicting operation submitted separately fails admission.
