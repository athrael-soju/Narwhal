# Tools and results (proposed)

This page specifies tool arguments, results and errors for the
[MCP contract](../MCP-Contracts.md). The unreleased [server command](../cli/MCP.md)
exposes eighteen tools; the catalogue identifies them below. The installed
`local-dev-v1` and `ssh-v1` adapters support preparation, execution and
resumption for their registered actions. Registration binds each target ID to its inputs and access grants.

The inspection adapters enforce those grants, redact returned content and keep
each result within the response limit. They retain oversized results as private
artifacts. The dispatcher returns `outcome: error` with code `adapter_failed`
if an adapter returns an invalid result or fails to apply the response limit.

## Common argument rules

The server accepts tool arguments as JSON objects and rejects unknown fields.
`Alias` and `UUID` use the [registration types](Registration.md#target-entry).
`Timestamp` is an RFC 3339 UTC string ending in `Z`. Names are case-sensitive.
Numeric inputs must be finite; booleans are not integers.

Every tool except `target_list` requires `target_id: Alias`. Other IDs in a call
must belong to that target and registry. Every target tool requires `inspect`.
Before remote access or work submission, the adapter rejects a missing target,
object, capability or action grant.

All tools accept optional `timeout_s: integer` in `1..30`. The default is `30`
for synchronous calls and `5` for operation admission. This limits the tool
exchange; the recorded plan sets deadlines for the long operation itself.

If admission times out after the executor persists an operation, the client
repeats the same request ID to recover its receipt. A tool timeout does not
cancel an accepted operation.

`target_list` and `operation_list` accept these pagination arguments:

| Argument | Type | Default |
| --- | --- | --- |
| `cursor` | Opaque string or `null` | `null` |
| `limit` | Integer `1..100` | `20` |

The server lists results from an immutable snapshot and returns
`next_cursor: string|null`. A target-list cursor binds the registry snapshot
and requested `limit`. Reusing it after a registry change, with another limit,
or after its stored snapshot is removed returns `invalid_cursor`. Target lists
sort by target ID and survive a server restart with the same registry and state.
A page may contain fewer than `limit` entries to satisfy the response byte limit.
Operation-list cursors also bind the target registration and requested limit; operation
lists sort by creation time descending, then operation ID ascending.

The maximum serialized `structuredContent` is 262,144 bytes. When a production
adapter exceeds this limit, it must retain the complete redacted result as an
artifact within the export size limit and return a reference, following
[Artifact references and bounds](#artifact-references-and-bounds).

## Tool catalogue

Use this table to find a tool's purpose and owning component. The sections below
specify arguments, results and execution rules. Arguments are required unless
marked optional; common arguments above also apply.

The result shapes below describe the management envelope's `data` field.

| Tool | Purpose | Owner | Availability |
| --- | --- | --- | --- |
| `target_list` | List permitted registered targets. | Deployment registry | Implemented |
| `config_inspect` | Read effective fleet configuration. | `config/` | Implemented |
| `config_validate` | Validate a fleet file offline. | `config/` | Implemented |
| `fleet_status` | Read router status and state. | `serving/`, `runtime/` | Implemented |
| `dev_status` | Read a dev instance's recorded status. | `dev/` | Implemented |
| `diagnostics_collect` | Collect selected incident evidence. | `diagnostics/` | Implemented |
| `plan_prepare` | Discover and freeze proposed action inputs. | Deployment preparation and site adapter | Local dev and SSH fleet actions implemented |
| `plan_inspect` | Read a saved plan and its input references. | Plan store | Implemented |
| `plan_execute` | Submit a saved plan for execution. | Shared executor | Local dev and SSH fleet actions implemented |
| `operation_list` | List operations for a target. | Operation store | Implemented |
| `operation_inspect` | Read one operation's state. | Operation store | Implemented |
| `operation_cancel` | Record cancellation for an operation. | Shared executor | Implemented |
| `operation_resume` | Submit a new attempt after reconciliation. | Shared executor | Local dev and SSH fleet actions implemented |
| `monitoring_status` | Read scrape health and monitoring readiness. | Site adapter | Local and SSH providers implemented |
| `metrics_query` | Run a registered Prometheus query. | Observability adapter | Implemented |
| `host_inventory` | Collect inventory from a registered host. | Site adapter | Local and SSH providers implemented |
| `host_logs` | Collect a selected log source. | Site adapter | Local and SSH providers implemented |
| `artifact_read` | Read a retained redacted export. | Artifact store | Implemented |

### Targets and configuration

`target_list` accepts the common pagination arguments. It returns:

```text
{targets: TargetSummary[], next_cursor: string|null}
```

A `TargetSummary` contains `id`, `kind`, `capabilities`, `actions`, `adapter_id`,
`recipes: [{id, kind}]`, and arrays of aliases named `query_ids`, `log_ids` and
`host_ids`. It excludes credentials, environment values, source paths and SSH
destinations. Listing includes only targets with `inspect`. For dev targets,
`host_ids` includes `local`. For fleet targets, the current listing derives
host aliases from registered log entries and does not open SSH settings or
discover hosts. Later discovery must establish each host's live identity.

`config_inspect` and `config_validate` take no arguments beyond the common
fields. They call `narwhal config inspect --format json` and
`narwhal config validate --format json`, respectively, and return an
effective-config document. The adapter opens the registered fleet file, checks
its ownership, permissions and 8 MiB size limit, then passes the open descriptor
to the installed CLI. The CLI runs in the registered working directory with
the target's permitted environment. The result preserves the CLI envelope and
effective configuration after redaction. Both config tools validate offline;
neither result establishes live fleet readiness. All three tools complete
synchronously.

### Fleet and local status

`fleet_status` and `dev_status` take only the common arguments and complete
synchronously. `dev_status` requires a dev target, calls
`narwhal dev status --format json`, and returns the existing dev lifecycle state
and command result. Run the server in the Python environment recorded by the
instance. The tool reads process ownership, HTTP health and retained
verification evidence; it performs no GPU discovery or new transfer probes.
An initialized instance with no running generation reports `stopped`.

`fleet_status` resolves the registered `endpoints.router_env` variable, then
reads `/health`, `/ready`, `/narwhal/state` and `/narwhal/lifecycle` through the
documented router GET endpoints. Its successful `data` is
`{sources: Observation[]}`. Each observation contains:

| Field | Type |
| --- | --- |
| `source` | Registered alias or route string |
| `observed_at` | Timestamp |
| `status` | `ok`, `unavailable`, `timeout`, `truncated`, `stale` or `error` |
| `http_status` | Integer or `null` |
| `data` | Object, string or `null` |
| `artifact_id` | UUID or `null` |

The adapter retains each received HTTP status and redacts response bodies.
If excessive JSON nesting prevents body processing, the adapter discards that
body, reports `source_unavailable` and preserves the other observations.
An HTTP 200 from `/health` does not establish admission readiness. A `/ready`
response with HTTP 503 remains a successful observation of a router that is
not ready.
Redirects produce `unavailable`; the client follows none. Transport failures,
timeouts and other unsuccessful HTTP responses produce failed observations.

The state and lifecycle routes must return their supported version 1 document
contracts. A missing or unsupported contract produces `unsupported_contract`
and `outcome: invalid_input`, while preserving the other observations. Other
source failures produce `degraded` when response bodies or artifacts remain,
or `error` when collection retains none.

### Diagnostic collection

`diagnostics_collect` calls `narwhal diagnostics collect --format json` with the
registered fleet or instance and a fresh private output directory. It completes
synchronously and may write diagnostic artifacts within the registered target.
The registered `endpoints.router_env` variable must resolve to the router URL.
The collector reads the router's five diagnostic routes, including `/metrics`,
and the local sources selected by the
[diagnostic collection contract](../Diagnostic-Bundles.md#source-selection).

The tool accepts optional `include_request_content: boolean=false`. Setting it
to `true` requires the matching registry permission. The tool does not expose
arbitrary `--artifact` paths or automatically include request content.

The successful result contains:

```text
{
  bundle_artifact_id: UUID,
  manifest_artifact_id: UUID,
  collection_status: success|partial,
  source_count: integer
}
```

`bundle_artifact_id` identifies a JSON index of the captured sources and their
artifact IDs. `manifest_artifact_id` identifies the redacted versioned manifest,
with collection status and per-source outcomes. The adapter exports each
captured source separately and removes the temporary collection directory when
the call ends. Read the index, manifest and individual sources through
`artifact_read`; paths in the command result describe the collection run and
do not grant access to those paths.

Before writing a temporary fleet snapshot, the adapter redacts it. Its manifest
row identifies the capture as `redacted_registered_fleet_snapshot` and records
the original captured bytes' hash and size. It omits the temporary file's
modification time. Other source exports retain their collected provenance.

Missing, excluded, timed-out or truncated sources make the collection partial.
An export failure also produces a partial manifest and retains the source's
error. A partial collection returns `outcome: degraded` with the command result
and any retained artifacts.

### Plans

Plan preparation and execution return asynchronously. Inspection of a saved plan
completes synchronously. Use these arguments in addition to the common fields:

| Tool | Arguments |
| --- | --- |
| `plan_prepare` | `action: Action`, `parameters: ActionParameters`, `request_id: UUID` |
| `plan_inspect` | `plan_id: UUID` |
| `plan_execute` | `plan_id: UUID`, `request_id: UUID` |

`plan_prepare` checks the grants required by the selected action, discovers and
freezes inputs, and returns `{operation_id: UUID}`. It performs inspection only.
`plan_execute` rechecks the recorded action's grants and returns
`{operation_id: UUID}` after the shared executor accepts the work.

Both tools use `request_id` for [deduplication](Operations.md). A returned
operation ID records acceptance; inspect the operation to learn its outcome.

`plan_inspect` returns:

```text
{
  plan: DeploymentPlan,
  locally_stale: boolean,
  input_artifacts: [{name: string, artifact_id: UUID}],
  snapshot_artifact_id: UUID
}
```

The plan store redacts the plan and input exports. `locally_stale` reports the
local registration, recipe and available adapter checks. An absent adapter
does not by itself make an immutable plan stale. Execution still requires that
adapter and fresh checks of live inputs.

### Operations

All operation tools complete synchronously except `operation_resume`, which
submits a new attempt. Use these arguments in addition to the common fields:

| Tool | Arguments |
| --- | --- |
| `operation_list` | Common pagination arguments |
| `operation_inspect` | `operation_id: UUID` |
| `operation_cancel` | `operation_id: UUID` |
| `operation_resume` | `operation_id: UUID`, `plan_id: UUID`, `request_id: UUID` |

`operation_list` returns
`{operations: OperationSummary[], next_cursor: string|null}`. Inspection and
cancellation return `{operation: OperationSummary, record_artifact_id: UUID}`.
The artifact contains a frozen redacted record. The
[operation contract](Operations.md) defines `OperationSummary` and the persisted
record.

`operation_cancel` requests cancellation; its response does not establish that
cleanup has finished. Cancelling execution requires the original action's
current grants. Cancelling preparation requires only `inspect`.

The request is durable even when the tool response is lost. If no worker has
claimed the queued operation, the coordinator records terminal cancellation
and its evidence without launching work. A worker that has already claimed it
handles the request under its cleanup budget. Repeat the same cancel call to
inspect the current summary and retained record.

For a lost worker, cancellation can stop local temporary helpers recorded by
the shared command runner, after verifying their ownership and process
identities. It does not stop remote resources or services retained by completed
stages. Unknown effects keep the operation in `recovery_required`, with its
reservations held. See the [cancellation contract](Operations.md#deadlines-and-cancellation)
for the cleanup boundary.

`operation_resume` requires the original action's current grants and uses
`request_id` for deduplication. Before creating the child, the coordinator checks
that the parent has reconciled to `failed` or `cancelled` and validates the
fresh plan's retained inputs and local binding. The worker checks live inputs
before it performs a stage. Every stage runs again with new records and
evidence. An accepted request returns `{operation_id: UUID}` for that new attempt.

### Monitoring and metrics

`monitoring_status` takes only the common arguments and completes synchronously.
The site adapter supplies expected scrape identities. The observability package
inspects Prometheus targets, verifies Grafana, and reads router readiness. The
[monitoring guide](Observability.md) lists the acceptance checks and provider
availability. The result is:

```text
{sources: Observation[], readiness: pass|fail|unknown}
```

A successful collection may still report `readiness: fail`. The tool must retain
the failed readiness check in its result.

`metrics_query` completes synchronously through the registered Prometheus
endpoint. It selects a fixed query by ID and accepts:

| Argument | Type | Default or requirement |
| --- | --- | --- |
| `query_id` | Alias | Required |
| `start` | Timestamp | Required for range queries; omit for instant queries |
| `end` | Timestamp | Required for range queries; omit for instant queries |
| `step_s` | Integer `1..300` | `15` for range queries; omit for instant queries |
| `limit_series` | Integer `1..100` | `100` |

Its result is:

```text
{
  result_type: string,
  series: MetricSeries[],
  complete: boolean,
  observed_at: Timestamp
}
```

A `MetricSeries` contains `labels: object<string,string>` and
`samples: [{at: Timestamp, value: string}]`. The tool preserves Prometheus numeric
strings, including nonfinite values. A nonfinite sample is data and cannot
qualify a measurement. The limits below determine whether a result is complete.

### Host evidence and artifacts

These tools complete synchronously and accept:

| Tool | Arguments |
| --- | --- |
| `host_inventory` | `host_id: Alias` |
| `host_logs` | `log_id: Alias`, optional `max_bytes: integer=65536` |
| `artifact_read` | `artifact_id: UUID`, optional `offset: integer=0`, optional `max_bytes: integer=65536` |

`host_inventory` uses the site adapter to inspect the registered host. It returns
`{snapshot_artifact_id: UUID, observed_at: Timestamp, complete: boolean}`.

`host_logs` copies a selected, site-filtered source into an artifact. It returns
`{artifact_id: UUID, bytes: integer, complete: boolean, observed_at: Timestamp}`.

`artifact_read` reads an authorised immutable export and returns:

```text
{
  artifact_id: UUID,
  sha256: string,
  text: string,
  offset: integer,
  next_offset: integer|null,
  complete: boolean
}
```

See [Artifact references and bounds](#artifact-references-and-bounds) for byte
offsets, UTF-8 handling and checks before access.

### Plan actions

The `action` field selects one parameter object. Parameters have no omission
defaults except those listed here. The saved plan contains the resolved defaults.

| Action | Parameters |
| --- | --- |
| `dev_init` | `{recipe_id: Alias}` |
| `dev_up` | `{}` |
| `dev_verify` | `{}` |
| `dev_down` | `{}` |
| `fleet_deploy` | `{recipe_id: Alias}` |
| `fleet_profile` | `{engine_ids?: Alias[]}`; omission selects all configured engines |
| `fleet_preflight` | `{}` |
| `engine_replace` | `{engine_id: Alias}` |
| `monitoring_start` | `{}` |
| `deployment_cleanup` | `{operation_id: UUID}` |

Each action must appear in the target's `actions` allowlist. All require
`inspect`, plus the grants below:

| Action | Target kind | Additional grants |
| --- | --- | --- |
| `dev_init`, `dev_down` | dev | `mutate` |
| `dev_up` | dev | `measure`, `mutate` |
| `dev_verify` | dev | `measure` |
| `fleet_deploy`, `engine_replace` | fleet | `measure`, `mutate` |
| `fleet_profile`, `fleet_preflight` | fleet | `measure` |
| `monitoring_start`, `deployment_cleanup` | fleet | `mutate` |

The dev adapter invokes the corresponding `narwhal dev` subcommand with
`--format json`. `dev_init` selects the registered recipe and succeeds when it
creates an instance or reuses a matching one. `dev_up` establishes launched
state; it does not establish verification. `dev_verify` requires complete
preflight, directed KV checks and routed verification. `dev_down` requires owned
teardown and stopped state.

For `fleet_deploy`, the site adapter executes Gates A–G, including workload
acceptance. `fleet_profile` uses `narwhal-profile --format json` and binds
measurements to the current processes. If supplied, `engine_ids` contains
1–256 unique registered engine aliases. Preparation resolves and freezes the
selection even when the client uses the default. `fleet_preflight` runs the
full `narwhal-check --format json` procedure; a skipped required gate cannot
pass.

For `engine_replace`, the site adapter supervises replacement and requests the
documented router drain/readmission procedure and required requalification.
Version 1 requires `recovery.engine_restart_policy: "individual"` and an owned
standalone router, with `ha.standby: false` and `ha.epoch: 0`. It uses the
engine's recorded launch specification. After full preflight, it restarts the
owned router with updated profiles and `--resume`, verifies the retained drain
hold, then readmits the engine. Model migrations or arbitrary launch overrides
require a separately prepared supported deployment.

`monitoring_start` invokes the existing monitoring startup procedure and checks
its full readiness contract. `deployment_cleanup` acts on recorded resources
from a selected inactive execution and its bounded cleanup lineage after
checking ownership. The store transfers only that lineage's exact reservations;
see [cleanup admission](Operations.md#deadlines-and-cancellation).

Successful preparation produces terminal operation `data: {plan_id: UUID}`.
Every execution action produces `data: {summary_artifact_id: UUID}`.
The summary artifact holds the underlying final result, verified postconditions,
stage and evidence references, and remaining resources. These small shapes
keep status responses within the limit. The full operation record retains
command results for inspection.

### Collection and query limits

Diagnostic collection allows at most 128 source records, 5 seconds per source,
30 seconds overall and 8,388,608 bytes retained per source. `timeout_s` may
lower the overall budget. Source records include selection outcomes. If the
collector omits additional sources to stay within the cap, the collection
becomes partial. The limit on source records applies to MCP collection;
the underlying CLI applies no such limit unless `--max-sources` is set.

Each status source has at most five seconds within the overall call deadline.
The adapter caps a source body at 262,144 bytes, including bytes received while
streaming. For a larger body, it exports the retained redacted prefix and
returns `status: truncated`, `data: null` and its `artifact_id`. That artifact
has `complete: false`.

The router endpoints have no top-level capture timestamp. `observed_at` records
the start of each GET. At the end of collection, the adapter compares the
elapsed time since that start with the target's `freshness_s`; an otherwise
successful observation older than that limit becomes `stale`. Engine startup
times and nested event timestamps do not measure the age of the response.

For a range metric query, `start` must precede `end` and the window must be at
most 3600 seconds. The adapter rejects future query times. It returns at most
10,000 samples per call and applies the `limit_series` bound. Query warnings,
truncated series or samples, and stale or missing required observations produce
`outcome: degraded`. Where data is incomplete, the result also has
`complete: false`.

The executor bounds the Prometheus timeout by `timeout_s` and limits response
bytes while streaming, with a maximum HTTP body of 8,388,608 bytes. The client
disables redirects and environment proxies and rejects compressed responses.
Registered query expressions cannot contain runtime
substitutions supplied by the agent.

`host_inventory` retains at most 1,048,576 bytes and reports failures for each
source. If a required host utility is unavailable, it reports incomplete
inventory; it must not infer the missing hardware information.

`host_logs.max_bytes` and `artifact_read.max_bytes` accept `1..65536`.
`artifact_read.offset` is an integer greater than or equal to zero. The artifact
tool returns at most 32,768 bytes of content per call even when `max_bytes`
requests more. This leaves room for JSON escaping and envelope fields within
the response cap. Follow `next_offset` until it is `null`. Host log
collection captures the last requested bytes of the selected regular file.
Later changes to the source do not alter that export. If the source is shortened,
the tool reports `complete: false`.

## Result envelope

Every executed tool returns `narwhal.management-result` version `1` in MCP
`structuredContent` and the same serialized object in one text content block.
The server publishes an `outputSchema` for it, following the
[MCP structured-result and tool-error interfaces](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

The server uses JSON-RPC errors for malformed protocol requests and unknown
tool names. A valid call with invalid domain arguments receives a structured
tool error.

| Field | Type |
| --- | --- |
| `schema` | Literal `narwhal.management-result` |
| `schema_version` | Literal integer `1` |
| `tool` | Exact catalogue name |
| `target_id` | Alias or `null` |
| `observed_at` | Timestamp of the response snapshot |
| `outcome` | One of the values below |
| `data` | Object with the tool's result fields |
| `errors` | Array of `{code: string, message: string, context: object}` |
| `artifacts` | Array of export references, defined below |
| `command_result` | Original redacted versioned command result or `null` |

`target_id` is `null` for target listing or arguments that did not identify a
valid target. `data` is `{}` when unavailable. Error context includes affected
IDs, fields, stages and evidence references when available, after redaction.

The adapter preserves the command's schema, status, exit code, error codes and
diagnostic context. It excludes credential and prohibited request content.
`command_result` is `null` when no command result exists or when that result must
be read from an artifact. Absolute paths in command artifact metadata are
informational and do not authorise reads. Top-level export references use
artifact IDs.

For HTTP operations, the adapter retains route and status information without
inventing a CLI exit code. Clients should branch on error codes; command prose
provides diagnostic detail.

| Tool outcome | MCP `isError` | Meaning |
| --- | --- | --- |
| `success` | false | The tool completed. |
| `accepted` | false | The executor persisted the operation or recovered an existing submission. |
| `degraded` | false | The tool returned usable partial data or a degraded command result. |
| `failed_gate` | true | A required precondition or evidence check failed. |
| `invalid_input` | true | Input, binding, document version or domain validation failed. |
| `error` | true | An operational failure prevented the requested result. |
| `interrupted` | true | A synchronous operation was interrupted. |

A successful inspection call may describe a failed operation; read the
operation's `result_status`. An accepted operation has not necessarily
succeeded. Durable cancellation appears in operation state.

Existing CLI outcomes map by identical name. `accepted` has no CLI exit-code
equivalent. A terminal operation stores the command outcome separately from
an inspection call's outcome. A degraded required gate cannot advance a
deployment; its deployment result becomes `failed_gate`.

### Management error codes

These codes supplement existing command-result codes. When admission rejects
new work, it returns no operation ID. A recovered submission may return its
previously persisted ID. Failures after admission appear in the operation
record and terminal result.

| Code | Tool outcome | Action |
| --- | --- | --- |
| `target_not_found`, `object_not_found`, `plan_not_found` | invalid_input | Select a registered target and IDs belonging to it. |
| `unsupported_contract` | invalid_input | Supply a supported document version. |
| `invalid_cursor` | invalid_input | Restart listing. |
| `permission_denied` | invalid_input | Have the operator update the local grant. |
| `audit_failed` | error | Restore private audit storage, then inspect the retained operation before retrying its request. See [audit receipts](Registration.md#audit-receipts) for failures after admission or completion. |
| `request_id_conflict`, `plan_scope_mismatch` | invalid_input | Correct the mismatched request. |
| `operation_record_removed` | error | Inspect the retained tombstone or archive. |
| `stale_plan`, `adapter_prerequisite_missing` | failed_gate | Correct the inputs or prerequisites, then prepare a new plan. |
| `adapter_unavailable` | failed_gate | Use a build with the required execution adapter. |
| `plan_evidence_missing`, `plan_evidence_changed` | failed_gate | Restore the retained inputs or prepare a new plan. |
| `resource_busy`, `fleet_busy` | failed_gate | Inspect the operation or serving work that holds the resource. |
| `recovery_required`, `operation_not_resumable` | failed_gate | Resolve the recorded ownership or effects before another action. |
| `artifact_missing`, `artifact_changed`, `unsupported_media_type` | error | Select a retained supported export. |
| `artifact_too_large` | error | Reduce the source size below the export limit. |
| `source_unavailable`, `source_stale`, `source_truncated`, `query_incomplete` | degraded or error | Inspect the source evidence and correct the cause. |
| `result_too_large` | See below | Read the retained result artifact. |

Use a new request ID only for new work. Do not replay an operation reported as
`operation_record_removed`. After `resource_busy` or `fleet_busy`, prepare or
retry only when the executor can establish exclusive access.

Source failures return `degraded` when usable requested data remains, or `error`
when no requested result can be retained. `result_too_large` makes an otherwise
successful read `degraded`; it preserves a failure outcome. The artifact rules
below specify the returned reference.

The adapter passes through existing `stage_timeout`, `stage_cancelled`,
`engine_http_error`, `gate_failed` and other command codes. Readers preserve
compatible new codes without treating them as success. The executor rejects
unsupported schema versions before an action or checkpoint reuse.

`operation_timeout` uses `outcome: error` when the overall operation budget
expires. The operation's recovery state records any unresolved effects.

### Artifact references and bounds

An export reference contains:

| Field | Type |
| --- | --- |
| `artifact_id` | UUID |
| `kind` | String |
| `state` | `created`, `updated`, `existing` or `missing` |
| `sha256` | Lowercase hexadecimal SHA-256 string or `null` |
| `size_bytes` | Integer or `null` |
| `observed_at` | Timestamp |
| `complete` | Boolean |

The hash covers the frozen exported bytes after redaction. Missing exports have
null size and hash with `complete: false`. `state` describes retention. The
original artifact's provenance and schema determine whether it can serve as
deployment evidence.

The artifact store keeps immutable regular files scoped to one target. It
rejects symbolic-link traversal, credential files and reads outside the allowed
root. Each export holds at most 16 MiB of UTF-8 content; an export that exceeds
that limit returns `artifact_too_large`. The store binds the export to the
registry ID and target registration, then verifies that binding,
ownership, permissions and content hash before every read. A raw private record
and its filtered export have different identities and hashes; plan and evidence
validation still use the original identity.

Adapters follow the [diagnostic content policy](../Diagnostic-Bundles.md#content-policy)
when redacting exports. If a site's free-text format needs additional filtering,
the site adapter must provide a filtered source before export.

`artifact_read.offset` counts bytes from zero. The reader returns complete UTF-8
codepoints up to `max_bytes`. It returns `invalid_input` if the offset falls
inside a codepoint or the byte limit cannot contain the next codepoint.
`next_offset` identifies the next unread byte and is `null` at EOF.

The read result's `complete` field reports whether that read reached EOF. The
export reference separately reports whether collection was complete. The store
supports UTF-8 text, JSON and JSONL. If an export is missing or its hash changed,
the reader must not follow the same ID to a new live source.

When a valid result exceeds the response cap and fits within the export limit,
the adapter must:

1. Store the complete redacted result locally.
2. Set `data: {result_artifact_id: UUID}` and include its reference in `artifacts`.
3. Add `result_too_large` and set `command_result: null` in the returned wrapper.
4. Preserve the original failure outcome, or use `degraded` for an otherwise
   successful synchronous result.

The original command outcome remains in the artifact. Accepted receipts and
operation summaries must use their fixed result fields and fit without this
fallback.

## Example submission

The following example shows the exchange after a plan has been prepared. IDs are illustrative.

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

The server returns this receipt in `structuredContent`:

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

The client polls `operation_inspect` with this ID. To recover a lost receipt,
repeat the submission with the same request ID. To request a new attempt,
follow the [resumption contract](Operations.md).

## Protocol and contract compatibility

The server negotiates MCP protocol support separately from Narwhal document
versions. It uses the tools/structured-result interface and
[stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
The executor captures child command output so only MCP messages reach server
stdout.

Long actions use the operation tools above and do not require an MCP Tasks
extension. After the executor issues a durable receipt, transport cancellation
does not substitute for an authorised `operation_cancel` call.

Tool schemas use JSON Schema 2020-12 object types, required fields, rejection of
additional properties, and the bounds in this reference. Each action uses a
fixed discriminator and parameter schema.

Readers tolerate added optional output fields and diagnostic codes within a
document version. Changing required inputs, identity rules, result meanings or
state transitions requires a new contract version. If the server cannot read a
persisted document, it refuses resumption and leaves the evidence available
for inspection.
