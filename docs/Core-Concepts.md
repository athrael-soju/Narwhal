# Core concepts

Every engine in a Narwhal fleet serves the same model, and every engine can perform prefill or decode. Narwhal assigns each engine a role and changes it at runtime.

A role change redirects new requests. Model weights stay loaded, KV paths stay in place, and requests already running on the engine finish where they started.

## Concepts

- [Request flow and fleet topology](concepts/01-Request-and-Topology.md)
- [Role control and capacity floors](concepts/02-Role-Control.md)
- [Failure, readmission, and state](concepts/03-Failure-and-State.md)

## Related reference

- [Disaggregated backend execution](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution): KV handoff ownership, same-worker decode, descriptor validation, timing boundaries.
- [Configuration](Configuration.md): placement policy, role guards, serving limits, engine health.
- [Measure a fleet](Measure.md): performance profiles, transfer checks, deployment load, acceptance evidence.
- [HTTP API](HTTP-API.md): state documents.
- [Operate Narwhal](Operate.md): lifecycle and failover procedures.
- [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities): which package owns what, and import rules between packages.
