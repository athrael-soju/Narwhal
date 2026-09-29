# Narwhal fleet configuration and deployment reference

Each router process serves one model from one engine fleet. To serve several models, select the model at ingress and route each one through its own fleet and router.

A fleet JSON file controls serving, profiling, validation, role control, recovery, and engine compatibility checks.

Start a new fleet from the annotated example:

```bash
.venv/bin/narwhal-check --print-example-config
```

Pass the fleet file path to `narwhal-serve`, `narwhal-profile`, and `narwhal-check` through `--fleet`, and to `create_app()` through the `NARWHAL_FLEET` environment variable.

Declare the schema identity and version in every fleet file:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Narwhal validates the declared schema and version against the [supported interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment) before it reads any other fleet field.

In fleet schema version 1, ingress handles client identity and content capture. Narwhal applies one global admission budget and measures requests in token counts and durations.

## Fleet and deployment contracts

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md): top-level keys, JSON types, paths, environment variables, minimal fleet definition, hardware block, `engine_contract` fields, and attestation
- [Serving and role control](configuration/02-Serving-and-Role-Control.md): admission, queueing, retries, placement, deadlines, token counting, and role-controller settings
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md): breaker and drift settings, restart policy, resume, engine credentials, protocol adapters, profile store path, and decode-fit error limits
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md): generated deployment files, role environments, host inventory, and known-hosts handling
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md): engine URLs, launch-record fields, image verification, prefix caching, and KV cache events
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md): fabric workload budgets, CLI precedence, request journal, provenance and publication rules, and deployment sequence
