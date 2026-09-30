# Core concepts

Every engine in a Narwhal fleet serves the same model, and every engine can do either prefill or decode. Narwhal decides which role each one plays at any given moment.

When Narwhal moves an engine to a different role, the only thing that changes is where new requests go. The model weights stay loaded, the KV paths stay in place, and requests already running on that engine finish where they started.

## Concepts

- [Request flow and fleet topology](concepts/01-Request-and-Topology.md)
- [Role control and capacity floors](concepts/02-Role-Control.md)
- [Failure, readmission, and state](concepts/03-Failure-and-State.md)

## Related reference

- [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution) explains who owns the KV handoff, how same-worker decode works, how descriptors are validated, and where the timing boundaries fall.
- [Configuration](Configuration.md) covers placement policy, role guards, serving limits, and engine health.
- [Measure a fleet](Measure.md) shows how to build performance profiles, check transfers, apply deployment load, and collect acceptance evidence.
- [HTTP API](HTTP-API.md) defines the state documents.
- [Operate Narwhal](Operate.md) walks through lifecycle and failover procedures.
- [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities) describes which package owns what, and the rules for importing between them.
