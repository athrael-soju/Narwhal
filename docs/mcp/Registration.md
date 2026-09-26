# Registration and permissions (proposed)

This page defines the planned local management registry used by the
[MCP contract](../MCP-Contracts.md). The operator creates and maintains it on
the management workstation. MCP tools select registered identifiers;
registration itself is an operator action outside the MCP tool catalogue.

## Registry document

The proposed server accepts one explicit `--registry PATH` at startup. Relative
registry paths resolve against its startup directory. The registry is JSON,
with schema `narwhal.management-registry`, version `1`. It is not a fleet
document and does not change the `NARWHAL_FLEET` serving contract.

| Field | Type and contract |
| --- | --- |
| `schema`, `schema_version` | Literal `narwhal.management-registry`, integer `1`. |
| `registry_id` | UUID, retained across server restarts and registry edits. |
| `state_dir` | Absolute local directory for the executor, deduplication index, reservations, plans and operations. |
| `targets` | Array of target entries, at most 100; unique `id` values. |

The registry and private files require mode `0600`, private directories `0700`,
and ownership by the executor's operating-system user. The server refuses a
registry writable by another user. Network filesystems without the required
atomic writes and locks are unsupported for `state_dir` in v1. Startup
validates the complete document before exposing tools.

The operator registers a new deployment using paths to prepared local inputs
and host aliases. Engines and their profiles need not exist yet. Registering
an existing deployment points to its actual fleet document and site settings;
registering an existing dev instance points to its instance directory. This
does not adopt or authorise termination of any process. Ownership is established
from the instance or deployment records and live identities.

## Target entry

Alias identifiers (`id`, `recipe_id`, `host_id`, `engine_id`, `query_id`,
`log_id`) match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Fleet engine IDs that do not
fit this management boundary require an explicit adapter alias mapping;
the underlying fleet schema remains unchanged. Operation, request, plan,
snapshot and artifact IDs are UUID strings.

| Field | Type, default and validation |
| --- | --- |
| `id` | Stable alias, required. |
| `kind` | `dev` or `fleet`, required. |
| `working_directory` | Required absolute directory; all invoked commands use it. |
| `artifact_root` | Required absolute private directory for management evidence and frozen exports. Original CLI evidence remains at the registered instance/deployment source locations. |
| `fleet_file` | Required absolute path or `null`; may name the expected file before creation. A fleet target requires a path. For dev, the instance owns its generated fleet. |
| `instance_dir` | Required absolute path for `dev`; `null` for `fleet`. May be absent before `dev_init`. |
| `adapter` | Required object `{id, settings_path}`; `id` is `local-dev-v1` for dev or `ssh-v1` for fleet. `settings_path` is an absolute private adapter settings file, or `null` for a dev target using only its registered recipe. |
| `endpoints` | Object with optional `router_env`, `prometheus_env`, `grafana_env` environment-variable names; default `{}`. |
| `credential_env` | Distinct environment-variable names available to this target's subprocesses; default `[]`, maximum 64. |
| `capabilities` | Distinct values from `inspect`, `measure`, `mutate`; default `["inspect"]`. |
| `actions` | Distinct allowed [plan actions](Tools.md#plan-actions); default `[]`. |
| `recipes` | Array of `{id, kind, path}`; default `[]`, maximum 100. `kind` is `dev` or `fleet`, matching the target; `path` is an absolute operator-owned input file. IDs are unique within a target. |
| `queries` | Array of `{id, expression, kind}`; default `[]`, maximum 100. `expression` is fixed PromQL, 1–4096 characters; `kind` is `instant` or `range`. |
| `logs` | Array of `{id, host_id, source}`; default `[]`, maximum 100. `source` is an absolute regular-file path on the registered host. A dev host uses alias `local`. |
| `freshness_s` | Integer 1–3600, default `60`; observation age beyond this is stale. |
| `allow_request_content` | Boolean, default `false`; permits an explicitly requested diagnostic export with request content. Credential redaction always applies. |
| `preparation` | Optional object with `timeout_ms=300000`, `term_grace_ms=10000`, `kill_grace_ms=5000`, `reconcile_ms=30000`. Each field is an integer `1..86400000`; missing fields use these proposed defaults. These budgets apply even when the action selects no recipe. |

Paths are literal absolute paths, without shell expansion. Relative paths
inside a fleet file retain Narwhal's existing working-directory semantics.
Config inspection may resolve those paths before the files exist; subsequent
actions check containment and identity when accessing them.

Endpoint variables must resolve to HTTP(S) URLs at call time. Embedded URL
credentials are rejected. Only registered origins and the engine endpoints
resolved from this target's fleet are accessible. Redirects to other origins
are rejected. Environment names match `[A-Za-z_][A-Za-z0-9_]*`.
Credential values are resolved by the executor/adapter and excluded from
tool arguments, plans and responses. Missing endpoint variables affect the
tools that require them; offline config validation remains available.

For a fleet, host aliases and SSH trust bindings come from the adapter settings
and its discovery snapshot. Tools cannot supply an SSH destination, port
forward, executable, environment override or arbitrary URL. The operator
selects query expressions that constrain results to the registered fleet;
MCP arguments select a query ID without inserting expressions or label values.

### Example dev registration

This proposed registry uses illustrative paths. The operator supplies a valid
dev template and matching runtime/model inputs at the registered recipe path.

```json
{
  "schema": "narwhal.management-registry",
  "schema_version": 1,
  "registry_id": "10000000-0000-4000-8000-000000000001",
  "state_dir": "/srv/narwhal/runs/management",
  "targets": [
    {
      "id": "local-dev",
      "kind": "dev",
      "working_directory": "/srv/narwhal",
      "artifact_root": "/srv/narwhal/runs/dev-evidence",
      "fleet_file": null,
      "instance_dir": "/srv/narwhal/runs/dev",
      "adapter": {"id": "local-dev-v1", "settings_path": null},
      "endpoints": {"router_env": "DEV_ROUTER_URL"},
      "credential_env": ["NARWHAL_ENGINE_API_KEY"],
      "capabilities": ["inspect", "measure", "mutate"],
      "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
      "recipes": [
        {"id": "small-cuda", "kind": "dev", "path": "/srv/narwhal/config/dev-recipe.json"}
      ]
    }
  ]
}
```

The adapter's recipe formats and immutable input capture are described in
[Plans and deployment adapter](Deployment.md). Defaults omitted in this
example are filled before the registration digest is computed.

## Permission evaluation

`inspect` permits configuration reads, GET requests, selected log/inventory
collection and creation of private diagnostic artifacts. Those artifact writes
do not confer authority to change a deployment. `measure` permits active
profiling, transfer checks and verification. `mutate` permits the specific
registered lifecycle/deployment actions. Capabilities are independent;
`mutate` does not imply `measure`.

Every target tool requires `inspect`. Plan preparation also requires the
capabilities and action grant needed by its requested action. Preparation can
discover and freeze inputs but cannot install software remotely, launch an
engine, drain traffic or run active measurements. Execution checks the same
grants again against the current registry and selected immutable plan.

Preparation records the exact proposed changes. Execution through
`plan_execute` is an explicit request to apply that plan within the configured
authority; no tool can grant itself additional authority or change registry
permissions. A client may ask its user to review the plan before that call.
The executor enforces authority even when a client omits that UI step.

Execution cancellation and resumption require the original action's current
grants. Cancelling preparation requires only `inspect` to stop its bounded
inspection helpers.
Inspection can still report an operation whose execution grants were revoked.
Revocation prevents new stages or resumed work; the executor records the
blocked state and residual resources. Cleanup that needs revoked authority
requires operator action through the site automation boundary.

## Registry changes and retention

V1 uses one management authority and local coordinator store for each managed
resource set. Cooperating MCP frontends and CLI invocations use the same
registry and `state_dir`. Independent registries/workstations do not provide a
distributed exclusion guarantee; site automation must direct these operations
through the designated authority.

The proposed `NARWHAL_MANAGEMENT_REGISTRY` environment variable selects that
registry for supported finite management CLI commands. The selected fleet file
or dev instance resolves to exactly one registered target by canonical path;
missing or ambiguous matches fail before the operation. Commands retain their
existing arguments and result contracts. Direct registered commands create a
recorded plan/operation internally and enforce the same capabilities and locks.
Nested commands inherit an executor-authenticated local operation context and
join the parent instead of submitting duplicate work. An environment variable
claiming an operation ID is insufficient to bypass coordination.

For the proposed MCP server, explicit `--registry` takes precedence over
`NARWHAL_MANAGEMENT_REGISTRY`; absence of both is a startup error. Existing CLI
invocations without that variable retain their current behaviour and are
outside the shared-coordination guarantee. These bindings are implementation
work for #151, not available CLI settings in the current release.

The server loads a registry snapshot on startup. Operator edits take effect
through executor reload/restart, validated atomically; other connected MCP
frontends use that same executor snapshot. The executor checks permissions
and the registration digest at every stage boundary. Running subprocesses
retain their recorded bounded action/cleanup semantics.

The registration digest covers the default-populated target entry, registry
ID, resolved nonsecret endpoints, adapter settings and recipe input identities.
It excludes secret values. Rebinding an alias invalidates its old plans.
Changing a credential value without changing its reference requires fresh
access checks, without rewriting the plan as if hardware/model inputs changed.

Do not reuse a target ID for a different deployment. Removing a target with
active, cancelling or recovery-required work is rejected; completed records
retain the target snapshot. Plans, operation records, deduplication entries
and failed-attempt evidence have no automatic expiry in v1. Explicit operator
archival may remove evidence after resolving ownership and dependencies;
request-ID tombstones remain so an old retry cannot create another operation.
Tools report `artifact_missing` for removed evidence rather than treating its
absence as success.
