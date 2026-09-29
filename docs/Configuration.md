# Narwhal fleet configuration and deployment reference

Each router process serves one model from one engine fleet.

Multi-model serving uses one fleet and one router per model, selected at ingress.

A fleet JSON file configures:

- Serving
- Profiling
- Validation
- Role control
- Recovery
- Engine compatibility checks

Start a new fleet from the annotated example:

```bash
.venv/bin/narwhal-check --print-example-config
```

Fleet file path:

| Consumer                                            | Setting                              |
| --------------------------------------------------- | ------------------------------------ |
| `narwhal-serve`, `narwhal-profile`, `narwhal-check` | `--fleet`                            |
| `create_app()`                                      | `NARWHAL_FLEET` environment variable |

Fleet file header:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Narwhal validates the declared schema and version against the [supported interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment).

[Judge Comment: Operative facts were lost here. The original stated the header is required in every fleet file and that validation runs before any other fleet field is read.]

Fleet schema version 1:

- Ingress: client identity and content capture
- Admission: one global budget
- Request measurement: token counts and durations

## Fleet and deployment contracts

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md): top-level keys, JSON types, paths, `engine_contract` fields, attestation
- [Serving and role control](configuration/02-Serving-and-Role-Control.md): admission, queueing, retries, role-controller settings
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md): breaker and drift settings, restart policy, engine credentials, profile store path
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md): generated deployment files, role environments, host inventory, known-hosts handling
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md): engine URLs, launch-record fields, image verification, KV cache events
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md): fabric workload budgets, CLI precedence, request journal, deployment sequence