# Request flow and fleet topology

## Runtime contract

Every engine must:

- expose the configured inference-engine API (for example, the vLLM API);
- produce and consume KV cache that is compatible with its peers;
- transfer KV to every eligible peer;
- provide measured prefill and decode performance profiles;
- pass preflight validation before entering service;
- pass lifecycle checks again before returning to service after a hold, drain, failure, or maintenance event.

For vLLM engines with effective [`kv_both` behavior](../deploy/05-Attest.md#capture-attestation-inputs), an attestation sidecar binds the running process to its image, NIXL connector, and required runtime features. `narwhal-check` validates that process before Narwhal permits KV transfer across the configured ring or mesh. If validation fails, Narwhal does not allow KV transfer to or from that engine.

## How a request executes

1. Admission. The router receives the request and assigns it a slot under its [global admitted-request limit](../configuration/02-Serving-and-Role-Control.md#41-global-admission). When all slots are occupied, the request enters a bounded FIFO and keeps its original deadline. Once the FIFO reaches `serving.queue_capacity`, Narwhal returns HTTP 429. The default capacity is `0`, so a saturated router refuses new requests immediately. `serving.queue_timeout_s` limits time spent in the FIFO.
2. Prefill pricing. Narwhal counts prompt tokens and evaluates eligible prefill engines using their measured performance curves and current resident work.
3. Predictive admission. Under the default `serving.admission = "predictive"`, Narwhal rejects the request with HTTP 429 if even the cheapest prefill path is projected to exceed the request's time-to-first-token (TTFT) budget.
4. Prefill. The selected engine processes the prompt and returns a KV handoff. The prefill engine owns the handoff until decode consumes it.
5. Decode placement. Narwhal selects a decode engine. Decode runs on the prefill engine or consumes the KV handoff on another eligible engine.
6. Streaming. Decode tokens stream to the client while Narwhal tracks token timing and resident work.
7. Journal. The request journal records admission, placement, retries, transfer activity, timing, and final outcome.

Every retry obtains fresh KV ownership.

## Fleet topology

| Topology             | Role assignment                                               | Trade-off                                                                                     |
| -------------------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Aggregated           | Every engine executes prefill and decode with local KV        | Long prefills occupy the same scheduler as decode batches                                     |
| Static disaggregated | Separate fixed prefill and decode pools                       | Operator must manually change pool membership                                                 |
| Adaptive cold-swap   | Engines move between pools by draining and relaunching        | Restart, weight load, peer registration, and validation delay the new capacity                |
| Adaptive hot-swap    | Dual-capability engines form logical prefill and decode pools | Scheduler changes the role label while weights remain resident                                |

### Aggregated serving

![Four identical replicas, each serving prefill and decode.](../assets/architectures/aggregated.svg)

### Static disaggregation

![Two fixed prefill engines and two fixed decode engines.](../assets/architectures/static.svg)

Prompts run on the prefill pool, and their KV handoffs move to the decode pool.

The pool ratio is fixed at sizing time. When the request mix shifts, an operator reallocates engines.

### Adaptive cold-swap

![One engine draining and restarting in the decode pool.](../assets/architectures/coldswap.svg)

A cold-swap runs these steps in order:

1. Drain the engine.
2. Relaunch it in the target role.
3. Reload weights.
4. Register transfer peers.
5. Complete health validation.
6. Return the engine to service.

The new capacity serves no traffic until these steps finish, so a cold-swap pays off only when the traffic shift outlasts them.

### Adaptive hot-swap

![One engine changing role while its weights remain resident.](../assets/architectures/hotswap.svg)

Hot-swap reassigns the scheduler role of an eligible dual-capability engine while model weights stay resident and KV paths continue connecting eligible peers.

Role changes are limited by:

- cooldown
- dwell time
- confirmation rules
- minimum role floors
- resident-work guards
- health and lifecycle exclusions

See [Serving and role control](../configuration/02-Serving-and-Role-Control.md) for how each is configured.
