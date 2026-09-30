# Narwhal fleet configuration and deployment reference

A router process serves one model from one engine fleet. To serve a second model, run a separate fleet and router and choose between them at ingress.

The fleet is a JSON document. It configures serving, profiling, validation, role control, recovery, and the compatibility checks run against engines. The deployment tooling reads it alongside the workstation's environment to generate private inputs for each host.

Start a new fleet from the annotated example:

```bash
.venv/bin/narwhal-check --print-example-config
```

`narwhal-serve`, `narwhal-profile`, and `narwhal-check` take the fleet path through `--fleet`. Python code that calls `create_app()` can set `NARWHAL_FLEET` instead.

Every fleet file declares its schema and version, and Narwhal checks both before it reads any other field:

```json
{
  "schema": "narwhal.fleet",
  "schema_version": 1
}
```

Version 1 leaves client identity and content capture to ingress. Inside Narwhal there is a single global admission budget, and requests are measured in tokens and durations. The schemas used by fleets, profiles, and runtime artifacts are listed under [interface versions](telemetry/05-Compatibility.md#check-interface-compatibility-before-deployment).

## Fleet and deployment contracts

- [Fleet schema and engine contract](configuration/01-Fleet-Schema.md)
- [Serving and role control](configuration/02-Serving-and-Role-Control.md)
- [Recovery, authentication, and profile validation](configuration/03-Recovery-and-Validation.md)
- [Deployment inputs and SSH trust](configuration/04-Deployment-Inputs.md)
- [Engine endpoints and launch records](configuration/05-Engine-Launch.md)
- [Fabric, CLI, and configuration operations](configuration/06-Fabric-and-Operations.md)
