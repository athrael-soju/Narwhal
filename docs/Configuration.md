# Fleet configuration

The fleet file is JSON. It configures serving, profiling, validation, role control, recovery, and the compatibility checks run against engines. `narwhal-serve`, `narwhal-profile`, and `narwhal-check` read it. The deployment tooling combines it with the workstation environment to generate private inputs for each host.

Print an annotated example config:

```bash
.venv/bin/narwhal-check --print-example-config
```

`narwhal-serve`, `narwhal-profile`, and `narwhal-check` take the fleet path through `--fleet`. Python code that calls `create_app()` can set `NARWHAL_FLEET` instead.

Every fleet file starts with a schema name and version. Narwhal checks both before reading other fields:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

The schemas used by fleets, profiles, and runtime artifacts are listed under [interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment).

## Limits

- A router process serves one model from one engine fleet. To serve a second model, run a separate fleet and router and choose between them at ingress.
- Schema version 1 leaves client identity and content capture to ingress. The fleet file does not configure them.
- Admission uses a single global budget. Requests are measured in tokens and durations.

## Reference pages

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md)
- [Serving and role control](configuration/02-Serving-and-Role-Control.md)
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md)
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md)
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md)
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md)
