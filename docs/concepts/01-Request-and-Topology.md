# Request flow and fleet topology

## Engine contract

Every engine in a fleet meets the same contract:

- Speaks the configured inference-engine dialect.
- Produces and consumes compatible key-value (KV) cache.
- Sends KV to every peer eligible to receive it.
- Has measured prefill and decode performance profiles.
- Passes preflight validation before taking traffic.
- Passes readmission checks after a hold, drain, failure, or maintenance event.

The fleet's `engine_contract` lists the [compatibility fields](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) that every engine must match.

### KV transfer for vLLM engines

KV transfer across the configured ring or mesh is allowed for a vLLM engine with the effective `kv_both` role when all three requirements hold:

| Requirement | Provider |
| --- | --- |
| Attestation inputs captured from the live process | [Gate E](../deploy/05-Attest.md#capture-the-attestation-inputs) |
| The process bound to its image, NIXL connector, and runtime features | Attestation sidecar |
| The attested process validated | [`narwhal-check`](../cli/Check.md) |

## How a request executes

| Stage | Behavior |
| --- | --- |
| Admission | The request takes a seat under the [global admitted-request limit](../configuration/02-Serving-and-Role-Control.md#41-global-admission). |
| Pricing | Each eligible prefill engine is priced from the prompt token count, its measured performance curves, and its resident work. |
| Predictive check | With the default `serving.admission` of `predictive`, a projected time to first token (TTFT) above the TTFT budget on the cheapest available prefill path rejects the request. |
| Prefill | The chosen engine holds the prompt KV as the producer and returns a typed KV handoff. |
| Decode | An eligible engine consumes the handoff and runs decode. |
| Streaming | Tokens stream to the client. |
| Journal | The request journal records admission, placement, retries, transfers, timing, and the final outcome. |

When every seat is occupied, `serving.queue_capacity` sets the outcome:

| `serving.queue_capacity` | Outcome |
| --- | --- |
| Positive, queue has space | The request waits in a bounded FIFO queue under its original deadline. |
| Positive, queue full | Retryable refusal. |
| `0` (the default) | Retryable refusal. |

A [retry](../configuration/02-Serving-and-Role-Control.md#42-waiting-phase-concurrency-and-retries) reruns prefill and decode with a fresh KV handoff.

## Fleet topology

| Topology | How roles are assigned | In practice |
| --- | --- | --- |
| Aggregated serving | Every engine does both prefill and decode, with local KV | Long prefills share a scheduler with decode batches |
| Static disaggregation | Fixed prefill pool and fixed decode pool | An operator changes pool membership by hand |
| Adaptive cold-swap | Engines change pools by draining and relaunching | Capacity arrives after restart, weight load, and validation |
| Adaptive hot-swap | Dual-capability engines form logical prefill and decode pools | Weights stay loaded |

### Aggregated serving

![Four identical replicas, each serving prefill and decode.](../assets/architectures/aggregated.svg)

### Static disaggregation

![Two fixed prefill engines and two fixed decode engines.](../assets/architectures/static.svg)

### Adaptive cold-swap

![One engine draining and restarting in the decode pool.](../assets/architectures/coldswap.svg)

A cold-swap move runs these steps:

1. Drain the engine.
2. Relaunch it in the new role.
3. Reload its weights.
4. Register its transfer peers.
5. Pass health validation.

Use cold-swap for traffic shifts longer than this sequence.

### Adaptive hot-swap

![One engine changing role while its weights remain resident.](../assets/architectures/hotswap.svg)

Hot-swap changes the scheduler role of an eligible dual-capability engine in place.

[Role-change guards](02-Role-Control.md#guards-on-role-changes) limit role changes:

- a cooldown
- a minimum dwell time
- confirmation rules
- minimum role floors
- a check on resident work
- exclusion of unhealthy engines
- exclusion of engines in a lifecycle event
