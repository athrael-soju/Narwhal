# Core concepts

Narwhal assigns prefill and decode roles across dual-capability engines that serve one model.

A role move:

- changes placement for new requests;
- keeps model weights, KV paths, and resident requests on their engines.

## Concept pages

- [Request flow and fleet topology](concepts/01-Request-and-Topology.md): request admission, prefill, decode placement, the journal, and fleet topologies.
- [Role control and capacity floors](concepts/02-Role-Control.md): role-move pricing, guards, and floor repair.
- [Failure, readmission, and state](concepts/03-Failure-and-State.md): monitoring stages, failure classes, readmission checks, and the state handoff.

## Terms

| Term | Meaning | Reference |
| --- | --- | --- |
| Time to first token (TTFT) | Router-measured time from request arrival to prefill completion, with its target in `slo.ttft_s`. | [Request timing](http-api/03-Backend-and-Failures.md#request-scope-and-timing) |
| Time per output token (TPOT) | Router-measured average interval between output tokens after prefill, with its target in `slo.tpot_s`. | [Load definitions](configuration/02-Serving-and-Role-Control.md#71-load-definitions) |
| Service-level objective (SLO) | The TTFT and TPOT targets in the `slo` object. | [SLO attainment](http-api/06-SLO-and-Demand.md#slo-attainment) |
| Dual-capability engine | An engine that runs both prefill and decode. | [Adaptive hot-swap](concepts/01-Request-and-Topology.md#adaptive-hot-swap) |
| Adjacent split | A prefill/decode split one engine move away from the current split. | [Role control](concepts/02-Role-Control.md#role-control) |
| KV handoff | The backend's key-value (KV) descriptor that a prefill engine returns and a decode engine consumes. | [Disaggregated backend execution](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution) |
| State handoff | The active router's versioned high-availability (HA) state document, served at `/narwhal/handoff` and read by standby routers and `--resume`. | [State handoff](http-api/07-Handoff-and-Lifecycle.md#state-handoff) |
| Attestation sidecar | The `narwhal-attest` process that serves an engine's attestation, returning HTTP 503 after the engine process changes. | [`narwhal-attest`](cli/Attest.md) |
| Sample sidecar | The `.samples.json` file that `narwhal-profile` writes beside the profile store, holding raw observations and the process evidence for each fit. | [Retain profile samples and fits](measure/01-Profile.md#3-retain-profile-samples-and-fits) |
| Process generation | One incarnation of an engine process, identified in each profile by the vLLM version and process start time, or by the verified attestation digest with `engine_contract`. | [Validate the engine cost model](telemetry/02-Profiles.md#validate-the-engine-cost-model) |
| Whole-wave | Scope of the `whole_wave` restart policy, which drains, restarts, and readmits every configured engine as one wave. | [Restart an engine wave](operate/03-Restart-Engines.md#8-restart-an-engine-wave) |
| Lease domain | Shared storage with POSIX `flock`, coherent reads, and atomic rename that gives both routers of a pair one lease. | [Start a router pair](operate/01-Start-Routers.md#4-start-a-router-pair) |
| Deployment set | The Narwhal release, fleet configuration, profile store, deployment evidence, and configured first-token calibration artifact that a router pair runs under one release identifier. | [Keep one deployment set](operate/01-Start-Routers.md#2-keep-one-deployment-set) |
| Engines eligible for placement | Engines left after ejection, drain, and quarantine exclusions, counted as `available_instances` in `/health` and as the live count for role floors. | [Pool and SLO fields](http-api/05-Live-State.md#pool-and-slo-fields) |

## Related reference material

- [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution): KV producers, local decode, descriptor validation, and timing boundaries.
- [Configuration](Configuration.md): placement policy, role guards, serving limits, and engine health.
- [Measure a fleet](Measure.md): performance profiles, transfer checks, deployment load, and acceptance evidence.
- [HTTP API](HTTP-API.md): state-document definitions.
- [Operate Narwhal](Operate.md): lifecycle and failover procedures.
- [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities): package responsibilities and import constraints.
