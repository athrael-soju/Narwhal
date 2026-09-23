# Narwhal fleet configuration and deployment reference

Narwhal serves one model from one engine fleet per router process. A second model requires a separate fleet and router; select between models at ingress.

A fleet is described by a JSON document. The same document drives serving, profiling, validation, role control, recovery, and engine compatibility checks. Deployment tooling derives private host-specific inputs from that fleet plus workstation environment data.

Use the annotated example as the starting point for a new fleet:

```bash
.venv/bin/narwhal-check --print-example-config
```

`narwhal-serve`, `narwhal-profile`, and `narwhal-check` require the fleet path through `--fleet`. Python callers of `create_app()` may instead select the file with `NARWHAL_FLEET`.

The fleet document declares:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Version 1 keeps client identity and content capture outside Narwhal, at ingress. Narwhal owns one global admission budget and measures requests in token counts and durations. Schema validation runs before fleet fields are read.

The contract-version reference in [Telemetry and Artifacts](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment) defines the complete public interface set.

## Configuration sections

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md)
- [Serving and role control](configuration/02-Serving-and-Role-Control.md)
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md)
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md)
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md)
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md)
