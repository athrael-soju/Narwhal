# Manage a local dev instance through MCP

The unreleased `local-dev-v1` adapter runs `dev init`, `up`, `verify` and `down`
through persistent operations. Use `dev_status` to inspect an instance,
including one created by the CLI. This guide covers a local NVIDIA CUDA
instance on Ubuntu or Ubuntu under WSL2. Live GPU qualification of this MCP
path remains pending.

## Prerequisites

Install the [dev CUDA runtime and pinned model](../dev/CUDA-Runtime.md) first.
Use that Python environment for the MCP server and every command below. An
existing instance records its Python environment; the adapter rejects another
environment before execution.

Managed execution requires a wheel built from a clean commit containing this
feature. The package carries its full commit, source bundle and file hashes.
The adapter verifies the installed files against that bundle when it prepares
or executes a plan. Editable installations and packages built from a dirty
checkout cannot authorize managed execution.

At the repository root, confirm that `git status --porcelain` prints no changes,
then install the package and MCP dependency in the activated CUDA environment:

```bash
git status --porcelain
python -m pip install '.[mcp]'
narwhal-mcp --version
```

The model, tokenizer, GPU, interface and ports must satisfy the selected dev
template. The default template and its checks are described in
[Narwhal dev](../Dev-Runtime.md). Preparation inspects those prerequisites;
startup and verification still need the GPU.

## 1. Register the instance

From the repository root, create a private registry, adapter settings and a
copy of the installed two-engine template. This script selects the existing
CLI defaults and refuses to reuse its working directory:

```bash
python - <<'PY'
import json
import os
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

os.umask(0o077)
root = Path("runs/mcp-dev").resolve()
root.mkdir(parents=True, mode=0o700, exist_ok=False)
(root / "recipe.json").write_bytes(
    files("narwhal.dev").joinpath("small-cuda-v1.json").read_bytes()
)
(root / "settings.json").write_text(json.dumps({
    "schema": "narwhal.local-dev-settings",
    "schema_version": 1,
    "init": {},
}) + "\n")
registry = {
    "schema": "narwhal.management-registry",
    "schema_version": 1,
    "registry_id": str(uuid4()),
    "state_dir": str(root / "state"),
    "targets": [{
        "id": "local-dev",
        "kind": "dev",
        "working_directory": str(root),
        "artifact_root": str(root / "artifacts"),
        "fleet_file": None,
        "instance_dir": str(root / "instance"),
        "adapter": {
            "id": "local-dev-v1",
            "settings_path": str(root / "settings.json"),
        },
        "capabilities": ["inspect", "measure", "mutate"],
        "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
        "recipes": [{
            "id": "small-cuda",
            "kind": "dev",
            "path": str(root / "recipe.json"),
        }],
    }],
}
path = root / "registry.json"
path.write_text(json.dumps(registry, indent=2) + "\n")
print(path)
PY
```

If the model is outside the default cache or you need explicit GPU, interface
or port selection, edit `settings.json` using the [settings reference](#settings)
before preparing a plan. Keep all files private and retain `registry_id` when
editing the registry.

For an existing CLI instance, set `instance_dir` to its absolute directory
before starting the server. The adapter reads its saved inputs and ownership.
Start with `dev_status`; initialization is unnecessary. A new registration does
not grant ownership of an unrelated process.

## 2. Connect the client

Set your MCP client's stdio executable to the absolute path of `narwhal-mcp`
in the CUDA environment. Pass `--registry` and the registry path printed above
as separate arguments. See [Configure a client](../cli/MCP.md#3-configure-a-client)
for the launcher and SDK discovery procedure.

Call `target_list`. Its `data.targets` array must include `local-dev` with the
four registered actions. The tool list includes `dev_status`, `plan_prepare`,
`plan_inspect`, `plan_execute` and the operation tools. Discovery proves that
the client can reach the server and read the registration.

## 3. Initialize the instance

Ask the client to prepare `dev_init` for `local-dev` with
`parameters: {"recipe_id": "small-cuda"}` and a new UUID as `request_id`.
For example, the `plan_prepare` arguments have this shape; replace the request
ID with a newly generated UUID for each new request:

```json
{
  "target_id": "local-dev",
  "action": "dev_init",
  "parameters": {"recipe_id": "small-cuda"},
  "request_id": "598f92c5-13d3-4b93-aa59-ae3b13917980"
}
```

1. Poll `operation_inspect` with `target_id: "local-dev"` and the returned
   `operation_id`. When
   `data.operation.state` is `succeeded`, take the plan ID from
   `data.operation.result_data.plan_id`. Preparation failure leaves its error
   codes and evidence in the operation record.
2. Call `plan_inspect` with `target_id: "local-dev"` and that `plan_id`. Review
   the selected inputs, resources the executor will reserve and the action and
   cleanup budgets. `artifact_read` retrieves the returned input and snapshot
   references.
3. Call `plan_execute` with `target_id`, `plan_id` and a new `request_id` UUID.
   Poll its operation ID until it reaches a terminal state.
4. Call `dev_status` with `{"target_id": "local-dev"}`. A newly initialized
   instance reports `stopped` because it has no running generation. The retained
   initialization command result reports `initialized`. Repeating matching
   initialization preserves existing files and returns the CLI's `reused` result.

If a submission response is lost, repeat that submission with the same request
ID and arguments. Repeating execution of the same plan returns its existing
operation. It does not start another generation.

## 4. Start and verify the instance

Stop client traffic to the instance before startup or verification, and keep it
stopped until the operation finishes. The coordinator's reservations exclude
conflicting management actions. They do not block HTTP inference requests.

Prepare, inspect and execute `dev_up` using the same sequence, with
`parameters: {}`. Startup launches the engines, records attestations, profiles
the supported splits and starts the router. A successful operation leaves
`dev_status` reporting `launched`.

Then prepare, inspect and execute `dev_verify` with `parameters: {}`. Verification
runs full preflight across every eligible directed KV path, checks a routed
arithmetic request and router readiness, and retains the measurement evidence.
`dev_status` reports `ready` only after verification
succeeds with the current processes. A failed verification can leave the
services running with status `degraded`; inspect its retained command result
and artifacts before retrying.

Before verification probes, the adapter checks the owned router's reported
activity and returns `fleet_busy` if it observes active requests. A standby
router or a nonzero HA epoch returns `prerequisite_failed` because this adapter
does not coordinate HA leases. This check does not drain traffic or prevent a
client from sending a later request.

Each action needs a fresh plan because initialization, startup and verification
change the inputs or lifecycle state used by the next action. For an existing
instance, the runner checks the plan again under the dev instance lock before
the command changes state. For a new instance, initialization rechecks that
the directory is absent and refuses to overwrite a directory created by
another caller.

## 5. Stop the instance

Teardown stops the instance's recorded engine, attestation and router processes.
Prepare, inspect and execute `dev_down` with `parameters: {}`, then call
`dev_status`. Successful teardown reports `stopped`; logs, profiles and failed
runs remain available in the instance directory.

`dev_down` checks process ownership and the recorded Python environment. It
does not require a working model or GPU runtime to stop recorded services.
Repeated teardown follows the existing dev ownership rules and does not signal
an unrelated process whose PID replaced an earlier instance process.

## Interruption and recovery

Closing the client or restarting the MCP server leaves accepted operations
running in their detached worker. Reconnect with the same registry and use
`operation_list` or the saved operation ID to inspect them.

Use `operation_cancel` to request cancellation explicitly. During partial
startup, the runner stops recorded temporary helpers and their owned process
trees. Services retained by a completed startup remain for `dev_down`.
Cancellation keeps the initiating error, cleanup results and residual resource
identities. A cancellation receipt does not prove that cleanup has finished.

If a worker disappears, inspection reports `recovery_required` and schedules
bounded resource inspection in a detached worker. Poll again for its findings.
Unknown ownership or surviving temporary helpers keep the reservations held.
For a lost worker with recorded temporary helpers, request cancellation and
inspect the cleanup result. Once an execution operation reaches `failed` or
`cancelled`, prepare a fresh plan and use `operation_resume` with the parent
operation ID, new plan ID and a new request ID. Resumption creates a new attempt
and reruns the action. If preparation failed or was cancelled, submit
`plan_prepare` again with a new request ID.

If preparation or execution reports changed inputs, correct the named
prerequisite and prepare another plan. If startup reports occupied ports or an
active generation, inspect `dev_status` and the retained process records.
Stop an owned generation with `dev_down` before starting another one.

To run the same actions from the CLI, export the registry path in that terminal:

```bash
export NARWHAL_MANAGEMENT_REGISTRY="$PWD/runs/mcp-dev/registry.json"
narwhal dev status --instance runs/mcp-dev/instance --format json
```

Bound `init`, `up`, `verify` and `down` calls prepare and execute through the
same coordinator and wait for their command result. Explicit initialization
flags must match the registered recipe and settings. A CLI interrupt requests
cancellation. Commands run without the registry variable are outside this
shared-coordination guarantee; the existing dev instance lock still applies.

## Settings

The optional settings file uses `schema: narwhal.local-dev-settings` and
`schema_version: 1`. It accepts `init` and `budgets`; unknown fields fail
validation. A null `settings_path` uses the defaults below.

`init` accepts these existing CLI overrides. Each field defaults to `null`,
which leaves selection to the template or existing initialization behavior.

| Field | Accepted value |
| --- | --- |
| `model_path` | Absolute GGUF file path. |
| `model_dir` | Absolute tokenizer directory path. |
| `gpu_uuid` | NVIDIA UUID beginning with `GPU-`. |
| `fabric_interface` | Linux interface name, 1 to 64 letters, digits, underscores, periods or hyphens. |
| `engine_count` | Integer from 2 through 8. |
| `port_base` | Integer from 1 through 65535; all derived ports must also fit 1 through 65535 and be distinct. The upper usable value depends on `engine_count`. |
| `gpu_memory_utilization` | Finite fraction greater than 0 and at most 1. |
| `device_allowance` | Finite fraction greater than 0 and at most 1. |

`budgets` accepts `dev_init`, `dev_up`, `dev_verify` and `dev_down` objects.
Each object accepts the fields below. All values are integer milliseconds
from 1 through 86,400,000.

| Field | Default |
| --- | --- |
| `timeout_ms` | `300000` for init and verify; `3600000` for up; `60000` for down. |
| `term_grace_ms` | `10000` |
| `kill_grace_ms` | `5000` |
| `reconcile_ms` | `30000` |

The registered recipe remains `narwhal.dev-template` version 1. Its
`runtime.environment` accepts only `VLLM_SSM_CONV_STATE_LAYOUT` with value `DS`,
and `VLLM_USE_FLASHINFER_SAMPLER` and `VLLM_USE_V2_MODEL_RUNNER` with string
values `0` or `1`. Other runtime environment fields fail validation.

Use the pinned runtime installation from the prerequisites and retain the
registry, state directory, artifact root and instance directory for subsequent
inspection and recovery.
