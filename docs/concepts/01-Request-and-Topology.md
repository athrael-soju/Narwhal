# Request flow and fleet topology

## Runtime contract

Each engine implements the same runtime contract:

- expose the configured inference-engine dialect;
- produce and consume compatible KV cache;
- transfer KV to every eligible peer;
- provide measured prefill and decode performance profiles;
- pass preflight validation before entering service;
- pass lifecycle readmission checks after a hold, drain, failure, or maintenance event.

For vLLM engines with effective `kv_both` behaviour, an attestation sidecar binds the running process to its image, NIXL connector, and required runtime features. `narwhal-check` validates that process before Narwhal permits KV transfer across the configured ring or mesh.

## How a request executes

1. **Admission**
   The router receives the request and assigns one of the available serving seats.

   When all seats are occupied, the request enters a bounded FIFO and keeps its original deadline. Once the FIFO reaches its configured limit, Narwhal returns a retryable refusal.

2. **Prefill pricing**
   Narwhal counts prompt tokens and evaluates eligible prefill engines using their measured performance curves and current resident work.

3. **Predictive admission**
   Before dispatch, Narwhal can reject a request whose cheapest available prefill path projects TTFT beyond the request budget.

4. **Prefill**
   The selected engine processes the prompt and returns a typed KV handoff owned by the producer.

5. **Decode placement**
   Narwhal selects a decode engine. Decode may remain on the prefill engine or consume the KV handoff on another eligible engine.

6. **Streaming**
   Decode tokens stream to the client while Narwhal tracks token timing and resident work.

7. **Journal**
   The request journal records admission, placement, retries, transfer activity, timing, and final outcome.

Every retry obtains fresh KV ownership.

## Fleet topology

| Topology             | Role assignment                                               | Cost of changing the split                                                                    |
| -------------------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Aggregated           | Every engine executes prefill and decode with local KV        | Long prefills occupy the same scheduler as decode batches                                    |
| Static disaggregated | Separate fixed prefill and decode pools                       | Operator must manually change pool membership                                                 |
| Adaptive cold-swap   | Engines can move between pools by draining and relaunching    | Capacity is unavailable during drain, restart, weight load, peer registration, and validation |
| Adaptive hot-swap    | Dual-capability engines form logical prefill and decode pools | Scheduler changes the role label while weights remain resident                                |

### Aggregated serving

![Four identical replicas, each serving prefill and decode.](../assets/architectures/aggregated.svg)

### Static disaggregation

![Two fixed prefill engines and two fixed decode engines.](../assets/architectures/static.svg)

Prompts run on the prefill pool. Their KV handoffs then move to the decode pool.

The configured pool ratio reflects the workload used for sizing. When the request mix changes enough to require a different ratio, an operator must reallocate engines.

### Adaptive cold-swap

![One engine draining and restarting in the decode pool.](../assets/architectures/coldswap.svg)

A cold-swap:

1. drains the engine;
2. relaunches it in the target role;
3. reloads weights;
4. registers transfer peers;
5. completes health validation;
6. returns the engine to service.

Cold-swap pays the drain and restart interval before the new split serves traffic, so the traffic shift must persist long enough to use that capacity.

### Adaptive hot-swap

![One engine changing role while its weights remain resident.](../assets/architectures/hotswap.svg)

Hot-swap reassigns the scheduler role of an eligible dual-capability engine while model weights stay resident and KV paths continue connecting eligible peers.

Narwhal constrains these moves with:

- cooldown;
- dwell time;
- confirmation rules;
- minimum role floors;
- resident-work guards;
- health and lifecycle exclusions.
