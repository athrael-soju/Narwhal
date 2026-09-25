# Core concepts

Narwhal assigns prefill and decode roles across dual-capability engines serving one model. A role move changes placement for new requests while model weights, KV paths, and resident requests stay on their engines.

## Concepts

- [Request flow and fleet topology](concepts/01-Request-and-Topology.md)
- [Role control and capacity floors](concepts/02-Role-Control.md)
- [Failure, readmission, and state](concepts/03-Failure-and-State.md)

## Related reference material

- [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution): producer ownership, local decode, descriptor validation, and timing boundaries.
- [Configuration](Configuration.md): placement policy, role guards, serving limits, and engine health.
- [Measure a fleet](Measure.md): performance profiles, transfer checks, deployment load, and acceptance evidence.
- [HTTP API](HTTP-API.md): state-document definitions.
- [Operate Narwhal](Operate.md): lifecycle and failover procedures.
- [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities): package ownership and import constraints.
