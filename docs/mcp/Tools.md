# Tools and results (proposed)

This is the planned tool catalogue for the [MCP contract](../MCP-Contracts.md).
These names and schemas specify implementation work; the current installed
package does not expose them. Registration and plan preparation resolve the
paths and launch inputs before any execution tool runs.

## Common argument rules

All tool arguments are JSON objects with unknown properties rejected.
`Alias` and `UUID` use the [registration types](Registration.md#target-entry).
`Timestamp` is an RFC 3339 UTC string ending in `Z`. Booleans are not accepted
as integers; numeric inputs must be finite. Names are case-sensitive.

Every target tool requires `target_id: Alias`. `target_list` is the only
exception. IDs passed to a tool must belong to that target and registry.
A missing target, object, capability or action grant rejects the call before
remote access or work submission. Preparation, execution and resumption
require `request_id: UUID` for [deduplication](Operations.md).

All tools accept optional `timeout_s: integer` in `1..30`. The default is
`30` for synchronous calls and `5` for operation admission. This bounds the
tool exchange; the recorded plan owns long-work deadlines. If admission times
out after persistence, the client repeats the same request ID to recover its
receipt. A tool timeout does not cancel an accepted operation.

`cursor` is an optional opaque string or `null`, default `null`.
Paginated results use immutable snapshots and return
`next_cursor: string|null`. A cursor binds target, filters and snapshot;
changing those inputs returns `invalid_cursor`. Cursors remain valid while
their retained snapshot exists. `limit` is integer `1..100`, default `20`.
Target listing orders by target ID; operation listing orders by creation time
descending and then operation ID ascending.

The maximum serialized `structuredContent` is 262,144 bytes. Oversized
results retain a complete redacted result artifact and return its reference,
as described under [Artifact references and bounds](#artifact-references-and-bounds).

## Tool catalogue

The argument column lists fields beyond the common rules. `{}` means no
additional arguments. All nonoptional fields are required. Every row requires
`inspect`; additional capabilities appear in the execution column.

| Tool | Additional arguments | Execution and authority | Successful `data` | Underlying owner/interface |
| --- | --- | --- | --- | --- |
| `target_list` | `cursor?`, `limit?` | Synchronous, permitted targets only | `{targets: TargetSummary[], next_cursor}` | Deployment registry |
| `config_inspect` | `{}` | Synchronous | Effective-config document | `narwhal config inspect --format json`, `config/` |
| `config_validate` | `{}` | Synchronous, offline | Effective-config document | `narwhal config validate --format json`, `config/` |
| `fleet_status` | `{}` | Synchronous | `{sources: Observation[]}` | Router GETs `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`; `serving/` and `runtime/` |
| `dev_status` | `{}` | Synchronous; dev target | Existing dev lifecycle state | `narwhal dev status --format json`, `dev/` |
| `diagnostics_collect` | `include_request_content?: boolean=false` | Synchronous, bounded artifact writes | `{bundle_artifact_id: UUID, manifest_artifact_id: UUID, collection_status: success\|partial, source_count: integer}` | `narwhal diagnostics collect --format json`, `diagnostics/` |
| `plan_prepare` | `action: Action`, `parameters: ActionParameters`, `request_id` | Asynchronous inspection; also checks the action's grants | `{operation_id: UUID}` | `deployment/` preparation and site discovery |
| `plan_inspect` | `plan_id: UUID` | Synchronous | `{plan: DeploymentPlan, locally_stale: boolean, input_artifacts: [{name: string, artifact_id: UUID}], snapshot_artifact_id: UUID}` | Redacted plan/input store; live revalidation occurs before execution |
| `plan_execute` | `plan_id: UUID`, `request_id` | Asynchronous; capabilities for recorded action | `{operation_id: UUID}` | Shared executor, delegated CLI/site actions |
| `operation_list` | `cursor?`, `limit?` | Synchronous | `{operations: OperationSummary[], next_cursor}` | Operation store |
| `operation_inspect` | `operation_id: UUID` | Synchronous | `{operation: OperationSummary, record_artifact_id: UUID}` | Operation store and frozen redacted record |
| `operation_cancel` | `operation_id: UUID` | Synchronous request for bounded cleanup; execution grants, or inspect for preparation | `{operation: OperationSummary, record_artifact_id: UUID}` | Executor cancellation; response does not establish cleanup completion |
| `operation_resume` | `operation_id: UUID`, `plan_id: UUID`, `request_id` | Asynchronous; original action's current grants | `{operation_id: UUID}` for a new child attempt | Executor reconciliation and checkpoint validation |
| `monitoring_status` | `{}` | Synchronous | `{sources: Observation[], readiness: pass\|fail\|unknown}` | Prometheus target inspection, Grafana verification and router readiness; site adapter |
| `metrics_query` | `query_id: Alias`, `start?: Timestamp`, `end?: Timestamp`, `step_s?: integer=15`, `limit_series?: integer=100` | Synchronous, fixed registered query | `{result_type: string, series: MetricSeries[], complete: boolean, observed_at: Timestamp}` | Prometheus query API through registered endpoint |
| `host_inventory` | `host_id: Alias` | Synchronous, registered host | `{snapshot_artifact_id: UUID, observed_at: Timestamp, complete: boolean}` | Adapter's bounded hardware/runtime inventory |
| `host_logs` | `log_id: Alias`, `max_bytes?: integer=65536` | Synchronous, selected regular file | `{artifact_id: UUID, bytes: integer, complete: boolean, observed_at: Timestamp}` | Site-filtered source copied by adapter |
| `artifact_read` | `artifact_id: UUID`, `offset?: integer=0`, `max_bytes?: integer=65536` | Synchronous, authorised immutable export | `{artifact_id, sha256: string, text: string, offset: integer, next_offset: integer|null, complete: boolean}` | Bounded artifact store read |

`TargetSummary` contains `id`, `kind`, `capabilities`, `actions`, `adapter_id`,
`recipes: [{id, kind}]`, `query_ids`, `log_ids` and `host_ids` (arrays of
aliases). It excludes credentials, environment values, source paths and SSH
destinations. Hosts are the registered aliases; live identity still requires
discovery. The [operation page](Operations.md) defines `OperationSummary`
and the stored operation schema.

`Observation` contains `source: string` (registered alias or route),
`observed_at: Timestamp`, `status: ok|unavailable|timeout|truncated|stale|error`,
`http_status: integer|null`, `data: object|string|null`, and
`artifact_id: UUID|null`. An HTTP 200 from `/health` does not prove admission
readiness. Preserve the endpoint's status/body and distinguish observed
unhealthy service state from a failed observation. Missing required sources
produce a degraded result; a monitoring readiness failure is reported as
`readiness: fail` even when collection itself succeeds.

### Plan actions

The `action` discriminator selects exactly one parameter object below.
Parameters have no omission defaults except where listed. The plan stores
the default-populated object. A target's `actions` allowlist must contain
the selected action, in addition to every listed capability.

| Action | Target | `parameters` | Additional capabilities | Execution interface and completion |
| --- | --- | --- | --- | --- |
| `dev_init` | dev | `{recipe_id: Alias}` | mutate | Registered dev recipe to `narwhal dev init --format json`; created or matching reused instance |
| `dev_up` | dev | `{}` | measure, mutate | `narwhal dev up --format json`; launched state, without claiming verification |
| `dev_verify` | dev | `{}` | measure | `narwhal dev verify --format json`; complete preflight, directed KV and routed verification |
| `dev_down` | dev | `{}` | mutate | `narwhal dev down --format json`; owned teardown and stopped state |
| `fleet_deploy` | fleet | `{recipe_id: Alias}` | measure, mutate | Adapter executes Gates A–G including full workload acceptance |
| `fleet_profile` | fleet | `{engine_ids?: Alias[]=all configured engines}` | measure | `narwhal-profile --format json`; measurements bound to current processes |
| `fleet_preflight` | fleet | `{}` | measure | Full `narwhal-check --format json`; skipped required gates cannot pass |
| `engine_replace` | fleet | `{engine_id: Alias}` | measure, mutate | Adapter supervision plus documented router drain/readmission and required requalification |
| `monitoring_start` | fleet | `{}` | mutate | Existing monitoring startup through adapter; full monitoring readiness contract |
| `deployment_cleanup` | fleet | `{operation_id: UUID}` | mutate | Recorded resources from one inactive deployment operation, with ownership reconciliation |

An explicit `engine_ids` array has 1–256 unique registered engine aliases;
the default selection is resolved and frozen during preparation. V1 engine
replacement uses the recorded launch specification for that engine and
honours its configured restart policy. Model migrations and arbitrary launch
overrides require a separately prepared supported deployment.

Every execution action produces a terminal operation result with
`data: {summary_artifact_id: UUID}`. The summary artifact contains the
action's final underlying result, verified postconditions, stage/evidence
references and remaining resources. Successful preparation instead produces
`data: {plan_id: UUID}`. These small result shapes keep status responses
bounded; command results remain inspectable in the full operation record.

### Collection and query limits

Diagnostics use the registered fleet/instance selection, a fresh private
output directory and the existing 5-second per-source and 30-second overall
defaults. `timeout_s` may lower the overall budget. The source retention limit
is 8,388,608 bytes. `include_request_content: true` requires the matching
registry permission; false remains the default. Arbitrary `--artifact` paths
and automatic request-content inclusion are not exposed.

Each status source has at most five seconds within the overall call deadline.
A source body is capped at 262,144 bytes; larger bodies become bounded
artifacts and produce a truncated observation. Snapshot age is compared with
the target's `freshness_s`; an old capture cannot prove present readiness.

For instant metric queries, omit `start`, `end` and `step_s`. For range
queries, `start` and `end` are required, `start < end`, the window is at most
3600 seconds, and `step_s` is in `1..300`. Query future times are rejected.
`limit_series` is in `1..100`. At most 10,000 samples are returned per call.
`MetricSeries` is `{labels: object<string,string>, samples: [{at: Timestamp,
value: string}]}`. Prometheus numeric strings, including nonfinite sample
strings, are preserved as data and do not qualify a measurement. Result
warnings, truncated series/samples or stale/missing required observations
produce `outcome: degraded`, with `complete: false` where data is incomplete.
The executor uses a Prometheus timeout bounded by `timeout_s` and bounds
response bytes during streaming. Registered queries cannot contain runtime
substitutions supplied by the agent.

`host_inventory` retains at most 1,048,576 bytes and reports source failures.
`host_logs.max_bytes` and `artifact_read.max_bytes` are in `1..65536`.
Host logs capture the last requested bytes of the selected regular file into
a frozen export; concurrent log changes do not change an artifact already
returned. A shortened source reports `complete: false`. Unsupported host
utilities produce an explicit incomplete inventory, not inferred hardware.

## Result envelope

Every executed tool returns `narwhal.management-result` version `1` in MCP
`structuredContent`, and the same serialized object in one text content block.
The server publishes an `outputSchema` for this object. This follows the
[MCP structured-result and tool-error interfaces](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
Protocol-malformed requests and unknown tool names use JSON-RPC errors;
valid calls with invalid domain arguments return structured tool errors.

| Field | Type and contract |
| --- | --- |
| `schema`, `schema_version` | Literal `narwhal.management-result`, integer `1`. |
| `tool` | Exact catalogue name. |
| `target_id` | Alias, or `null` for target listing/arguments that did not identify a valid target. |
| `observed_at` | Timestamp of the response snapshot. |
| `outcome` | `success`, `accepted`, `degraded`, `failed_gate`, `invalid_input`, `error` or `interrupted`. |
| `data` | Tool-specific object from the catalogue; `{}` when unavailable. Oversize-result fallback is defined below. |
| `errors` | Array of `{code, message, context}`. Code/message are strings; context is a redacted object with affected IDs, field/stage and evidence references when available. |
| `artifacts` | Array of exported artifact references as defined below. |
| `command_result` | Original versioned command result after existing content/credential redaction, or `null` when the tool has no such result or it must be read from an artifact. |

Preserve the command's schema, status, exit code, error codes and diagnostic
context. Absolute paths in its artifact metadata are informational; they do
not authorise arbitrary path reads. Top-level export references use artifact
IDs. HTTP-backed results retain route/status information instead of inventing
a CLI exit code. Command prose is diagnostic text; callers branch on codes.

| Tool outcome | MCP `isError` | Meaning |
| --- | --- | --- |
| `success` | false | This tool completed. Polling a failed operation can succeed; inspect its `result_status`. |
| `accepted` | false | The operation was persisted or the existing submission recovered. It has not necessarily succeeded. |
| `degraded` | false | Usable partial result or existing degraded command outcome; inspect errors and completeness. |
| `failed_gate` | true | Required evidence or a precondition failed. |
| `invalid_input` | true | Input, registry binding, document version or domain validation rejected the call. |
| `error` | true | Operational failure prevented the requested tool result. |
| `interrupted` | true | A synchronous operation was interrupted. Durable cancellation is read through operation state. |

Existing CLI outcomes map by identical name. `accepted` is new and has no
CLI exit-code equivalent. A terminal operation stores the command outcome
separately from the outcome of an inspection call. A degraded required gate
cannot advance a deployment; the deployment result becomes `failed_gate`.

### Management error codes

These codes supplement the existing command-result codes, which retain their
meaning. Admission returns no operation ID on rejection unless a previously
persisted submission is being recovered. Failures after admission appear in
the operation record and its terminal result.

| Code | Tool outcome | Recovery |
| --- | --- | --- |
| `target_not_found`, `object_not_found` | invalid_input | Select a registered target and its own object IDs. |
| `unsupported_contract`, `invalid_cursor` | invalid_input | Use compatible documents or restart listing. |
| `permission_denied` | invalid_input | Operator updates the applicable local grant; the tool cannot change it. |
| `request_id_conflict`, `plan_scope_mismatch` | invalid_input | Correct the mismatched submission; use a new request ID only for new work. |
| `operation_record_removed` | error | Inspect the retained tombstone/archive; do not replay the old request. |
| `stale_plan`, `adapter_prerequisite_missing` | failed_gate | Correct inputs/prerequisites and prepare a new plan. |
| `resource_busy`, `fleet_busy` | failed_gate | Inspect the owning operation/resident work, then prepare or retry when exclusivity can be established. |
| `recovery_required`, `operation_not_resumable` | failed_gate | Resolve recorded ownership/effects or use the permitted next action. |
| `artifact_missing`, `artifact_changed`, `unsupported_media_type` | error | Inspect the record and select a retained supported export. |
| `source_unavailable`, `source_stale`, `source_truncated`, `query_incomplete` | degraded, or error if no requested result can be retained | Inspect per-source evidence; repeat collection after the identified cause is corrected. |
| `result_too_large` | degraded for otherwise successful reads; preserve failure outcome otherwise | Read the retained result artifact. |

Existing `stage_timeout`, `stage_cancelled`, `engine_http_error`, `gate_failed`
and other command codes pass through. Compatible new codes are retained and
presented without being mistaken for success. Unsupported schema versions
are rejected before action or checkpoint reuse.
The new `operation_timeout` code uses `outcome: error` when the overall budget
expires; unresolved effects remain in the operation's recovery state.

### Artifact references and bounds

An export reference is `{artifact_id: UUID, kind: string, state:
created|updated|existing|missing, sha256: string|null, size_bytes: integer|null,
observed_at: Timestamp, complete: boolean}`. The hash is lowercase hexadecimal
SHA-256 of the frozen exported bytes, after redaction. Missing exports have
null size/hash and `complete: false`. `state` describes retention, not gate
qualification. The original artifact's provenance and schema determine
whether a deployment can use it as evidence.

Exports are target-scoped immutable regular files. Symbolic-link traversal,
credential files and out-of-root reads are rejected. Raw private records and
filtered exports have different identities and hashes; an exported projection
does not replace the original identity used for plan or evidence validation.
Redaction follows [diagnostic collection](../Diagnostic-Bundles.md#content-policy).
Site-specific free-text encodings require a site-filtered source before export.

`artifact_read.offset` counts bytes in the exported file, starting at zero.
The reader returns complete UTF-8 codepoints up to `max_bytes`; an offset
inside a codepoint or too small a byte limit returns `invalid_input`.
`next_offset` identifies the next unread byte, or is null at EOF.
`complete` states whether this read reached EOF; export completeness is
reported separately in its reference. Supported content is UTF-8 text,
JSON or JSONL. Missing or hash-mismatched exports are not followed to a new
live source behind the same ID.

If an otherwise valid result exceeds the response cap, store its complete
redacted representation locally, set `data: {result_artifact_id: UUID}` and
return that artifact in `artifacts`. The wrapper adds `result_too_large`,
sets `command_result: null` and retains the original failure outcome, or
uses `degraded` for otherwise successful synchronous results. The original
command outcome remains inside the artifact. Accepted receipts and operation
summaries have fixed bounded shapes and must fit without this fallback.

## Example submission

This is a proposed tool exchange after preparing a plan, not a command
available in the current release. IDs are illustrative.

```json
{
  "name": "plan_execute",
  "arguments": {
    "target_id": "trial-fleet",
    "plan_id": "20000000-0000-4000-8000-000000000001",
    "request_id": "30000000-0000-4000-8000-000000000001"
  }
}
```

The `structuredContent` receipt is:

```json
{
  "schema": "narwhal.management-result",
  "schema_version": 1,
  "tool": "plan_execute",
  "target_id": "trial-fleet",
  "observed_at": "2026-09-26T12:00:00Z",
  "outcome": "accepted",
  "data": {"operation_id": "40000000-0000-4000-8000-000000000001"},
  "errors": [],
  "artifacts": [],
  "command_result": null
}
```

The client polls `operation_inspect` with this ID. Receipt recovery uses the
same request ID; a deliberate new attempt follows the
[resumption contract](Operations.md).

## Protocol and contract compatibility

The server negotiates MCP transport/protocol support separately from Narwhal
document versions. The design uses the tools/structured-result interface and
[stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
Child command output is captured; only valid MCP messages reach server stdout.
Long actions use the application operation tools above and do not require an
MCP Tasks extension. Transport cancellation does not substitute for an
authorised `operation_cancel` after a durable receipt exists.

Tool schemas use JSON Schema 2020-12 object types, required-field lists,
additional-property rejection and the bounds in this reference. Each action
variant uses a fixed discriminator/parameter schema. Adding optional output
fields or diagnostic codes within a document version is compatible; changing
required inputs, identity semantics, result meanings or state transitions
requires a new contract version. A server refuses to resume persisted
documents it cannot read, leaving the evidence available for inspection.
