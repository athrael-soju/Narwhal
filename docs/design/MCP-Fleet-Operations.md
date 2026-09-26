# MCP fleet operations contract (planned)

This page specifies the proposed v1 contract for an optional Narwhal MCP server. It is a design for [MCP Fleet Operations](https://github.com/athrael-soju/Narwhal/issues/147), not a description of commands available in the current `narwhal-inference` wheel. It targets operators running an MCP client on a management workstation and implementers of the server, executor, and site adapter. The current package remains the source of truth for the [CLI](../CLI-Reference.md), [HTTP API](../HTTP-API.md), and [deployment gates](../Deploy.md).

The client supplies the model and conversation. The server exposes named tools over local stdio, resolves registered targets, enforces operation permissions, and returns structured results. It does not start a second role controller. Narwhal's router retains admission, placement, and lease authority.

## Target registration

The proposed `NARWHAL_MCP_TARGETS` environment variable points to an absolute, private JSON file on the management workstation. The file uses `narwhal.mcp-targets` version 1 and lists targets with unique IDs. A user edits this file outside the agent session; v1 has no tool to add an arbitrary host or URL. The server validates the file before exposing target operations. It rejects duplicate IDs, unknown target kinds, paths outside declared roots, invalid capability values, and missing required fields. For a new fleet, `fleet_path` and `hosts_path` can designate outputs that Gate A has not created yet; registration reports `unprepared`. The site adapter must obtain the discovery inputs from its declared workstation environment and materialize those files before accepting a plan. Configuration inspection reports `input_missing` until the fleet file exists.

| Field | Type and rule |
| --- | --- |
| `schema`, `schema_version` | Required: `narwhal.mcp-targets`, `1`. |
| `targets` | Required array of target objects; may be empty. |
| `id` | Required unique ASCII identifier matching `[a-z][a-z0-9-]{0,63}`. This is the value passed to tools as `target_id`. |
| `kind` | Required: `dev` or `fleet`. |
| `capabilities` | Required subset of `inspect`, `measure`, `mutate`; an empty array permits target listing only. No capability is granted implicitly. |
| `artifact_root` | Required absolute path for private operation records and diagnostic output. It must exist or have an existing writable parent, and must resolve inside an operator-approved private location. |
| `instance_path` | Required for `dev`: absolute path to the selected instance, including a path not yet created by `dev init`. |
| `fleet_path`, `hosts_path` | Required for `fleet`: absolute paths to the fleet document and site inventory. The adapter checks their contents and referenced environment before mutation. |
| `router_url_env` | Required for `fleet`: name of the environment variable holding the router URL. The URL itself is not a tool parameter. |
| `prometheus_url_env`, `grafana_url_env` | Required for `fleet`: names of environment variables holding the monitoring origins. The adapter checks their reachability when the relevant tool runs. |
| `adapter` | Required for `fleet`: `ssh-v1` in this milestone. Adapter-specific preparation is checked before a plan can execute. |

Example registration, with paths and values supplied by the operator:

```json
{
  "schema": "narwhal.mcp-targets",
  "schema_version": 1,
  "targets": [
    {
      "id": "fleet-a",
      "kind": "fleet",
      "capabilities": ["inspect", "measure", "mutate"],
      "artifact_root": "/private/narwhal/runs/mcp/fleet-a",
      "fleet_path": "/private/narwhal/config/fleet.json",
      "hosts_path": "/private/narwhal/config/hosts.json",
      "router_url_env": "NARWHAL_ROUTER_URL",
      "prometheus_url_env": "NARWHAL_PROMETHEUS_URL",
      "grafana_url_env": "NARWHAL_GRAFANA_URL",
      "adapter": "ssh-v1"
    }
  ]
}
```

These values are illustrative. Keep the registry under an ignored private location such as `runs/mcp/targets.json`. The server resolves and checks registered paths on each call. It uses environment references for credentials; secret values never enter tool arguments, plan summaries, or result envelopes. The environment supplied to a command is limited to the target and operation it serves. A registered fleet can refer to the existing `NARWHAL_FLEET` and site environment conventions without adding management fields to `narwhal.fleet`.

`narwhal_targets_list` returns IDs, kinds, capabilities, and availability diagnostics, including `unprepared`. It does not return private addresses, credential values, or absolute paths. A failed registration entry is visible as invalid with a diagnostic; the server does not silently drop it.

## Common tool contract

Every input schema is a JSON object with `additionalProperties: false`. Unless stated otherwise, a tool requires `target_id`. IDs resolve only through the registry. Tools cannot accept command strings, unregistered URLs, arbitrary file paths, or environment values as arguments. The server validates the target kind and required capability before invoking the underlying operation. Unavailable dependencies produce a structured failure.

| Input | Type and rule |
| --- | --- |
| `target_id` | Required registered target ID for target tools. |
| `request_id` | Required UUID string on every mutating or measuring submission. The caller reuses it only to retry the same submission. Scoped to the target and tool. |
| `plan_id` | Required for deployment execution; exact ID returned by `narwhal_deployment_prepare`. |
| `operation_id` | Required UUID string for operation status, cancellation, and resumption; always paired with `target_id`. |
| `artifact_id` | Opaque ID returned by a result or operation record; never a client-supplied path. |

Read calls have a 30-second server deadline by default. Diagnostic collection retains its existing 30-second overall and 5-second per-source defaults; a caller may lower either deadline but not raise the overall limit above 120 seconds. Tool responses have a 256 KiB serialized size cap. A bounded artifact read returns at most 64 KiB of redacted UTF-8 text per call. HTTP source reads are capped at 1 MiB before parsing. `offset_bytes` addresses an immutable, redacted UTF-8 snapshot recorded under the artifact ID; a changed source gets a new artifact ID. If a result would exceed the response cap, the server returns a bounded summary and an artifact reference with `result_too_large`; it never truncates JSON into an invalid result. Long submissions return an operation handle within 30 seconds; their stage deadlines are finite values recorded by the executor or adapter, not extensions of the tool-call deadline. The adapter must reject a stage without a defined deadline.

A completed call returns `structuredContent` conforming to the proposed `narwhal.mcp-tool-result` version 1 envelope and a short text summary. The envelope is a Narwhal document carried inside the MCP result, not a replacement for the MCP wire protocol. Its top-level fields are:

| Field | Type and meaning |
| --- | --- |
| `schema`, `schema_version` | `narwhal.mcp-tool-result`, `1`. |
| `status` | `success`, `accepted`, `degraded`, `failed_gate`, `invalid_input`, `error`, or `interrupted`. `accepted` means an operation was recorded, not that its work passed. |
| `data` | Tool-specific object. It can contain a sanitized `source_result` for an existing command or HTTP response. Private paths in the raw result become artifact IDs or stable aliases in the MCP view; the raw result remains in private storage. |
| `errors` | Array with stable `code`, message, and available stage/engine/field/context. Preserve unknown compatible codes. |
| `artifacts` | Array of opaque `artifact_id`, kind, state, byte count when known, and truncation status. Do not expose arbitrary paths in the MCP response. |
| `operation_id` | UUID for an accepted long action; absent for synchronous results. |
| `observed_at` | UTC timestamp for observed state, so a cached response cannot appear current. |

The server sets MCP `isError: true` for `degraded`, `failed_gate`, `invalid_input`, `error`, and `interrupted`. An accepted submission is not a successful deployment. Argument/schema failures are reported at the tool boundary without starting work. A command's `narwhal.command-result` status, stable code, and artifact state are retained in the sanitized `data.source_result`; a mapped error does not discard partial evidence. Existing documents keep their current schema versions. The consumer rejects unsupported versions before using fields and may retain unknown compatible fields for inspection. A new incompatible MCP envelope gets a new `schema_version`.

The proposed input schema for deployment execution requires the registered target, client retry key, and exact plan identity:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["target_id", "request_id", "plan_id"],
  "properties": {
    "target_id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
    "request_id": {"type": "string", "format": "uuid"},
    "plan_id": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
  }
}
```

A successful submission returns an operation handle, while the final gate result arrives through `narwhal_operation_get`:

```json
{
  "schema": "narwhal.mcp-tool-result",
  "schema_version": 1,
  "status": "accepted",
  "data": {"plan_id": "sha256:72d3b2e2f28c4b2860fb5cfd0a4de75b153b3783041672bd5df901d7d43c917a"},
  "errors": [],
  "artifacts": [],
  "operation_id": "4f3843ab-0bc4-4be6-b582-74116823c35b",
  "observed_at": "2026-09-26T12:00:00Z"
}
```

The envelope's `status` is the operation submission outcome. `narwhal_operation_get` returns its current state in `data.operation`. For a blocked stage, it retains a sanitized `source_result` with the original `failed_gate` status and `gate_failed` code. The executor still records the complete underlying result privately.

| Proposed error code | Envelope status | Meaning and next action |
| --- | --- | --- |
| `target_missing`, `registration_invalid`, `idempotency_conflict` | `invalid_input` | Fix the selected target or client submission. No stage starts. |
| `permission_denied` | `error` | The target lacks the required configured capability or access. No protected action starts. |
| `plan_stale`, `operation_conflict`, `adapter_unavailable` | `failed_gate` | Reprepare changed inputs, wait or reconcile the active operation, or install the declared adapter. No new conflicting mutation starts. |
| `artifact_unavailable`, `result_too_large`, `collection_partial` | `degraded` | Inspect retained manifest/artifact references; the result is incomplete. |
| `recovery_required` | `failed_gate` | Reconcile ownership and evidence before resuming. |

Existing command codes such as `gate_failed`, `stage_timeout`, and `engine_http_error` stay unchanged inside `source_result`. A new MCP code supplies adapter context and does not relabel an underlying command failure.

The standard MCP tool annotations are descriptive hints. The configured capabilities and executor checks provide the actual permission decision. An inspection tool that writes a local evidence bundle is not marked as free of side effects merely because it leaves the fleet unchanged. The [MCP tool specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) defines the wire-level schemas and annotations; [task #149](https://github.com/athrael-soju/Narwhal/issues/149) selects and tests the SDK and protocol revisions. Narwhal operation handles use ordinary tool results and status tools in v1. They do not require the draft MCP Tasks extension.

## Tool catalogue

Inputs below are in addition to the common fields. Bracketed inputs are optional. `operation` means a returned `operation_id` and a persisted operation record. The result column names the required `data` fields in the common envelope. Each output also carries the applicable status, errors, artifacts, and observation time.

| Tool | Kind; capability | Inputs and defaults | Result data; owner |
| --- | --- | --- | --- |
| `narwhal_targets_list` | Either; none | No arguments. | `targets[]` with ID, kind, granted capabilities, and registration state; MCP adapter. |
| `narwhal_config_inspect` | Fleet; inspect | `target_id`; `mode` defaults to `inspect`, accepts `inspect` or `validate`. | `source_result` with the existing effective-config document on success; `config/` via `narwhal config`. No live reachability claim. |
| `narwhal_fleet_status` | Fleet; inspect | `target_id`. | `health`, `ready`, `state`, `lifecycle`, per-endpoint observation/errors; documented router GETs. |
| `narwhal_diagnostics_collect` | Either; inspect | `target_id`, `request_id`, [`source_timeout_seconds`] default 5, [`timeout_seconds`] default 30. | `bundle_artifact_id`, `manifest_status`, `sources[]`; `diagnostics/` collector. Creates only a fresh private bundle under the target artifact root. |
| `narwhal_artifact_read` | Either; inspect | `target_id`, `artifact_id`, [`offset_bytes`] default 0, [`limit_bytes`] default 65536. | `text`, `next_offset_bytes`, `truncated`; evidence owner through bounded adapter. `limit_bytes` accepts 1–65536. No raw binary or arbitrary paths. |
| `narwhal_dev_init` | Dev; mutate, operation | `target_id`, `request_id`, [`settings`] containing only supported `narwhal dev init` flags. | `operation_id`, eventual lifecycle `source_result`; `dev/`. Omitted settings retain existing init semantics. |
| `narwhal_dev_up` | Dev; mutate and measure, operation | `target_id`, `request_id`. | `operation_id`, eventual launch `source_result`; `dev/`. |
| `narwhal_dev_verify` | Dev; measure, operation | `target_id`, `request_id`. | `operation_id`, eventual verification `source_result`; `dev/`. |
| `narwhal_dev_status` | Dev; inspect | `target_id`. | `source_result`, retained verification failure and process status; `dev/`. |
| `narwhal_dev_down` | Dev; mutate, operation | `target_id`, `request_id`. | `operation_id`, eventual teardown `source_result`; `dev/`. |
| `narwhal_deployment_prepare` | Fleet; measure, operation | `target_id`, `request_id`. | `operation_id`, eventual `plan_id` and sanitized plan summary; `deployment/` plus site adapter. Discovery may start temporary inspection processes. |
| `narwhal_deployment_execute` | Fleet; mutate and measure, operation | `target_id`, `request_id`, `plan_id`. | `operation_id`, gate progress and final result; `deployment/` plus site adapter. |
| `narwhal_deployment_cleanup` | Fleet; mutate, operation | `target_id`, `request_id`, `deployment_operation_id` of a prior deployment attempt. | `operation_id` for cleanup, removed/retained resource IDs; `deployment/` plus site adapter. Requires verified ownership. |
| `narwhal_operation_get` | Either; inspect | `target_id`, `operation_id`. | Complete operation record, latest gate, progress and artifact IDs; operation owner. |
| `narwhal_operation_cancel` | Either; mutate | `target_id`, `operation_id`, `request_id`. | Updated state and cancellation request result; operation owner. Cancellation does not imply teardown. |
| `narwhal_operation_resume` | Either; capability of original operation, operation | `target_id`, `operation_id`, `request_id`. | Same operation ID, updated state and next gate; operation owner. Only a blocked or recovery-required operation with valid plan and reconciled ownership may resume. |
| `narwhal_monitor_start` | Fleet; mutate, operation | `target_id`, `request_id`. | `operation_id`, startup result and target evidence; site adapter plus `observability/`. |
| `narwhal_monitor_status` | Fleet; inspect | `target_id`. | `targets[]`, scrape observations/errors, Grafana checks, router readiness separately; `observability/` plus site adapter. |
| `narwhal_monitor_query` | Fleet; inspect | `target_id`, `query`, [`timeout_seconds`] default 10. | Bounded Prometheus instant-query data, result type, evaluation time, truncation/error details; `observability/`. `query` is at most 2048 characters; timeout accepts 1–30 seconds; at most 100 series returned. |
| `narwhal_host_evidence_collect` | Fleet; inspect | `target_id`, `request_id`, `host_id`, `source` (`inventory` or an allowlisted service log), [`max_bytes`] default 65536. | `artifact_id`, source identity, byte count, truncation/error; site adapter. `max_bytes` accepts 1–65536. |

The `settings` object for `narwhal_dev_init` names only the existing flags `template`, `model`, `model_dir`, `gpu`, `engine_count`, `port_base`, `gpu_memory_utilization`, `device_allowance`, and `interface`, with the same values and validation as [the CLI reference](../cli/Dev.md). It has no free-form launch arguments. `narwhal_monitor_query` accepts only a query against the registered Prometheus service, never a URL. The host evidence source allowlist is part of adapter configuration, not a tool argument that can name any remote command.

## Deployment plan

`narwhal_deployment_prepare` performs Gate A discovery and records a private `narwhal.mcp-plan` version 1 document under the target artifact root. `narwhal_deployment_execute` revalidates Gate A evidence and executes Gates B–G, retaining all seven gate records in the deployment operation. It resolves the registered fleet and host inputs, approved source revision, immutable engine image identity, model revision or checkpoint digest, accelerator allocation, TP shape, fabric route/transport, SLOs, supported adapter version and prerequisites. Each value records its source. The plan records a fingerprint of canonical resolved inputs and a separate sanitized summary for the agent. Credentials are referenced by name, excluded from both the fingerprint and result summary, and checked for availability at execution. The plan's `plan_id` is the SHA-256 digest of its canonical immutable inputs, prefixed by `sha256:`.

The executor requires the exact `plan_id` and repeats the relevant prerequisite and identity checks before the first mutation and on resume. A changed source revision, image, checkpoint, host allocation, route, fleet contract, adapter revision or measurement policy rejects the old plan; the operator prepares a new one. A changed engine process generation can leave the desired plan valid, but invalidates the associated live cache, attestation, profile and transfer evidence. The executor reruns affected gates. A changed SLO or first-token deadline requires a new plan, then repeats the applicable preflight gates against saved profiles and live handoffs, following the [deployment invalidation table](../Deploy.md#deployment-sequence). Gate G acceptance also requires the current workload/journal and monitoring evidence.

A sanitized plan summary can look like this:

```json
{
  "schema": "narwhal.mcp-plan",
  "schema_version": 1,
  "plan_id": "sha256:72d3b2e2f28c4b2860fb5cfd0a4de75b153b3783041672bd5df901d7d43c917a",
  "target_id": "fleet-a",
  "source_revision": "31e862e3e1d20a294d5b886186bafd250a3d5016",
  "hosts": ["router-1", "engine-1", "engine-2"],
  "engines": [
    {"id": "engine-1", "accelerators": ["gpu-a"], "tp": 1},
    {"id": "engine-2", "accelerators": ["gpu-b"], "tp": 1}
  ],
  "gates": ["A", "B", "C", "D", "E", "F", "G"],
  "artifact_id": "plan:example"
}
```

The real private plan includes verified identities and input hashes that the summary omits. This example represents the proposed schema shape; it is not an installed command output. `prepare` can return a failed prerequisite before a plan is accepted. `execute` cannot turn a missing adapter, missing workload tool, or failed required gate into a successful deployment.

## Operation lifecycle

The proposed `narwhal.mcp-operation` version 1 record contains `operation_id`, `target_id`, tool, `request_id`, sanitized input hash, `plan_id` when applicable, state, stage/gate, timestamps, source results, evidence IDs, owned resource identities, and cleanup/recovery outcome. Records use private storage under the registered artifact root. The executor persists a transition before starting its external action and reconciles uncertain outcomes after restart.

| State | Meaning | Allowed next state |
| --- | --- | --- |
| `queued` | Submission recorded; execution has not started. | `running`, `cancel_requested`, `failed`. |
| `running` | One stage is active. | `running`, `blocked`, `cancel_requested`, `recovery_required`, `succeeded`, `failed`. |
| `blocked` | A gate or prerequisite failed with retained evidence. No stage is active. | `running` after valid resume, `cancel_requested`, `failed`. |
| `cancel_requested` | Cancellation recorded; owned active work is stopping. | `cancelled`, `recovery_required`. |
| `recovery_required` | Execution outcome or ownership is uncertain; no new mutation may start. | `blocked` after reconciliation, `running` after verified resume, `cancel_requested`, `failed`. |
| `succeeded` | All required work and acceptance gates passed. | Terminal. |
| `failed` | Operation cannot resume under its plan. | Terminal. |
| `cancelled` | Current work stopped and cleanup outcome was recorded. | Terminal. |

`narwhal_operation_resume` accepts `blocked` or `recovery_required` only. It first reconciles process boot ID/start ticks, remote supervisor identity, stage records and gate evidence. It resumes under the same `operation_id` and valid plan, retaining all prior attempts. A failed plan check leaves the old operation blocked or failed and requires a newly prepared plan and submission. Cancellation requests are idempotent for the same active operation, but do not reverse completed gates. `narwhal_deployment_cleanup` is a separate action limited to resources proven to belong to that attempt. A successful Gate G leaves engines, attestation sidecars, router and monitoring running, as in the [deployment runbook](../Deploy.md#deployment-record).

An operation record uses the following required fields. Optional fields appear when the relevant action has begun:

| Field | Type and rule |
| --- | --- |
| `schema`, `schema_version` | `narwhal.mcp-operation`, `1`. |
| `operation_id`, `target_id`, `tool`, `request_id` | Required strings identifying the submission. |
| `input_sha256` | Required SHA-256 of normalized, secret-free tool inputs. |
| `state`, `created_at`, `updated_at` | Required state and UTC timestamps. |
| `plan_id` | Required for deployment execution; absent for unrelated operations. |
| `stage`, `gate` | Current or most recent stage and gate when applicable. |
| `attempts`, `artifacts`, `owned_resources` | Arrays retained across retries; each attempt carries source outcome and start/end times. Resource entries include the identity used for ownership checks. |
| `recovery` | Reconciliation result, next permitted action and residual resources when work was interrupted or cancelled. |

`request_id` deduplication persists with an operation or collection record. Repeating the same tool, target and request ID with identical normalized input returns the original operation ID and current state, or the original collection result, including after a server restart. Reusing that key with different input returns `invalid_input` and `idempotency_conflict` without starting work. A new request ID may start another operation only when the target's coordination rules allow it. Simple inspection calls need no request ID; evidence collection requires one so a retry does not create a second bundle. The server disconnect does not cancel an accepted operation.

The executor serializes conflicting mutation and active measurement per target across MCP clients and participating CLI entry points. Existing dev instance locking remains authoritative for local lifecycle actions. Disjoint engine launches may proceed concurrently; actions sharing a GPU serialize. Fabric tests run one directed edge at a time while engines are idle. Attestations may run concurrently after fabric qualification. A busy target returns `failed_gate` and `operation_conflict` before starting a conflicting stage. Site automation external to the executor must use the same reservation protocol or be treated as a prerequisite that needs operator reconciliation. Narwhal's router continues managing serving roles independently.

## Evidence, permissions, and adapter boundary

`inspect` reads configuration and live state and may write private diagnostic artifacts. `measure` allows active probes, profiling and directed transfer qualification. `mutate` allows process and service changes and cleanup. Capabilities are independent: `narwhal_dev_up` and `narwhal_deployment_execute` require both `measure` and `mutate` because they start processes and profile or qualify the fleet. The server checks the registered capability for each call and again before executing a persisted plan. A tool's MCP annotation never grants permission. The process account, SSH identity and existing host permissions remain additional limits. The adapter must reject unsupported hosts, missing tools, unverified SSH host keys, unavailable credentials and unavailable packaged assets before the first mutation.

The server returns artifact IDs only for files its operation recorded inside the target's registered root. `narwhal_artifact_read` resolves an ID from the manifest, rejects symlinks and files outside the root, redacts recognised credentials and request fields, and applies the byte cap after redaction. An unknown artifact ID is `invalid_input`; a recorded artifact that has disappeared returns `degraded` with `artifact_unavailable`. It reports `truncated` or `unavailable` rather than returning unbounded log text. The existing [diagnostic bundle content policy](../Diagnostic-Bundles.md#content-policy) remains the default; v1 exposes no option to include request bodies through MCP. Structured command results and errors keep stable status and code fields. Logs, source records and plan details stay in private ignored locations; exported summaries use stable host aliases. V1 does not delete completed operation records or artifact snapshots automatically. The operator manages retention in the private artifact root after the associated operation is terminal; there is no MCP delete tool.

The `ssh-v1` adapter accepts one model and a homogeneous accelerator/TP shape under the [deployment operating model](../Deploy.md#operating-model). Its declared prerequisite record must cover the following before mutation:

| Location | Required inputs or capability | Check |
| --- | --- | --- |
| Management workstation | Linux, Python 3.11+, Git, Bash, OpenSSH, the selected SSH credential method and trusted host keys. | Check versions, credential reference presence and recorded host-key trust. Do not replace a changed host key automatically. |
| Router/observability host | Reachable Python 3.11+ with `venv`, Git, Make, curl, Docker Engine/Compose for monitoring, private control endpoints and writable run root. | Check access, runtime tools, listener ownership and intended service ports. |
| Engine hosts | Allocated accelerators and transfer devices, compatible driver/container runtime, pinned image, checkpoint and fabric reachability. | Compare discovered device/launch identity, image and model digests, and planned route with the live hosts. |
| Installed adapter | Versioned deployment and monitoring scripts/assets, plus the selected workload acceptance client. | Check installed location and content digest; reject a missing or mismatched asset before execution. |

The adapter's platform claim extends only to configurations that pass discovery and the live qualification in [task #156](https://github.com/athrael-soju/Narwhal/issues/156). A passing synthetic test cannot establish support for an unmeasured GPU/runtime combination.

The site adapter owns host credentials, package distribution, network preparation, remote process launch and monitoring startup. The executor owns plan identity, stage order, persisted progress, concurrency and recovery. `mcp/` owns tool schemas, dispatch, transport and result mapping. The existing `tools/deployment/` and `tools/observability/` scripts are checkout tools today. [Task #153](https://github.com/athrael-soju/Narwhal/issues/153) must package their required assets or declare a separately installed, versioned adapter with an explicit root and digest. An installed MCP server cannot assume those scripts exist beside a wheel. It validates the selected adapter and its assets before accepting execution.

For observability, “scraping” means Prometheus scraping of the configured router and engine metrics targets plus bounded collection of registered-host inventory and selected logs. Prometheus owns periodic scraping. The MCP server can configure the existing monitoring stack, report target scrape status and errors, and make bounded instant queries. Successful scraping, router readiness and workload acceptance are separate observations. Gate G requires the existing [monitoring readiness checks](../observability/01-Start-and-Verify.md#readiness-contract), workload/journal reconciliation and a post-load KV ring, as documented in [Gate G](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

## Worked cases

| Case | Response and retained evidence | Next action |
| --- | --- | --- |
| Gates A–G pass | `narwhal_deployment_execute` returns `accepted`; `narwhal_operation_get` later returns `succeeded` with gate records, live engine generations, workload/journal reconciliation, scrape and post-load ring evidence. | Keep the serving processes running and inspect the operation record. |
| Gate D fails | Operation becomes `blocked`; the failed directed edge, measured budget, stage logs and prior gates remain. | Correct the fabric cause, then resume if plan identity remains valid. Remeasure affected edges. |
| Duplicate request | Same target/tool/request ID and input returns the existing operation ID; changed input returns `idempotency_conflict`. | Inspect the existing operation or submit a new request ID for distinct work. |
| Client disconnect | Accepted operation continues; the record retains its last transition and evidence. | Reconnect and call `narwhal_operation_get` with its ID. |
| Server restarts during launch | Operation becomes `recovery_required` until remote and local process identities are reconciled. No second launch starts automatically. | Inspect the record, reconcile ownership, then resume if permitted. |
| Cancel after Gate B | Current stage stops if owned; completed installation and evidence remain; cleanup outcome lists any residual resources. | Inspect the result and invoke explicit cleanup for owned resources if required. |
| Engine generation changes | The current plan may remain valid, but old cache, attestation, profiles and directed transfer evidence are marked stale. | Requalify the changed engine and affected paths before later gates. |
| Adapter prerequisite missing | `prepare` or `execute` returns a named `invalid_input`/`input_missing` or `failed_gate`/`adapter_unavailable` before mutation, with no accepted deployment. | Install or correct the adapter, then prepare or execute with a valid plan. |
| CLI or probe conflict | A conflicting submission returns `operation_conflict`; no stage begins. The current owner/operation ID is reported where disclosure permits. | Wait for the active operation or reconcile external activity. |

All examples above describe planned behaviour. [Task #156](https://github.com/athrael-soju/Narwhal/issues/156) qualifies the assembled workflows on a supported local GPU and live fleet; synthetic tests alone establish only code behaviour within their fixtures.
