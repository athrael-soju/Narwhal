# Request flow and fleet topology

## Engine contract

A fleet only works if its engines are interchangeable, so each one has to meet the same contract. It speaks the configured inference-engine dialect, produces and consumes compatible key-value (KV) cache, and can send KV to any peer eligible to receive it. It also needs measured prefill and decode performance profiles, because the router prices work from them. Before an engine takes traffic it passes preflight validation, and after any hold, drain, failure, or maintenance event it has to pass readmission checks again.

The fleet's `engine_contract` lists the [compatibility fields](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) that every engine must match.

### KV transfer for vLLM engines

An engine with the effective `kv_both` role can both produce and consume KV. For vLLM engines with that role, Narwhal only allows transfer across the configured ring or mesh once three things are in place:

| Requirement                                                          | Provider                                                        |
| -------------------------------------------------------------------- | --------------------------------------------------------------- |
| Attestation inputs captured from the live process                    | [Gate E](../deploy/05-Attest.md#capture-the-attestation-inputs) |
| The process bound to its image, NIXL connector, and runtime features | Attestation sidecar                                             |
| The attested process validated                                       | [`narwhal-check`](../cli/Check.md)                              |

## How a request executes

1. **Admission.** The router gives the request a seat under the [global admitted-request limit](../configuration/02-Serving-and-Role-Control.md#41-global-admission). If every seat is taken, `serving.queue_capacity` decides what happens. A positive value puts the request in a bounded FIFO queue, where it keeps its original deadline. If the queue is full, or the capacity is `0` (the default), Narwhal returns a retryable refusal.
2. **Pricing.** Narwhal counts the prompt tokens, then prices each eligible prefill engine using its measured performance curves and the work already resident on it.
3. **Predictive check.** With `serving.admission` set to `predictive` (the default), Narwhal projects time to first token (TTFT) on the cheapest available prefill path. If the projection exceeds the TTFT budget, the request is rejected before it is dispatched.
4. **Prefill.** The chosen engine processes the prompt, holds the resulting KV as the producer, and returns a typed KV handoff.
5. **Decode.** Decode runs either on the same engine or on another eligible engine that consumes the handoff.
6. **Streaming.** Narwhal streams tokens to the client while tracking token timing and resident work.
7. **Journal.** The request journal records admission, placement, retries, transfers, timing, and the final outcome.

A [retry](../configuration/02-Serving-and-Role-Control.md#42-waiting-phase-concurrency-and-retries) reruns prefill and decode from scratch and gets a fresh KV handoff, so any KV from the failed attempt is discarded.

## Fleet topology

The four topologies differ in two ways: who decides which engines do prefill and which do decode, and how expensive it is to change that decision.

| Topology              | How roles are assigned                                        | In practice                                                          |
| --------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------- |
| Aggregated serving    | Every engine does both prefill and decode, with local KV      | Simple, but long prefills share a scheduler with decode batches      |
| Static disaggregation | Fixed prefill pool and fixed decode pool                      | Changing pool membership is a manual job                             |
| Adaptive cold-swap    | Engines change pools by draining and relaunching              | New capacity arrives only after restart, weight load, and validation |
| Adaptive hot-swap     | Dual-capability engines form logical prefill and decode pools | Only the role label changes; weights stay loaded                     |

### Aggregated serving

![Four identical replicas, each serving prefill and decode.](../assets/architectures/aggregated.svg)

This is the baseline. Every replica handles the whole request, so there is nothing to balance between pools. The cost is that a long prefill occupies the same scheduler as decode batches.

### Static disaggregation

![Two fixed prefill engines and two fixed decode engines.](../assets/architectures/static.svg)

Prompts run on the prefill pool, and their KV handoffs move to the decode pool. You choose the pool ratio when you size the fleet, based on the workload you expect. If the mix changes enough to need a different ratio, for example a shift toward longer prompts, an operator has to move engines between pools by hand.
### Adaptive cold-swap

![One engine draining and restarting in the decode pool.](../assets/architectures/coldswap.svg)

Moving an engine to the other pool means draining it, relaunching it in the new role, and reloading its weights. Then its transfer peers have to register and it has to pass health validation before it takes traffic. Until that finishes the engine adds no capacity, so cold-swap makes sense only for traffic shifts that will outlast it.
### Adaptive hot-swap

![One engine changing role while its weights remain resident.](../assets/architectures/hotswap.svg)

Hot-swap changes the scheduler role of an eligible dual-capability engine without restarting it. The engine keeps its weights loaded and its KV connections to eligible peers.

Role changes are limited by [role-change guards](https://claude.ai/chat/02-Role-Control.md#guards-on-role-changes): a cooldown, a minimum dwell time, confirmation rules, minimum role floors, a check on resident work, and exclusion of engines that are unhealthy or in a lifecycle event.