---
description: How Narwhal moves dual-capability engines between prefill and decode roles for disaggregated LLM inference.
---

# Core concepts

Narwhal assigns prefill and decode roles across dual-capability engines that serve one model.

A role move:

- Placement changes for new requests.
- Model weights, KV paths, and resident requests stay on their engines.

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

    Monitoring and readiness, engine failure handling, readmission and drains, serving saturation and retries, and durable control-plane state.

</div>

## Terms

| Term | Meaning | Reference |
| --- | --- | --- |
| Time to first token (TTFT) | Router-measured time from request arrival to prefill completion, targeted by `slo.ttft_s`. | [![Request timing documentation](https://img.shields.io/badge/docs-Request%20timing-0f766e)](http-api/03-Backend-and-Failures.md#request-scope-and-timing) |
| Time per output token (TPOT) | Router-measured average interval between output tokens after prefill, targeted by `slo.tpot_s`. | [![Load definitions documentation](https://img.shields.io/badge/docs-Load%20definitions-0f766e)](configuration/02-Serving-and-Role-Control.md#71-load-definitions) |
| Service-level objective (SLO) | The TTFT and TPOT targets in the `slo` object. | [![SLO attainment documentation](https://img.shields.io/badge/docs-SLO%20attainment-0f766e)](http-api/06-SLO-and-Demand.md#slo-attainment) |
| Dual-capability engine | An engine that runs both prefill and decode. | [![Adaptive hot-swap documentation](https://img.shields.io/badge/docs-Adaptive%20hot--swap-0f766e)](concepts/01-Request-and-Topology.md#adaptive-hot-swap) |
| Adjacent split | A prefill/decode split one engine move away from the current split. | [![Role control documentation](https://img.shields.io/badge/docs-Role%20control-0f766e)](concepts/02-Role-Control.md#role-control) |
| KV handoff | The backend's key-value (KV) descriptor that a prefill engine returns and a decode engine consumes. | [![Disaggregated backend execution documentation](https://img.shields.io/badge/docs-Disaggregated%20backend%20execution-0f766e)](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution) |
| State handoff | The active router's versioned high-availability (HA) state document at `/narwhal/handoff`, read by standby routers and `--resume`. | [![State handoff documentation](https://img.shields.io/badge/docs-State%20handoff-0f766e)](http-api/07-Handoff-and-Lifecycle.md#state-handoff) |
| Attestation sidecar | The `narwhal-attest` process that returns HTTP 503 after the engine process changes. | [![narwhal-attest documentation](https://img.shields.io/badge/docs-narwhal--attest-0f766e)](cli/Attest.md) |
| Sample sidecar | The `.samples.json` file beside the profile store, holding raw observations and process evidence for each fit. | [![Retain profile samples and fits documentation](https://img.shields.io/badge/docs-Retain%20profile%20samples%20and%20fits-0f766e)](measure/01-Profile.md#3-retain-profile-samples-and-fits) |
| Process generation | The engine identity a profile binds to: with `engine_contract`, the attested launch digest when the sidecar reports launch evidence and the verified attestation digest otherwise; for other fleets, the vLLM version and process start time. | [![Validate the engine cost model documentation](https://img.shields.io/badge/docs-Validate%20the%20engine%20cost%20model-0f766e)](telemetry/02-Profiles.md#validate-the-engine-cost-model) |
| Whole-wave | Scope of the `whole_wave` restart policy, covering every configured engine as one wave. | [![Restart an engine wave documentation](https://img.shields.io/badge/docs-Restart%20an%20engine%20wave-0f766e)](operate/03-Restart-Engines.md#8-restart-an-engine-wave) |
| Lease domain | Shared storage with POSIX `flock`, coherent reads, and atomic rename, giving both routers of a pair one lease. | [![Start a router pair documentation](https://img.shields.io/badge/docs-Start%20a%20router%20pair-0f766e)](operate/01-Start-Routers.md#4-start-a-router-pair) |
| Deployment set | The Narwhal release, fleet configuration, profile store, deployment evidence, and configured first-token calibration artifact sharing one release identifier. | [![Keep one deployment set documentation](https://img.shields.io/badge/docs-Keep%20one%20deployment%20set-0f766e)](operate/01-Start-Routers.md#2-keep-one-deployment-set) |
| Engines eligible for placement | Engines left after ejection, drain, and quarantine exclusions, reported as `available_instances` in `/health` and used as the live count for role floors. | [![Pool and SLO fields documentation](https://img.shields.io/badge/docs-Pool%20and%20SLO%20fields-0f766e)](http-api/05-Live-State.md#pool-and-slo-fields) |

## Related reference material

<div class="grid cards" markdown>

-   [Backend continuation contract](http-api/03-Backend-and-Failures.md#disaggregated-backend-execution)

    ---

    Prefill and decode legs, KV handoff, and backend failures.

-   [Configuration](Configuration.md)

    ---

    Fleet fields, environment inputs, and their defaults.

-   [Measure a fleet](Measure.md)

    ---

    Profiling, SLO calibration, and target-workload measurement.

-   [HTTP API](HTTP-API.md)

    ---

    Completion, inspection, and lifecycle endpoints.

-   [Operate Narwhal](Operate.md)

    ---

    Ingress, routers, engines, and software upgrades.

-   [Source responsibilities](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md#source-responsibilities)

    ---

    Source packages and the areas each one implements.

</div>
