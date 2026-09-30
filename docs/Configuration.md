# Narwhal fleet configuration and deployment reference

| Models  | Deployment                                                             |
| ------- | ---------------------------------------------------------------------- |
| One     | One router process and one engine fleet                                |
| Several | One router and one fleet per model, with the model selected at ingress |

Annotated example fleet file:

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

The declared schema and version must match a [supported interface version](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment).

Fleet schema version 1:

- Ingress: client identity and content capture
- Admission: one global budget
- Request measurement: token counts and durations

## Fleet and deployment contracts

<div class="grid cards" markdown>

-   [Fleet schema and engine contract](configuration/01-Fleet-Schema.md)

    ---

    Top-level keys, JSON types, paths, environment variables, minimal fleet definition, hardware block, `engine_contract` fields, and attestation.

-   [Serving and role control](configuration/02-Serving-and-Role-Control.md)

    ---

    Admission, queueing, retries, placement, deadlines, token counting, and role-controller settings.

-   [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md)

    ---

    Breaker and drift settings, restart policy, resume, engine credentials, protocol adapters, profile store path, and decode-fit error limits.

-   [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md)

    ---

    Generated deployment files, role environments, host inventory, and known-hosts handling.

-   [Engine endpoints and launch records](configuration/05-Engine-Launch.md)

    ---

    Engine URLs, launch-record fields, image verification, prefix caching, and KV cache events.

-   [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md)

    ---

    Fabric workload budgets, CLI precedence, request journal, provenance and publication, and deployment sequence.

</div>
