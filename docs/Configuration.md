# Narwhal fleet configuration and deployment reference

Each router process serves one model from one engine fleet. Multi-model serving uses one fleet and one router per model, with the model selected at ingress.

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

Every fleet file declares its schema identity and version:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Narwhal validates the declared schema and version against the [supported interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment) before it reads any other fleet field.

Fleet schema version 1:

- Ingress: client identity and content capture
- Admission: one global budget
- Request measurement: token counts and durations

## Fleet and deployment contracts

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md): top-level keys, JSON types, paths, environment variables, minimal fleet definition, hardware block, `engine_contract` fields, and attestation
- [Serving and role control](configuration/02-Serving-and-Role-Control.md): admission, queueing, retries, placement, deadlines, token counting, and role-controller settings
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md): breaker and drift settings, restart policy, resume, engine credentials, protocol adapters, profile store path, and decode-fit error limits
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md): generated deployment files, role environments, host inventory, and known-hosts handling
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md): engine URLs, launch-record fields, image verification, prefix caching, and KV cache events
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md): fabric workload budgets, CLI precedence, request journal, provenance and publication, and deployment sequence
