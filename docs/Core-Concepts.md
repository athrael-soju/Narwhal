# Core concepts

Narwhal manages a fleet of dual-capability inference engines serving one model. Its defining capability is role reallocation: an engine can move between logical prefill and decode pools without reloading model weights.

The scheduler changes how resident engine capacity is used. Engines remain loaded, KV transfer paths remain available, and existing requests retain their placements while new work follows the current role assignment.

## Concepts

- [Request flow and fleet topology](concepts/01-Request-and-Topology.md)
- [Role control and capacity floors](concepts/02-Role-Control.md)
- [Failure, readmission, and state](concepts/03-Failure-and-State.md)

## Related reference material

- [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution): producer ownership, local decode, descriptor validation, and timing boundaries.
- [Configuration](Configuration.md): placement policy, role guards, serving limits, and engine health.
- [Measure a fleet](Measure.md): performance profiles, transfer checks, deployment load, and occupied-role canaries for the pinned backend.
- [HTTP API](HTTP-API.md): state-document definitions.
- [Operate Narwhal](Operate.md): lifecycle and failover procedures.
- [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities): package ownership and import constraints.
