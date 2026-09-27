# Deploy a fleet through MCP

The unreleased `ssh-v1` adapter installs and qualifies Narwhal on existing GPU
hosts through Gates A–G. Run the MCP server on a Linux management workstation.
The engine hosts supply the GPUs, container runtime and model files.
Live qualification of this unreleased adapter remains pending; the outcomes
below define what each operation must verify.

Version 1 requires at least two numbered engines, homogeneous accelerator,
tensor-parallel and transport settings, and disjoint GPU allocations. Engine
`n1` maps to host role `engine-1`, and so on. The router listens on loopback on
the host assigned the `router` role. The adapter supports individual engine
replacement on an owned standalone router. Cloud provisioning, shared-device
deployments and router failover are outside this adapter's scope.

## Prerequisites

Prepare the host inventory, fleet file and engine launch configuration described
in [Gate A: Discover](../deploy/01-Discover.md). Select an immutable image digest
and an existing model directory on each engine host. The checked launcher must
support that image and accelerator. Each launch must declare a positive
`--max-num-seqs` value for profiling limits.

Each checkpoint check reads the full set of model files. Plan preparation checks
each host; launch repeats the check for the selected engines. The SSH worker
hashes up to four files concurrently and fails the check if a file changes or
its deadline expires.

The management workstation needs Git, OpenSSH and the installed MCP extra.
Password authentication also needs `sshpass`. Register a known-hosts file with
the expected host keys. The adapter disables SSH configuration files and checks
the registered keys on every connection.

The hosts need Python 3.11 or newer, Git, Make, curl, `ip`, virtual-environment
support, Docker and the GPU
runtime tools required by the selected launch. The router host also needs
Docker Compose for Prometheus and Grafana. Install the fabric measurement tools
for the selected [TCP or RDMA workflow](../deploy/04-Qualify-Fabric.md).
Hosts need access to the package dependencies used during installation.

Install a wheel built from a clean commit containing this feature. Retain a
checkout of that same commit on the management workstation: the adapter checks
the installed package provenance and the checkout's deployment, measurement and
monitoring assets. Set `source_root` to that checkout and `source_commit` to its
full commit SHA. The checkout and its inputs must belong to the management
user and deny group and other write access. An editable installation cannot
authorize fleet mutation.

Stop client traffic before measurement or replacement and keep it stopped until
the operation finishes. Reservations exclude cooperating management operations.
The adapter checks current engine generations and idle counters; those checks
cannot prevent a separate client from sending a later request.

## 1. Register the fleet

Create a private directory under the repository's ignored `runs/` location on
the management workstation. Keep the registry, settings, recipe, host inventory
and deployment inputs there. Use absolute paths, mode `0700` for private
directories and `0600` for private files. Follow the complete
[registration and permissions contract](Registration.md) when creating the
registry.

Set the target's `kind` to `fleet`, its `fleet_file` to the prepared fleet file,
and its adapter to `{"id": "ssh-v1", "settings_path": "/absolute/settings.json"}`.
Replace that path with your settings file. Register a fleet recipe under a
stable ID such as `measured-pair`. Grant `inspect`, `measure` and `mutate`, and
allow only the actions needed for this deployment:

```json
[
  "fleet_deploy",
  "fleet_profile",
  "fleet_preflight",
  "engine_replace",
  "monitoring_start",
  "deployment_cleanup"
]
```

Write a settings document with these required fields. Replace every path and
the commit placeholder before use. `remote_root` is the private absolute parent
directory for operation inputs, source checkouts and evidence on each host.

```json
{
  "schema": "narwhal.ssh-settings",
  "schema_version": 1,
  "source_root": "/absolute/verified-checkout",
  "source_commit": "FULL_40_CHARACTER_COMMIT_SHA",
  "hosts_path": "/absolute/hosts.json",
  "launch_path": "/absolute/engine-launch.json",
  "known_hosts_path": "/absolute/ssh.known_hosts",
  "remote_root": "/absolute/private/narwhal-managed"
}
```

The host inventory maps each host alias to SSH environment-variable names and
roles. This example uses two engine hosts and a separate router host. Supply
the three SSH destinations to the server's environment before startup.

```json
{
  "hosts": [
    {"id": "node-a", "ssh_env": "FLEET_NODE_A_SSH", "roles": ["engine-1"]},
    {"id": "node-b", "ssh_env": "FLEET_NODE_B_SSH", "roles": ["engine-2"]},
    {"id": "router", "ssh_env": "FLEET_ROUTER_SSH", "roles": ["router"]}
  ]
}
```

If a host uses password authentication, add its `password_env` name and include
that name in the target's `credential_env`. Passwords remain in the server's
environment. Tool arguments and JSON input documents contain references.
You can place a router and an engine on the same host when their listener
ports and other reserved resources do not overlap.

Write the recipe with schema `narwhal.ssh-recipe`, version `1`. Its `environment`
object contains fixed nonsecret `NARWHAL_*` deployment values. Alternatively,
`environment_refs` maps a deployment variable to the server environment
variable that supplies its value. Populate the
[deployment environment inputs](../configuration/04-Deployment-Inputs.md),
including image, model, interface and port settings. Node-specific settings take precedence
over shared engine settings. Register any credential reference in
`credential_env`.

Select the recipe's `profiling`, `fabric` and `load` values before preparing a
plan. Use the profiling domain and SLO established for the intended workload.
The load recipe requires at least two distinct arrival rates. Defaults are
input choices; they do not establish capacity for your hardware or model. The
installed `ssh-recipe-v1.schema.json` and `ssh-settings-v1.schema.json` define
the fields, accepted ranges and defaults. From your private working directory,
extract them with the installed environment's Python:

```bash
python - <<'PY'
from importlib.resources import files
from pathlib import Path

package = files("narwhal.deployment")
for name in ("ssh-recipe-v1.schema.json", "ssh-settings-v1.schema.json"):
    with Path(name).open("xb") as output:
        output.write(package.joinpath(name).read_bytes())
PY
```

The settings default to 10 seconds for SSH connection establishment and 300
seconds for a command exchange. Discovery, attestation, preflight, serving and
cleanup stages each default to 300,000 ms of action time. Installation, engine
launch, fabric, profiling and workload stages each default to 3,600,000 ms.
Every stage also reserves 10,000 ms for TERM, 5,000 ms for KILL and 30,000 ms
for reconciliation. Select finite budgets that cover your registered workload
before preparing the plan.

The load recipe reserves management-workstation ports `18000`, `19090` and
`13000` for its router, Prometheus and Grafana forwards by default. These ports
must be free when you prepare a new deployment. The recipe can select other
distinct ports. Fleet status and monitoring inspection use the target's
registered endpoint variables; keep a separate verified access path available
when inspecting loopback services outside a workload stage.

## 2. Prepare and inspect the plan

Start the [MCP client](../cli/MCP.md#3-configure-a-client) with the installed
`narwhal-mcp` executable and your registry. Call `target_list` and confirm the
target and action list. Then call `plan_prepare` with your target ID,
`action: "fleet_deploy"`, `parameters: {"recipe_id": "measured-pair"}` and a
new UUID as `request_id`.

Inspect the returned operation until preparation finishes. A successful
preparation returns `result.data.plan_id`. Call `plan_inspect` with that ID and
read its input artifacts. Check the physical hosts and GPUs, image and model
identities, source commit, ports, measurement settings and stage budgets.

Preparation checks registered files, SSH access, hardware, model contents,
images, interfaces and resource conflicts. A successful preparation establishes
those observations at that time. Execution checks the bindings again before
performing the affected stages.

## 3. Execute Gates A–G

Call `plan_execute` with the target, inspected plan ID and a new request UUID.
Retain the returned operation ID. Use `operation_inspect` to follow its stage
records and read exported evidence through `artifact_read`.

| Gate | Required result |
| --- | --- |
| A | The recorded host discovery still matches. |
| B | The bound revision installs successfully, first on the host carrying engine 1. |
| C | Checked engines start and return live process and cache evidence. |
| D | Serial directed fabric measurements meet the recorded transfer budget. |
| E | Live attestation identifies the running engine generations. |
| F | Measured profiles and full KV preflight pass. |
| G | The router and monitoring start; recorded load trials, journal reconciliation, post-load KV checks and final monitoring acceptance pass. |

Each stage must finish before its dependent stage starts. The adapter retains
verified engines, sidecars, router and monitoring services across stage
boundaries. Gate G runs clients through owned SSH forwards and checks the
registered workload at every selected rate. Missing evidence, failed workload
acceptance or unavailable required monitoring prevents deployment success.

An operation with `state: "succeeded"` establishes qualification for its recorded
configuration, generations and workload. The result does not establish capacity
at other request rates, context lengths or runtime settings.

## Replace one engine

With client traffic stopped, prepare `engine_replace` with
`parameters: {"engine_id": "n2"}` for the selected engine. Inspect and execute
the resulting plan. The adapter requires an existing owned deployment, an
`individual` restart policy and a standalone router.

The adapter drains the selected engine and waits for permission to stop it.
It stops that engine and its attestation service, then repeats launch, cache,
fabric, attestation, selected-engine profiling and full preflight. Unchanged
engines keep their measured profile rows and samples.

The router loads profiles at startup. The adapter therefore captures its idle
handoff, restarts the owned router with the complete updated profile store and
`--resume`, and verifies that the original drain hold survived. Readmission
follows that check. Load, post-load KV and monitoring acceptance must pass
before the replacement operation succeeds.

`fleet_profile` creates measurement evidence without activating it in the
running router. `fleet_preflight` checks the current recorded runtime fleet.
Use the replacement workflow to refresh and activate a changed engine's
generation-bound profiles.

## Recover or clean up an operation

After a client disconnect, reconnect and inspect the retained operation ID.
Disconnecting does not cancel accepted work. Use `operation_cancel` to request
cancellation. Remote supervisors enforce recorded deadlines and attempt cleanup
of their owned helpers. A lost SSH connection cannot prove remote cleanup.

On `command_failed`, use `artifact_read` to inspect the `ssh_command_output`
artifact when present. The adapter attempts to export the first 65,536 bytes of
each remote stdout and stderr log, with credentials, request content and private
paths redacted. Each stream reports truncation or an explicit omission; either
condition marks the artifact `complete: false`. Output collection uses the
remaining stage deadline; collection failure preserves the original command
error.

If an operation reports `recovery_required`, restore host access and inspect
its reconciled effects. The coordinator retains reservations while effects or
ownership remain unknown. Follow [Operations and recovery](Operations.md) for
the conditions under which a fresh plan can resume execution.

To stop resources owned by an operation, prepare `deployment_cleanup` with
`parameters: {"operation_id": "UUID_OF_THE_OPERATION"}`. Replace the placeholder
with the retained execution ID, inspect the plan and execute it. Cleanup stops
the selected owned processes and containers. It preserves private inputs,
source checkouts, gate evidence and monitoring data volumes for inspection.
The cleanup result reports removed and residual resources and retained evidence.
Successful cleanup does not restore an earlier deployment generation.

If cleanup itself is interrupted, select that cleanup operation in a new cleanup
plan. The coordinator verifies the previous worker's absence before transferring
its exact reservations. An unreachable host or uncertain process identity keeps
the operation in recovery until those observations can be established.

### Resume a failed deployment

If deployment preparation reports `resource_conflict` with the message
`A recorded fleet must be cleaned up before a new deployment`, clean up the
earlier failed deployment before resuming it:

1. Complete the cleanup procedure above using the failed deployment's execution
   ID. Wait until the cleanup operation reports `state: "succeeded"`.
2. Prepare `fleet_deploy` again with the registered recipe and inspect the new
   plan.
3. Call `operation_resume` with the failed deployment's execution ID and the
   new plan ID. The resumed operation records its parent and reruns every stage.

The failed execution and its evidence remain available after cleanup and
resumption.
