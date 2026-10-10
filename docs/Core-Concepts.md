---
description: How Narwhal moves dual-capability engines between prefill and decode roles for disaggregated LLM inference.
---

# Core concepts

Narwhal assigns prefill and decode roles across dual-capability engines that serve one model.

A role move changes placement for new requests. Model weights, KV paths, and resident requests stay on their engines.

## Concept pages

<div class="grid cards" markdown>

-   [Request flow and fleet topology](concepts/01-Request-and-Topology.md)

    ---

    Engine contract, request execution, and fleet topology.

-   [Role control and capacity floors](concepts/02-Role-Control.md)

    ---

    Role-controller evaluation, role floors, fallback, and degraded capacity.

-   [Failure, readmission, and state](concepts/03-Failure-and-State.md)

    ---

    Monitoring and readiness, engine failure handling, peer memory release, readmission and drains, serving saturation and retries, and durable control-plane state.

</div>

## Terms

| Term                           | Meaning                                                                                                                                                                                                                                                                                                                                                                    | Reference                                                                                                                                                                                              |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Time to first token (TTFT)     | Router-measured time from request arrival to prefill completion, targeted by `slo.ttft_s`.                                                                                                                                                                                                                                                                                 | [Request timing documentation](http-api/03-Backend-and-Failures.md#request-scope-and-timing)                                             |
| Time per output token (TPOT)   | Router-measured average interval between output tokens after prefill, targeted by `slo.tpot_s`.                                                                                                                                                                                                                                                                            | [Load definitions documentation](configuration/02-Serving-and-Role-Control.md#load-definitions)                                     |
| Service-level objective (SLO)  | The TTFT and TPOT targets in the `slo` object.                                                                                                                                                                                                                                                                                                                             | [SLO attainment documentation](http-api/06-SLO-and-Demand.md#slo-attainment)                                                             |
| Dual-capability engine         | An engine that runs both prefill and decode.                                                                                                                                                                                                                                                                                                                               | [Adaptive hot-swap documentation](concepts/01-Request-and-Topology.md#adaptive-hot-swap)                                             |
| Adjacent split                 | A prefill/decode split one engine move away from the current split.                                                                                                                                                                                                                                                                                                        | [Role control documentation](concepts/02-Role-Control.md#role-control)                                                                     |
| KV handoff                     | The backend's key-value (KV) descriptor that a prefill engine returns and a decode engine consumes.                                                                                                                                                                                                                                                                        | [Disaggregated backend execution documentation](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution)  |
| State handoff                  | The active router's versioned high-availability (HA) state document at `/narwhal/handoff`, read by standby routers and `--resume`.                                                                                                                                                                                                                                         | [State handoff documentation](http-api/07-Handoff-and-Lifecycle.md#state-handoff)                                                         |
| Attestation sidecar            | The `narwhal-attest` process that returns HTTP 503 after the engine process changes.                                                                                                                                                                                                                                                                                       | [narwhal-attest documentation](cli/Attest.md)                                                                                             |
| Sample sidecar                 | The `.samples.json` file beside the profile store, holding raw observations and process evidence for each fit.                                                                                                                                                                                                                                                             | [Retaining profile samples and fits documentation](measure/01-Profile.md#retaining-profile-samples-and-fits) |
| Process generation             | The engine identity that profiles and first-token calibration artifacts bind to. For a fleet with `engine_contract`, Narwhal derives the identity from the verified attestation's [`launch_digest` or `attestation_digest`](configuration/01-Fleet-Schema.md#attestation). For other fleets, Narwhal derives the identity from the vLLM version and process start time. | [Validating the engine cost model documentation](telemetry/02-Profiles.md#validating-the-engine-cost-model)      |
| Whole-wave                     | Scope of the `whole_wave` restart policy, covering every configured engine as one wave.                                                                                                                                                                                                                                                                                    | [Restarting an engine wave documentation](operate/03-Restart-Engines.md#restart-an-engine-wave)                      |
| Lease domain                   | Shared storage with POSIX `flock`, coherent reads, and atomic rename, giving both routers of a pair one lease.                                                                                                                                                                                                                                                             | [Starting a router pair documentation](operate/01-Start-Routers.md#starting-a-router-pair)                                 |
| Deployment set                 | The Narwhal release, fleet configuration, profile store, deployment evidence, and configured first-token calibration artifact sharing one release identifier.                                                                                                                                                                                                              | [Keeping one deployment set documentation](operate/01-Start-Routers.md#keeping-one-deployment-set)                     |
| Engines eligible for placement | Engines left after ejection, drain, and quarantine exclusions, reported as `available_instances` in `/health` and used as the live count for role floors.                                                                                                                                                                                                                  | [Pool and SLO fields documentation](http-api/05-Live-State.md#pool-and-slo-fields)                                              |

## Related reference material

<div class="grid cards" markdown>

-   [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution)

    ---

    Prefill and decode legs, KV handoff, and backend failures.

-   [Configuration](Configuration.md)

    ---

    Fleet fields, environment inputs, and their defaults.

-   [Measuring a fleet](Measure.md)

    ---

    Profiling, SLO calibration, and target-workload measurement.

-   [HTTP API](HTTP-API.md)

    ---

    Completion, inspection, and lifecycle endpoints.

-   [Operating Narwhal](Operate.md)

    ---

    Ingress, routers, engines, and software upgrades.

-   [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities)

    ---

    Source packages and the areas each one implements.

</div>
