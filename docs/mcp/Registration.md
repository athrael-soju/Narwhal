# Registration and permissions (proposed)

The operator uses a local registry to name the fleets and dev instances that
MCP tools may access. Each entry binds a target ID to its input files, endpoint
references and allowed actions. Create and maintain this file on the management
workstation; MCP tools cannot edit it.

The unreleased [server command](../cli/MCP.md) validates this document at
startup. The permissions and shared execution rules below define the planned
operations in the [MCP contract](../MCP-Contracts.md).

## Registry document

The server accepts one explicit `--registry PATH` at startup, with
`NARWHAL_MANAGEMENT_REGISTRY` as the fallback. Relative registry paths resolve
against its startup directory. The registry is JSON,
with schema `narwhal.management-registry`, version `1`. It is not a fleet
document and does not change the `NARWHAL_FLEET` serving contract.

| Field | Type and contract |
| --- | --- |
| `schema`, `schema_version` | Literal `narwhal.management-registry`, integer `1`. |
| `registry_id` | UUID, retained across server restarts and registry edits. |
| `state_dir` | Absolute local directory for the executor, deduplication index, reservations, plans and operations. |
| `targets` | Array of target entries, at most 100; unique `id` values. |

The registry requires mode `0600` and ownership by the server's operating-system
user. It must be a regular file at most 1 MiB; the loader rejects a symlink at
the final path component and duplicate JSON keys. Planned executor private files
require mode `0600`, private directories `0700`, and the same ownership.
The executor requires atomic writes and locks in `state_dir`. V1 does not
support network filesystems that cannot provide them. The server validates the
complete registry before exposing tools.

For a new deployment, register the paths to prepared local inputs and the host
aliases. Engines and profiles need not exist yet. For an existing deployment,
register its fleet document and site settings; for an existing dev instance,
register its instance directory.

Registration does not authorise the executor to terminate an existing process.
Before acting on a process, the executor must establish ownership from the
instance or deployment records and the process's live identity.

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
| `working_directory` | Absolute directory used by invoked commands. |
| `artifact_root` | Absolute private directory for management evidence and frozen exports. |
| `fleet_file` | Absolute path or `null`; a fleet target requires a path. |
| `instance_dir` | Absolute path for `dev`; `null` for `fleet`. |
| `adapter` | Object `{id, settings_path}`; see adapter rules below. |
| `endpoints` | Object with optional `router_env`, `prometheus_env`, `grafana_env` environment-variable names; default `{}`. |
| `credential_env` | Distinct environment-variable names available to this target's subprocesses; default `[]`, maximum 64. |
| `capabilities` | Distinct values from `inspect`, `measure`, `mutate`; default `["inspect"]`. |
| `actions` | Distinct allowed [plan actions](Tools.md#plan-actions); default `[]`. |
| `recipes` | Array of `{id, kind, path}`; default `[]`, maximum 100. |
| `queries` | Array of `{id, expression, kind}`; default `[]`, maximum 100. |
| `logs` | Array of `{id, host_id, source}`; default `[]`, maximum 100. |
| `freshness_s` | Integer 1–3600, default `60`; observation age beyond this is stale. |
| `allow_request_content` | Boolean, default `false`; permits request content in an explicitly requested diagnostic export. |
| `preparation` | Optional object containing the time budgets listed below. |

The operator may register `fleet_file` before the file exists and `instance_dir`
before `dev_init` creates the instance. A dev instance owns its generated fleet
document. CLI commands retain their original evidence at the registered source
locations; `artifact_root` stores the management copies and exports.

The adapter ID is `local-dev-v1` for a dev target and `ssh-v1` for a fleet target.
Its `settings_path` names an absolute private settings file. A dev target that
uses only its registered recipe may set `settings_path` to `null`.

Each recipe has an ID unique within its target, a `kind` matching the target's
`dev` or `fleet` kind, and an absolute path to an operator-owned input file.
Each query has a fixed PromQL `expression` of 1–4096 characters and a `kind` of
`instant` or `range`. Each log names an absolute regular-file `source` on its
registered host; a dev target uses host alias `local`.

Preparation uses these budgets, in milliseconds. Each accepts an integer in
`1..86400000`; omitted fields use their listed defaults. The budgets apply even
when an action selects no recipe.

| Field | Default |
| --- | ---: |
| `timeout_ms` | 300000 |
| `term_grace_ms` | 10000 |
| `kill_grace_ms` | 5000 |
| `reconcile_ms` | 30000 |

Paths are literal absolute paths, without shell expansion. Relative paths
inside a fleet file retain Narwhal's existing working-directory semantics.
Config inspection may resolve those paths before the files exist; subsequent
actions check containment and identity when accessing them.

At call time, the executor resolves each endpoint variable to an HTTP(S) URL.
It rejects embedded URL credentials and redirects to unregistered origins.
Tools may access only registered origins and engine endpoints resolved from
the target's fleet. Environment names match `[A-Za-z_][A-Za-z0-9_]*`.

The executor or adapter resolves credentials locally and excludes their values
from tool arguments, plans and responses. Credential redaction applies even
when `allow_request_content` is true. A missing endpoint variable fails the
tools that require it; offline config validation remains available.

For a fleet, the adapter settings and discovery snapshot supply host aliases
and SSH trust bindings. Tools cannot supply an SSH destination, port forward,
executable, environment override or arbitrary URL. The operator must choose
query expressions that constrain results to the registered fleet. MCP arguments
select a query ID and cannot insert expressions or label values.

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

The executor checks the target's capability grants before each operation:

- `inspect` allows configuration reads, GET requests, selected log and inventory
  collection, and private diagnostic artifacts. It does not permit changes to
  the deployment.
- `measure` allows active profiling, transfer checks and verification.
- `mutate` allows the lifecycle and deployment actions listed in `actions`.

Grant each capability separately. `mutate` does not imply `measure`.

Every target tool requires `inspect`. Before preparing a plan, the executor
also checks the capabilities and action grant needed to execute it. Preparation
may discover and freeze inputs, but must not install software remotely, launch
an engine, drain traffic or run active measurements. Before execution, the
executor checks those grants again against the current registry and the saved
plan.

The prepared plan records the proposed changes. A `plan_execute` call requests
those changes within the configured grants. No tool can change registry
permissions or grant itself further access. A client may ask the user to review
the plan before calling `plan_execute`; the executor checks permissions whether
or not the client provides that review step.

Before cancelling or resuming execution, the executor checks that the original
action's grants still exist. Cancelling preparation requires only `inspect`
because it stops inspection helpers.

If the operator revokes an execution grant, inspection can still report the
operation. The executor prevents new stages and resumption, records the blocked
state and lists remaining resources. If cleanup requires a revoked grant, the
operator must handle that cleanup through site automation.

## Registry changes and retention

V1 coordinates each managed resource set through one registry and local
`state_dir`. All MCP frontends and CLI invocations acting on those resources
must use that same store. Independent registries or workstations cannot exclude
conflicting work. Site automation must direct operations through the designated
registry and coordinator.

The planned finite management CLI commands use `NARWHAL_MANAGEMENT_REGISTRY`
to select the registry. The command resolves its fleet file or dev instance by
canonical path to exactly one target. Missing or ambiguous matches fail before
the operation starts.

These commands retain their existing arguments and result contracts. A direct
command creates a plan and operation record internally, then checks the same
capabilities and locks as an MCP call. A nested command joins its parent
operation using local context authenticated by the executor. An environment
variable containing an operation ID cannot establish that authority.

For the MCP server, explicit `--registry` takes precedence over
`NARWHAL_MANAGEMENT_REGISTRY`; absence of both is a startup error. Existing CLI
invocations without that variable retain their current behaviour and are
outside the shared-coordination guarantee. The MCP startup binding is part of
the unreleased server. The proposed finite management CLI bindings and
shared coordination remain implementation work for #151.

The server loads a registry snapshot on startup; restart it after editing the
registry. The planned executor validates edits atomically during reload or
restart, then supplies the same snapshot to connected MCP frontends. At each
stage boundary, it checks permissions and the registration digest. A subprocess
already running follows the action limits and cleanup rules recorded for it.

To calculate the registration digest, the executor fills in target defaults,
then includes the target entry, registry ID, resolved nonsecret endpoints,
adapter settings and recipe input identities. It excludes secret values.
Rebinding an alias invalidates plans prepared for the previous binding.

When only a credential's value changes, the executor repeats access checks.
The unchanged credential reference does not count as a change to the plan's
hardware or model inputs.

Do not reuse a target ID for a different deployment. The executor rejects
removal while the target has active, cancelling or recovery-required work.
Completed records retain their target snapshot.

V1 does not automatically expire plans, operation records, deduplication entries
or evidence from failed attempts. The operator may archive evidence after
resolving its ownership and dependencies. Request-ID tombstones remain so an
old retry cannot create another operation. Tools report removed evidence as
`artifact_missing`.
