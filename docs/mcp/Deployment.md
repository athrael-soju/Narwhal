# MCP deployment plans and site adapters

This page specifies the proposed version 1 deployment plan and site adapter
interfaces for implementers of the [MCP milestone](../MCP-Contracts.md).
These interfaces are not available yet. Operators deploying a fleet use
[Deploy a fleet](../Deploy.md).

A plan fixes the inputs, stages and execution budgets for one registered target.
The deployment executor checks those recorded inputs before it starts work.
Site adapters perform host operations and return evidence. The executor orders
deployment gates, prevents conflicting operations, and owns operation state and
recovery.

## Prepare and execute a plan

`plan_prepare(target_id, action, parameters, request_id)` starts a preparation
operation. The executor resolves the
[registered target and recipe](Registration.md), checks adapter prerequisites,
and collects current inputs. The completed operation returns
`result.data.plan_id` when preparation succeeds.

During preparation, the adapter may write private local evidence and run remote
inspection helpers within recorded deadlines. Its manifest declares each
inspection call. Preparation must finish cleanup of those helpers before
returning a usable plan. Host inventory may query devices through `nvidia-smi`
or `rocminfo` over the registered SSH connection. An inspection helper must not
allocate a workload, change device state or alter host configuration.

Image-introspection containers receive no GPUs, disable network access and use
only read-only host mounts. Their writable state is restricted to their own
disposable container filesystem. Preparation records ownership and finite
cleanup budgets. An image helper needing device exposure, host writes or a
workload requires a separately authorised action or suitable retained
introspection evidence; it cannot run as an inspection helper.

Current discovery runs a temporary `docker run --rm --network none` container
with a Python entry point to read image package metadata. The adapter must add
the recorded ownership and deadlines required here before exposing that helper
through plan preparation.

`plan_inspect(target_id, plan_id)` returns the plan's public projection: action,
target, input identities, stages, budgets and expected resource changes. The
result includes `input_artifacts: [{name, artifact_id}]` for every bound input
and `snapshot_artifact_id` for the preparation snapshot.

Read these immutable, redacted exports with `artifact_read` to review the plan.
They retain host aliases, GPU allocations, tensor-parallel shape, model/image
identity, effective nonsecret configuration, workload and budget details.
They exclude private destinations and secret values. If required review
evidence is missing, inspection reports the failure before offering execution.

`plan_execute(target_id, plan_id, request_id)` checks the plan digest and current
binding while holding the required locks, then returns an execution operation.
A plan binds one execution; repeating its execution returns that operation.
A new attempt needs a new plan. Request deduplication, cancellation and recovery
follow [Operations](Operations.md).

The executor also checks each stage's prerequisites immediately before that
stage. A passed preparation check does not reserve a port, guarantee remote
access, or prove that a model directory remains unchanged. A changed binding
stops execution before the affected side effect and identifies the input that
requires a fresh plan.

The executor checks changes produced by earlier stages against their recorded
receipts. After an engine launch, it verifies the engine against the recorded
launch identity. That engine's captured cache then becomes an input to fabric
qualification. An unexplained process replacement cannot qualify as an expected
stage effect.

## Immutable plan record

Every plan writer emits the required fields below. Readers retain compatible
added fields within the supported version and include them when checking the
stored payload digest. Readers reject unsupported document versions.

| Field | Type and meaning |
| --- | --- |
| `schema` | Literal `narwhal.deployment-plan`. |
| `schema_version` | Integer `1`. |
| `plan_id` | UUID assigned when the plan is committed. |
| `plan_digest` | Lowercase SHA-256 hex digest of canonical `payload`. |
| `created_at` | UTC RFC 3339 timestamp. |
| `payload` | Object containing exactly `action`, `target_id`, `binding`, `parameters`, `stages`. |

Canonical payload bytes use UTF-8 JSON with sorted object keys, no insignificant
whitespace, `ensure_ascii=false`, and no trailing newline. Arrays retain their
recorded order. Only strings, booleans, null, integers, arrays and objects are
allowed; floating-point numbers are rejected. Numeric durations use integer
milliseconds. Input documents that contain other numeric values are stored as
immutable byte blobs and bound by SHA-256, preserving their existing contracts.

`action` is one of the [plan actions](#actions-and-gate-results). `target_id` is
a registered alias. `parameters` is the validated object from the tool request.
The versioned adapter recipe generates `stages` as a nonempty ordered array.
Tool arguments and resumed operations cannot replace individual stages.

The following is a structural projection, with digest values abbreviated:

```json
{
  "schema": "narwhal.deployment-plan",
  "schema_version": 1,
  "plan_id": "8bc35a67-ae35-4c79-a214-0b623225f21e",
  "plan_digest": "<64 lowercase hex characters>",
  "created_at": "2026-09-26T12:00:00Z",
  "payload": {
    "action": "fleet_deploy",
    "target_id": "trial-fleet",
    "binding": "<binding object defined below>",
    "parameters": {"recipe_id": "trial-v1"},
    "stages": ["<ordered stage records defined below>"]
  }
}
```

The executor commits each plan atomically to the private state store and
addresses each input blob by its SHA-256 digest. Plan inspection redacts private
destinations, paths and credential references according to
[Tools and results](Tools.md). The returned `plan_digest` identifies the private
canonical payload. Execution loads that stored private record; the inspected
projection is not an executable input.

### Binding fields

All fields below are required. Nullable values are allowed only where stated.

| Field | Contract |
| --- | --- |
| `registration_digest` | SHA-256 of the target registration and referenced adapter settings at preparation, excluding credential values. |
| `adapter` | `{id, version, assets_sha256}`: registered adapter, exact implementation version, digest of its asset manifest. |
| `source` | `{commit, distribution_version, wheel_sha256, bundle_sha256}`: approved source and installed artifact identities. |
| `recipe` | `{recipe_id, sha256}` for the selected immutable recipe, or null under the condition below. |
| `snapshot_id` | UUID of the preparation snapshot. |
| `snapshot_sha256` | Digest of that snapshot's immutable bytes. |
| `identity_sha256` | Digest of the snapshot's stable identity projection, using the plan's canonical JSON encoding. |
| `inputs` | Array of `{name, sha256}` records, sorted by unique `name`, referencing the private input store. |

`source.commit` is the full approved Git commit, and
`source.distribution_version` is the Narwhal version. An unused wheel or bundle
digest is null; at least one artifact digest must be present.

`recipe` is null only when an action derives all its stages from the registered
target and retained deployment.

Input names are `fleet_config`, `launch_config`, `host_inventory`,
`model_identity`, `runtime_identity`, `network`, `service_policy`,
`measurement_recipe`, `load_recipe`, `credential_refs`, `resource_ownership` and
`cleanup_selection`. The action's required inputs must be present. Inapplicable
inputs are omitted. No input contains credential values.

The adapter records the following identities in the immutable input documents:

- `host_inventory`: stable host aliases, host boot and SSH host-key identities,
  role placement, observed accelerator products, GPU UUID or PCI identity,
  selected device mappings and tensor-parallel shape.
- `model_identity`: source and immutable revision, served name, configuration
  digest, checkpoint manifest and `model_tree_sha256`. For a non-Git model source,
  the revision is its immutable object version and manifest digest.
- `runtime_identity`: image content identity and registry digest when present,
  runtime package versions, model arguments, environment policy and current
  engine generation identities when the action requires running engines.
- `network`: fabric interfaces, addresses, routes, transport, transfer devices,
  HCA/port/GID selections where applicable, planned listeners and access path.
- `service_policy`: exact SLOs, first-token and request deadlines, profile error
  policy, and qualification acceptance criteria.
- `measurement_recipe` and `load_recipe`: fixed workload, sequence/concurrency
  points, rate points, request counts, sample limits and finite stage budgets.
- `resource_ownership`: recorded deployment/instance ownership for existing
  processes, containers, services and remote paths affected by the action.
- `cleanup_selection`: exact resources eligible for cleanup and their owning
  operation; required only for `deployment_cleanup`.

`fleet_config`, `launch_config` and `credential_refs` preserve the resolved
nonsecret documents and environment variable references used by the adapter.
Image tags, a branch name and a mutable model alias do not satisfy identity
requirements. An installed distribution's version alone does not establish its
source commit: preparation requires matching artifact provenance. The MCP
milestone must supply and verify that provenance before enabling the action.

The preparation snapshot has schema `narwhal.management-snapshot`, version `1`,
with `snapshot_id`, `observed_at`, `identity` and `observations`. `identity`
contains stable host/boot/SSH trust identities, selected GPU identities and
allocations, model/runtime identities, bound configuration digests, network
configuration and applicable engine generations, as enumerated above.
`observations` records readiness, free memory, counters, resident work and
per-source timestamps. The adapter version fixes the projection fields and
their normalisation; missing required identity fields fail preparation.

The executor checks `snapshot_sha256` and input byte digests to verify the
integrity of retained records. It then compares the stable `identity` projection
and bound configuration against current observations. It separately checks
volatile preconditions such as free memory, admission and resident work.
New timestamps, advancing counters and changed current load do not by themselves
change plan identity.

Before reusing a completed stage, the executor checks retained bytes for
integrity and verifies that stable prerequisites still match. Fresh readiness
and idleness checks determine whether the next action may run. The executor
retains each fresh snapshot with its own hash and preserves the original
evidence.

### Stage fields

Each stage contains exactly these fields:

| Field | Contract |
| --- | --- |
| `stage_id` | Unique alias within the plan. |
| `gate` | `A` through `G` for fleet deployment; null for standalone actions. |
| `operation` | Adapter operation identifier from its versioned capability manifest. |
| `depends_on` | Array of earlier stage IDs; cycles and unknown IDs are invalid. |
| `subjects` | Array of registered host/engine aliases or the target alias. |
| `input_names` | Names from `binding.inputs` required by this stage. |
| `timeout_ms` | Positive integer execution budget. |
| `cleanup` | `{policy, term_grace_ms, kill_grace_ms, reconcile_ms}`, with the constraints below. |
| `retain_on_success` | Array of resource kinds to transfer into the deployment's ownership record. Empty when nothing persists. |

`cleanup.policy` is `temporary_only` or `owned_stage_resources`. Each cleanup
budget is a positive integer in milliseconds.

Resource kinds are `engine`, `attestation`, `router`, `monitoring`, `tunnel`,
`measurement_helper`, `installation` and `private_artifact`. Cleanup policy
controls only resources created by that stage. Existing services require an
explicit action with verified ownership. Artifact retention follows
[Operations](Operations.md); cancellation does not erase failure evidence.

The adapter's capability manifest fixes the meaning, expected output and
exclusion requirements of each operation identifier. A recipe selects supported
operations and budgets; it cannot supply shell text or executable paths. Before
acceptance, the executor verifies that every operation and resource kind is
supported by the exact adapter version.

The existing stage helper defaults to 300 seconds for execution, 10 seconds for
SIGTERM cleanup and 5 seconds for SIGKILL cleanup. Docker reconciliation has a
separate 30-second default. See [stage deadlines](../Dev-Runtime.md#stage-deadlines-and-recovery)
for their current scope. The adapter records the resolved values in the plan
and preserves bounded nested helpers. Fleet recipes must declare budgets for
the additional remote and measurement stages. An agent cannot increase them,
concurrency or batch sizes based only on available headroom.

The overall execution budget is the sum of every stage's `timeout_ms`,
`term_grace_ms`, `kill_grace_ms` and `reconcile_ms`. It starts when execution
begins. Before starting a stage, the executor checks that its execution and
cleanup budgets fit within the remaining time. Permitted concurrency may
shorten the actual operation. The recorded outer budgets must include nested
commands and their cleanup, including an interrupted Docker client's final
termination grace.

## Actions and gate results

| Action | Parameters | Completion establishes |
| --- | --- | --- |
| `dev_init` | `{recipe_id}` | The selected installed recipe produced a validated private instance. |
| `dev_up` | `{}` | Startup, attestation, profiling and router launch completed for the current instance. |
| `dev_verify` | `{}` | Current dev verification passed, including directed KV preflight and a routed completion. |
| `dev_down` | `{}` | The current instance's recorded ownership set was stopped and inspected. |
| `fleet_deploy` | `{recipe_id}` | Every required gate A–G passed for the recorded workload and inputs. |
| `fleet_profile` | `{engine_ids?}` | Selected idle engine generations were profiled with the registered recipe. |
| `fleet_preflight` | `{}` | Full configured preflight passed against the current fleet and profiles. |
| `engine_replace` | `{engine_id}` | The selected replacement generation passed requalification before eligibility was restored. |
| `monitoring_start` | `{}` | The selected monitoring stack passed identity, scrape and dashboard checks. |
| `deployment_cleanup` | `{operation_id}` | Only the plan's enumerated resources owned by that operation were removed or verified absent. |

For `fleet_profile`, omitted `engine_ids` selects all configured engines; a supplied
list must be nonempty and contain unique configured engine IDs. Publication of
profiles preserves unaffected engines and refuses a combined store with stale
generations. The executor establishes engine idleness and excludes conflicting
operations before profiling or transfer qualification.

The `fleet_deploy` recipe expands into the following ordered gates. Parallel
work is permitted only inside a gate where the runbook permits it.

| Gate | Required result |
| --- | --- |
| [A: Discover](../deploy/01-Discover.md) | Inputs, host inventory, access, image and complete checkpoint identity agree. |
| [B: Install](../deploy/02-Install.md) | The installer verifies approved source and prepared role artifacts on each host. |
| [C: Engines](../deploy/03-Validate-Engines.md) | Allocations and listeners are validated; each checked engine starts and its live cache is captured. |
| [D: Fabric](../deploy/04-Qualify-Fabric.md) | Required directed links pass against budgets derived from the serving cache. |
| [E: Attest](../deploy/05-Attest.md) | Attestations bind the current processes, cache, model and transfer contract. |
| [F: Profile and preflight](../deploy/06-Profile-and-Preflight.md) | Current generations have usable profiles and every required preflight gate passes with the bound SLOs. |
| [G: Serve and measure](../deploy/07-Serve-and-Measure.md) | Routed load, client/journal reconciliation, Prometheus scrapes, required Grafana series and the post-load KV ring all pass. |

At Gate B, the adapter must prove installation on engine 1's host before
installing on the remaining hosts. At Gate D, it measures one directed edge at
a time while the engines remain idle.

At Gate G, the executor binds the trial path and both initial rate points from
the selected recipe. It retains client resource observations to identify
load-generator or SSH-path saturation. Gate completion requires every result
listed above; router readiness alone is insufficient.

After Gate G passes, the executor retains engines, attestation sidecars, router
and monitoring for service. The adapter stops temporary clients and tunnels
according to the plan.

During execution, the adapter produces operation evidence: live cache geometry,
process identities, fabric budgets, profiles and gate outputs. Each output
records the plan digest and exact upstream evidence digests. The immutable plan
remains unchanged. If profiling shows that the selected SLOs need revision, the
executor stops the operation and retains that evidence. Revised SLOs require a
new plan.

## Invalidation and evidence reuse

Changing any bound input requires a newly prepared plan. Resumption creates a
new operation with `parent_operation_id` and a fresh, unconsumed plan. The
original failed or cancelled attempt remains recorded. For an interrupted
attempt, the executor must first reconcile its effects and reach one of those
terminal states.

The executor may reuse a completed stage only when its evidence has a passing
verdict and each input digest, applicable live identity, adapter version and
prerequisite verdict still matches. Missing or ambiguous evidence cannot satisfy
a gate.

| Changed input | Evidence that must be refreshed |
| --- | --- |
| Engine process generation with unchanged launch inputs | Live cache capture, attestation, that generation's profile and full preflight; recompute fabric budget if cache geometry changes. |
| Checkpoint, image, runtime, allocation, cache or launch policy | Affected discovery and launch checks, engine generation and all downstream dependent measurements. |
| Fabric route, host assignment or transport | Affected directed link samples and live KV preflight. |
| SLO or first-token deadline | Preflight for the revised policy; unchanged generation profiles remain eligible. |
| Offered rate/count within measured coverage | New load evidence and post-load checks after drain; qualifying engine/fabric/profile evidence remains eligible. |
| Router restart with the same fleet document | Live profile-generation match, router readiness and affected service checks. |
| Credential value rotation under the same reference | Recheck access; input identity is unchanged when destination, principal and capabilities still match. |

A restarted engine cannot inherit its predecessor's profile solely because its
logical `engine_id` and image match. If a process survives an interrupted launch,
the executor must reconcile its identity before retrying launch or releasing
its GPU and port reservation.

## Site adapter contract

Version 1 defines two adapters. `local-dev-v1` calls the installed
[dev commands](../cli/Dev.md) for the supported
Ubuntu/WSL2 CUDA runtime and registered recipe. `ssh-v1` operates existing,
registered Linux hosts over verified SSH, following Gates A–G. Cloud account
provisioning and allocation of new virtual machines require a separate adapter.

Every adapter publishes an immutable manifest containing `id`, `version`,
supported actions, operation identifiers, required platform capabilities,
resource kinds, asset hashes and available recovery methods. It also names and
ships versioned JSON Schemas for its settings and recipe input formats.

Before mutation, the executor checks that the adapter supports the selected
action and its prerequisites are present. A missing prerequisite or unsupported
action produces a named failure with retained inspection evidence.

For `local-dev-v1`, the registered recipe is the existing `narwhal.dev-template`
version 1 document consumed by dev initialization. The adapter's settings schema
limits initialization overrides to the [dev CLI inputs](../cli/Dev.md), including
model/tokenizer paths, GPU selection and interface. A null settings file selects
the documented CLI defaults.

For `ssh-v1`, the recipe supplies the nonsecret
[deployment environment inputs](../configuration/04-Deployment-Inputs.md) and
references the existing host, launch and fleet documents. Its settings schema
declares source location, SSH trust, supervision method, fixed measurement/load
recipes and stage budgets. The manifest must enumerate those accepted fields,
their defaults and validators; unsupported settings fail before preparation.
Environment files containing secrets remain outside the immutable input store.
During preparation, the adapter captures their nonsecret values and credential
references separately.

The executor passes an internal context to every adapter call. It contains
target and operation IDs, the plan digest where one exists, stage ID, fencing
token, absolute deadline and a private output directory. The worker resolves
credential values in its own environment.

Each adapter response contains typed `data`, artifact references, observed
resource identities and a structured error or null. The executor retains every
invoked CLI result unchanged under the
[command result contract](../Command-Results.md).

| Interface | Inputs beyond context | Required output |
| --- | --- | --- |
| `snapshot` | Registered target and recipe references | Immutable host, artifact, runtime and access observations with a snapshot ID/digest. |
| `check` | Snapshot or plan binding and selected capability | Named prerequisite verdicts, tool versions, supported/unsupported action details. |
| `prepare` | Validated plan and stage | Verified package/role artifacts, hashes and destination manifest. |
| `launch` | Checked stage inputs and unique launch token | Owned process/container/service identity, launch evidence and readiness result. |
| `status` | Recorded resource identities | Observation time and `present`, `absent`, `mismatch` or `unknown` for each identity. |
| `measure` | Fixed recipe, current engine identities and required exclusion token | Samples, bounds, provenance and gate verdict. |
| `collect` | Registered evidence kind, subjects and byte/time limits | Bounded artifact manifest and explicit omissions/truncation. |
| `stop` | Enumerated owned identities and cleanup budgets | Removed, absent, surviving or unknown resources and cleanup evidence. |

These Python adapter interfaces use operation identifiers declared by the
adapter manifest for installation, attestation, router supervision, monitoring
and tunnels. They add no public shell commands. The `measure` interface follows
its fixed recipe without automatic tuning.

When an adapter creates a resource, it records ownership before later stages
depend on that resource. Linux identities include host boot ID, PID and start
ticks. Container ownership includes daemon host, container ID and Narwhal
launch/operation labels. A failed observation returns `unknown`; it does not
prove absence or authorise stopping an unrelated process.

Each resource receipt contains these fields:

| Field | Contract |
| --- | --- |
| `resource_id` | The coordinator's canonical resource key. |
| `kind` | One of the [stage resource kinds](#stage-fields). |
| `host_id` | Registered host alias. |
| `owner` | `{operation_id, stage_id, launch_token}`. |
| `identity` | Identity specific to the resource kind, or null before it is established. |
| `effect` | `confirmed`, `absent` or `unknown`. |
| `observed_at` | UTC RFC 3339 timestamp. |

`owner.launch_token` is null for resources that are not launched. `identity`
contains the resource's PID, container or service identity, or a path and digest
for an installation or artifact. It is null only while the intended resource's
identity has not been established. An unknown receipt keeps the operation in
`recovery_required` until reconciliation establishes its effect.

### Prerequisites and installed assets

For `ssh-v1`, validate the prerequisites required by the current SSH runbook:

- The management workstation requires Git, Bash, Python 3.11+, OpenSSH and the
  supplied access helpers. Password SSH also requires `sshpass`.
- Hosts running Narwhal commands require Python 3.11+ with `venv`, Git, Make and
  curl.
- Discovery requires Docker, `ip`, `rocminfo` or `nvidia-smi`, the pinned engine
  image and staged model. Engine hosts need the selected driver,
  container/device access and transfer devices.
- Fabric qualification requires `iperf3` for TCP or `ib_write_bw` from `perftest`
  for RDMA.
- The monitoring host requires Docker Engine and the Compose plugin.

The adapter checks listeners, routes, devices and SSH host keys on the exact
target host specified in the runbook.

`local-dev-v1` checks the registered recipe's native CUDA, Python package,
model, tokenizer, GPU and interface requirements through the installed dev
implementation. See [CUDA runtime preparation](../dev/CUDA-Runtime.md). A passed
synthetic adapter test cannot establish GPU fit, transfer compatibility or
recovery on that runtime.

An installed MCP wheel must not assume a repository `tools/` directory exists.
The first `ssh-v1` implementation uses an explicitly registered, verified source
checkout or extracted approved source bundle for the existing deployment,
measurement and observability helpers. Preparation checks its full commit and
all required assets before any remote installation. Missing assets disable the
affected action with their names in the error.

The asset manifest must include deployment/discovery/access helpers and their
package dependencies; the engine launcher and cache capture hook; fabric budget
tooling; load and evidence helpers; the Compose file, Prometheus configuration
and rules; the Grafana dashboard and provisioning files. Package-backed launcher
symlinks in a checkout must resolve to verified files when producing a bundle.
Installed dev templates are read through package resources. A later adapter may
package the remaining assets, but must retain the same manifest and installed
artifact verification contract.

Adapter qualification must exercise the server outside a checkout and verify
that missing prerequisites fail before mutation. It must also cover supported
host/recipe combinations, full Gate G evidence, and recovery of interrupted
operations using real process ownership. A deployment recipe requires live GPU
and fleet qualification before it can be declared supported. Use the
[worked cases](Worked-Cases.md) to select the contract scenarios to test.
