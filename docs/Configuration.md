# Narwhal fleet configuration and deployment reference

Each router process serves one model from one engine fleet. Route a second model through a separate fleet and router, with model selection at ingress.

A fleet JSON controls serving, profiling, validation, role control, recovery, and engine compatibility checks. Deployment tooling combines the fleet with workstation environment data to derive private host inputs.

Use the annotated example as the starting point for a new fleet:

```bash
.venv/bin/narwhal-check --print-example-config
```

Pass the fleet path to `narwhal-serve`, `narwhal-profile`, and `narwhal-check` through `--fleet`. Python callers of `create_app()` can select the file with `NARWHAL_FLEET`.

Fleet files declare their schema identity and version:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Narwhal validates the declared schema and version before reading fleet fields.

In version 1, ingress handles client identity and content capture. Narwhal applies one global admission budget and measures requests in token counts and durations.

Check [interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment) for the schemas used by the fleet, profiles, and runtime artifacts.

## Fleet and deployment contracts

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md)
- [Serving and role control](configuration/02-Serving-and-Role-Control.md)
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md)
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md)
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md)
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md)
