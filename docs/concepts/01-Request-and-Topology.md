# Request flow and fleet topology

## Engine contract

Every engine in a fleet meets the same contract:

- It speaks the configured inference-engine dialect.
- It produces and consumes compatible key-value (KV) cache.
- It sends KV to any peer eligible to receive it.
- It has measured prefill and decode performance profiles.
- It passes preflight validation before it takes traffic.
- It passes readmission checks again after any hold, drain, failure, or maintenance event.

The fleet's `engine_contract` lists the [compatibility fields](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) that every engine must match.

### KV transfer for vLLM engines

An engine with the effective `kv_both` role produces and consumes KV. Narwhal allows KV transfer for a vLLM engine with that role across the configured ring or mesh when all three requirements hold:

| Requirement                                                          | Provider                                                        |
| -------------------------------------------------------------------- | --------------------------------------------------------------- |
| Attestation inputs captured from the live process                    | [Gate E](../deploy/05-Attest.md#capture-the-attestation-inputs) |
| The process bound to its image, NIXL connector, and runtime features | Attestation sidecar                                             |
| The attested process validated                                       | [`narwhal-check`](../cli/Check.md)                              |

## How a request executes

| Stage | Behavior |
| --- | --- |
| Admission | The router gives the request a seat under the [global admitted-request limit](../configuration/02-Serving-and-Role-Control.md#41-global-admission). |
| Pricing | Narwhal counts the prompt tokens and prices each eligible prefill engine from its measured performance curves and resident work. |
| Predictive check | With `serving.admission` set to `predictive` (the default), Narwhal projects time to first token (TTFT) on the cheapest available prefill path. A projection above the TTFT budget rejects the request before dispatch. |
| Prefill | The chosen engine processes the prompt, holds the resulting KV as the producer, and returns a typed KV handoff. |
| Decode | The same engine, or another eligible engine that consumes the handoff, runs decode. |
| Streaming | Narwhal streams tokens to the client and tracks token timing and resident work. |
| Journal | The request journal records admission, placement, retries, transfers, timing, and the final outcome. |

When every seat is occupied, `serving.queue_capacity` sets the outcome:

| `serving.queue_capacity` | Outcome |
| --- | --- |
| Positive, queue has space | The request waits in a bounded FIFO queue and keeps its original deadline. |
| Positive, queue full | Narwhal returns a retryable refusal. |
| `0` (the default) | Narwhal returns a retryable refusal. |

A [retry](../configuration/02-Serving-and-Role-Control.md#42-waiting-phase-concurrency-and-retries) reruns prefill and decode from scratch with a fresh KV handoff. Narwhal discards the KV from the failed attempt.

## Fleet topology

The four topologies differ in how roles are assigned and in the cost of a role change.

| Topology              | How roles are assigned                                        | In practice                                                          |
| --------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------- |
| Aggregated serving    | Every engine does both prefill and decode, with local KV      | Long prefills share a scheduler with decode batches                  |
| Static disaggregation | Fixed prefill pool and fixed decode pool                      | An operator changes pool membership by hand                          |
| Adaptive cold-swap    | Engines change pools by draining and relaunching              | New capacity arrives after restart, weight load, and validation      |
| Adaptive hot-swap     | Dual-capability engines form logical prefill and decode pools | The role label changes and the weights stay loaded                   |

### Aggregated serving

![Four identical replicas, each serving prefill and decode.](../assets/architectures/aggregated.svg)

Aggregated serving is the baseline. Every replica handles the whole request. A long prefill occupies the same scheduler as decode batches.

### Static disaggregation

![Two fixed prefill engines and two fixed decode engines.](../assets/architectures/static.svg)

Prompts run on the prefill pool, and their KV handoffs move to the decode pool. The pool ratio is set when the fleet is sized for the expected workload. A workload shift that needs a different ratio, such as a move toward longer prompts, requires an operator to move engines between pools by hand.

### Adaptive cold-swap

![One engine draining and restarting in the decode pool.](../assets/architectures/coldswap.svg)

A cold-swap move completes these steps before the engine adds capacity in its new pool:

1. Drain the engine.
2. Relaunch it in the new role.
3. Reload its weights.
4. Register its transfer peers.
5. Pass health validation.

Cold-swap suits traffic shifts that last longer than this sequence.

### Adaptive hot-swap

![One engine changing role while its weights remain resident.](../assets/architectures/hotswap.svg)

Hot-swap changes the scheduler role of an eligible dual-capability engine in place. The engine keeps running, with its weights loaded and its KV connections to eligible peers intact.

[Role-change guards](02-Role-Control.md#guards-on-role-changes) limit role changes:

- a cooldown;
- a minimum dwell time;
- confirmation rules;
- minimum role floors;
- a check on resident work;
- exclusion of unhealthy engines and engines in a lifecycle event.
