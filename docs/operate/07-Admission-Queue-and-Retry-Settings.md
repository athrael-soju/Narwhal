---
description: Choose Narwhal's admission mode, queue, retry, quarantine, in-flight and decode-gap settings from the client outcomes each value produces.
---

# Choosing admission, queue and retry settings

This page covers the router settings that decide whether a request is admitted, how long it can wait, whether a failed attempt runs again, and which response the client receives when the router ends the request. For each setting, it gives the default, the client outcomes of each value, and when to change it.

[Serving and role control](../configuration/02-Serving-and-Role-Control.md) and [Recovery and validation](../configuration/03-Recovery-and-Validation.md) define every field, its accepted values and its validation. [Admission and refusal semantics](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) and [Engine failure handling](../http-api/03-Backend-and-Failures.md#engine-failure-handling) list every status and error type a client can receive.

## Defaults

| Setting | Default | Effect of the default |
| --- | --- | --- |
| `serving.admission` | `"predictive"` | The router refuses a request with HTTP 429 when its projected first token misses the TTFT budget |
| `serving.queue_capacity` | `0` | No queue: at the in-flight limit, new requests receive HTTP 429 |
| `serving.queue_timeout_s` | `0.0` | Unused while `serving.queue_capacity` is `0` |
| `serving.max_attempts` | `2` | One retry for a transient fault before visible output |
| `recovery.failure_quarantine_s` | `0.0` | No timed hold after a failed leg |
| `serving.max_connections` | `768` | In-flight limit and data connections per engine |
| `engine.decode_read_timeout_s` | `10.0` | A decode stream silent for 10 s fails |
| KV handoff bound | Derived per producer | From the producer's attested KV lease |
| Engine seats | Derived per engine | From the engine's attestation and profile |

The router reads these settings from the fleet file when it starts. A changed value takes effect after a router restart. Before you change a setting in production, compare its outcomes with the current value's, as [Checking a changed setting](#checking-a-changed-setting) describes.

## Admission mode

`serving.admission` selects how the router decides whether to admit an original request.

| Mode | The router admits a request when | Overload can reach clients as |
| --- | --- | --- |
| `predictive` | The request passes the router saturation checks, the in-flight limit, the TTFT check and the [decode admission check](../configuration/02-Serving-and-Role-Control.md#decode-admission-check) | HTTP 429 `server_overloaded_error` before placement, with journal terminal `refused` and the failed check as its `cause` |
| `open` | The request passes the router saturation checks and the in-flight limit | Late first tokens on HTTP 200 responses, engine faults as HTTP 502 or 504, and request deadline expiries as HTTP 504 `request_expired` |

The TTFT check prices the request's prefill on the cheapest live prefill engine, adds the time since arrival, and compares the result with the TTFT budget, `slo.ttft_s * (1 + serving.admission_margin)`. A `queue` refusal carries a `Retry-After` value from the price minus the budget, excluding the time the request has waited. A `prompt` refusal, for a prompt whose own prefill exceeds the budget, carries no `Retry-After`. A refusal from the decode admission check carries `Retry-After: 1`. Only an original request's first attempt is priced against the TTFT check. [Retry pricing](../configuration/02-Serving-and-Role-Control.md#retry-pricing) gives the check a retry passes.

In both modes, the router records the [admission price](../telemetry/01-Journal.md#admission-price) of each prefill placement in the journal. Open mode records the price without enforcing it. The price estimates the median TTFT, not an upper bound, so some admitted requests reach their first token after it.

### Changing the mode

- Keep `predictive` when clients should receive an immediate 429 with `Retry-After` instead of a first token later than the TTFT budget.
- Use `open` to measure the fleet without predictive refusals, such as an in-flight sweep. Open mode returns no `refused` outcomes, so overload appears as SLO misses and engine faults.
- Before you rely on predictive refusals, compare `price_s` with `ttft_s` on completed journal rows. When the median ratio of `ttft_s` to `price_s` is well above 1, predictive mode admits requests that miss the budget. When it is well below 1, predictive mode refuses requests the fleet would serve within the budget. Remeasure the [engine profiles](../measure/01-Profile.md) in either case.
- Raise `serving.admission_margin` to admit requests whose projected TTFT exceeds `slo.ttft_s` by up to that fraction of it. The margin must be zero or greater, so it only widens the budget.

## Queue

`serving.queue_capacity` sets how many requests can wait for an admission seat above the [in-flight limit](#in-flight-limit). `serving.queue_timeout_s` bounds the time one request waits for an admission seat and prefill seats combined. The defaults, `0` and `0.0`, turn the queue off. A positive `serving.queue_capacity` requires a positive `serving.queue_timeout_s`.

With the queue off:

- When the in-flight limit is reached, the router answers each new request with HTTP 429 `server_overloaded_error`, message `router in-flight limit reached`, and `Retry-After: 1`.
- The router places requests without checking [engine seats](#engine-seats), and an engine queues requests above its seats. The leg deadlines, `serving.prefill_timeout_s` and `engine.first_token_timeout_s`, and the original request deadline bound that engine-side wait.

With the queue on, a request can wait for an admission seat, a prefill seat and a decode seat. [Queue waits](../configuration/02-Serving-and-Role-Control.md#queue-waits) gives each wait's bound, the response when the bound ends it, the effect of event-loop lag, and the holds that end waiting requests.

In `predictive` mode, the router prices a first attempt each time its admission-seat or prefill-seat wait starts or wakes, and the price includes the time waited. A priced first attempt therefore leaves its wait with the TTFT check's HTTP 429, cause `queue`, before its wait exceeds the TTFT budget. A `serving.queue_timeout_s` longer than the TTFT budget still bounds the waits the router does not price: a retry's prefill-seat wait, and every wait in `open` mode.

The router does not price decode-seat waits in either mode. A decode-seat wait delays the client's first token after the request has passed admission, so under sustained overload queued requests can reach the client after the TTFT target, and goodput can fall below its queue-off value.

### Turning the queue on

Turn the queue on when both conditions hold:

- Overload arrives in short bursts, not as sustained load above capacity.
- Clients should wait for a seat instead of receiving an immediate 429 at the in-flight limit.

Set `serving.queue_timeout_s` to the longest time a client should wait before its request starts prefill. Raising it lengthens waits without adding seats.

In `predictive` mode, the router prices an admission-seat wait before it sizes the request, as a cold prompt of estimated length. With the queue on, a workload of long, mostly cached prompts can therefore receive `queue` or `prompt` refusals for requests the fleet would serve within the budget.

## Engine seats

The router derives each engine's prefill and decode seats from engine evidence. No fleet setting holds them. [Engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) gives the derivation and where placement and the decode admission check use seats.

To change an engine's decode seats, relaunch it with a different `--max-num-seqs` and attest it again, or remeasure its profile. Its prefill seats follow its profile and `slo.ttft_s`.

With the queue on, full seats in a phase produce the seat waits and responses in [Queue](#queue). In `predictive` mode, full decode seats also produce HTTP 429 refusals with cause `slot_wait` when the projected slot wait pushes the request past the TTFT budget.

## KV handoff bound

The router derives each producer's handoff bound from the KV lease in its attestation. [KV handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) gives the derivation. A producer whose attestation records no lease has no handoff bound: the original request deadline bounds its handoffs and its requests' decode-seat waits, and the producer can expire a waiting request's KV before the router ends the wait. Deploy engines whose attestation carries the lease, which the [engine launcher](../configuration/05-Engine-Launch.md#16-runtime-launch-records-and-image-verification) sets.

When a handoff reaches its bound before decode dispatch, the attempt ends. With attempts remaining, the request retries with a fresh prefill. Without them, the client receives HTTP 504 `handoff_expired`, and the journal records terminal `expired` with reason `handoff`. While every decode seat stays full, a retry waits for a decode seat again and can expire at the same bound.

### Changing the lease

[`runtime.kv_lease_s`](../configuration/05-Engine-Launch.md#16-runtime-launch-records-and-image-verification) sets the lease at engine launch. A longer lease lets a prefilled request wait longer for a decode seat, and a producer holds the KV blocks of an abandoned handoff for longer. A shorter lease frees producer KV blocks sooner, and decode-seat waits end sooner. Relaunch and attest the engines after you change the lease.

## Retries

`serving.max_attempts` sets the complete prefill and decode attempts for each admitted request, from `1` to `3`.

The router retries an attempt that fails before visible output with a transient fault: a transport error, an engine HTTP 408, 429, 500, 502, 503 or 504, or an expired KV handoff. A retry runs only while the original request deadline permits its backoff and a retry credit is available. After failed attempt `n`, the retry waits a random backoff between zero and this ceiling:

```text
min(serving.retry_cap_s, serving.retry_base_s * 2 ** (n - 1))
```

The retry then runs with fresh backend request IDs and a new KV handoff, on engines outside every engine whose leg failed earlier in the request. [Retries](../http-api/03-Backend-and-Failures.md#retries) gives the placement rules.

An exact input count that fails transiently also moves to another live engine, up to `serving.max_attempts` engines. These moves spend no retry credit.

| `serving.max_attempts` | Client outcome of a transient fault before visible output |
| --- | --- |
| `1` | The request ends with the fault's response: HTTP 502 or 504 with the phase, `prefill` or `decode`, as error type. The journal attempt entry carries `retry_reason` `attempt_limit`. |
| `2` | The request retries once. The retry can complete with HTTP 200, or end with any outcome in the next table. |
| `3` | The request retries up to twice, each time excluding the engines that failed it. Each retry adds a fresh prefill to prefill load. |

A retry can end the request with these responses:

| Condition | Client response | Journal |
| --- | --- | --- |
| The retry fails and no attempts remain | HTTP 502 or 504 with the phase as error type | `retry_reason` `attempt_limit` |
| The retry fails with a non-transient fault | HTTP 502 or 504 with the phase as error type | `retry_reason` `non_transient` |
| No live engine outside the request's failed engines can take a role's leg | HTTP 503 `backend_unavailable` | Reason `no_engine` |
| In `predictive` mode, the retry fails the decode admission check | HTTP 429 `server_overloaded_error` | Reason names the failed check, `retry_reason` `not_dispatched` |
| The KV handoff of the last attempt reaches its bound | HTTP 504 `handoff_expired` | Reason `handoff` |
| No retry credit remains | The response of the failed attempt | `retry_reason` `shared_budget` |
| The backoff would reach the original request deadline | The response of the failed attempt | `retry_reason` `original_deadline` |

A failure after visible output never retries. A streaming response then ends its HTTP 200 stream with a terminal error event of type `decode`. A request that completes on a later attempt increments `narwhal_served_after_retry_total`.

### Changing the attempt limit

- Set `1` when clients retry failed requests themselves, so that router retries do not add attempts on top of client retries.
- Set `3` only when each role keeps at least three live engines, counting unpinned engines that [aggregate fallback](../concepts/02-Role-Control.md#aggregate-fallback) can use. With fewer, a second retry can find no engine outside the request's failed engines and end with HTTP 503 `backend_unavailable`.

### Retry credits

The router shares one pool of retry credits across all requests. [Waiting, engine seats, and retries](../configuration/02-Serving-and-Role-Control.md#42-waiting-engine-seats-and-retries) defines `serving.retry_budget`, which sizes the pool, and `serving.retry_replenish`, which refills it.

While less than one credit remains, the router denies each retry, increments `narwhal_retry_denied_total`, and ends the request with its failed attempt's response.

Raise `serving.retry_budget` when `narwhal_retry_denied_total` rises during an engine failure while the remaining engines have spare capacity. Lower `serving.retry_replenish` to limit the prefill load that retries add during a sustained failure.

## Failure quarantine

`recovery.failure_quarantine_s` sets how long an engine stays out of placement for every request after one of its request legs fails. `0.0` turns quarantine off.

| Value | Placement after a failed leg | Client outcome |
| --- | --- | --- |
| `0.0` | A retry of the same request avoids the failed engine. Other requests can still be placed on it until the [breaker](../concepts/03-Failure-and-State.md#failure-evidence) takes it out of placement. The breaker acts after `recovery.eject_after` consecutive failures of one class, and its action depends on the class. | Each further request placed on a failing engine can fail and spend an attempt there. |
| Above `0` | Every new placement avoids a [covered](../concepts/03-Failure-and-State.md#failure-evidence) failed engine until the deadline, or until a health or inference probe passes. | Fewer requests reach a failing engine. While the engine is held out, the fleet has less capacity, and predictive refusals, seat waits or in-flight 429 responses can rise under load. |

A local connection-pool wait, and an engine HTTP 4xx response other than 408 or 429, start no quarantine. An engine that is not covered stays in placement. `narwhal_engine_held{kind="timed"}` and `holds.timed` in `/narwhal/state` report quarantined engines. [Failure quarantine](../concepts/03-Failure-and-State.md#failure-quarantine) describes when quarantine ends.

Set a positive value when one engine fails successive requests faster than the breaker removes it, and the remaining engines can carry the load for the quarantine period.

## In-flight limit

`serving.max_connections` sets the router's in-flight limit and the data connections the router opens to each engine. [`narwhal-serve --max-concurrent`](../cli/Serve.md) can set a lower in-flight limit, from `1` to `serving.max_connections`.

The router counts each completion request from its arrival until its response ends. When the counted requests reach the in-flight limit plus `serving.queue_capacity`, the router answers each new request with HTTP 429 `server_overloaded_error`, message `router in-flight limit reached`, and `Retry-After: 1`. It sends that response before it reads the body, so the predictive checks never run for these requests. The journal and `narwhal_rejected_total` record reason `inflight_limit`.

The in-flight limit protects the router, not goodput. It should bind before the router's [saturation check](../configuration/02-Serving-and-Role-Control.md#router-saturation) rejects requests for event-loop lag or request-sizing delay. A fleet usually stops meeting its SLO at an in-flight count well below that point, so in `open` mode the excess appears as SLO misses. To give clients a 429 instead of an SLO miss, use [`predictive` admission](#admission-mode).

| Change | Client outcome |
| --- | --- |
| Lower the limit | More requests receive the in-flight 429 at a lower load. |
| Raise the limit | More requests are admitted. Past the router's saturation point, clients receive HTTP 429 with reason `saturated` instead. |

To set the limit for your fleet, run an open-admission sweep that raises the in-flight requests past the limit, and record the in-flight count at which the first `saturated` 429 appears. Set `serving.max_connections` at or below that count. [In-flight limit rejections](../Troubleshoot.md#in-flight-limit-rejections) gives the diagnosis when this 429 rises in production.

## Decode stream gaps

`engine.decode_read_timeout_s` sets the longest silence the router accepts between two decode transport chunks after the first token. `0` turns the gap limit off.

When a gap reaches the limit, the decode leg fails with a timeout, reason `engine_timeout`, and the journal `error` contains `engine went silent between tokens`. The failure counts as `stream` [failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) against the decode engine.

| Request | Positive limit | `0` |
| --- | --- | --- |
| Streaming | The HTTP 200 stream ends with a terminal error event of type `decode` and code `failed`. | The stream stays open until the original request deadline, `serving.request_timeout_s`, and then ends with a terminal error event with code `expired`. |
| Non-streaming | The request retries when attempts remain, otherwise it receives HTTP 504 `decode`. | The original request deadline ends the request with HTTP 504 `request_expired`. |

After output starts, a failed stream does not retry, so a stalled stream on a hung decode engine ends after the limit.

Change the limit under these conditions:

- Raise it when served streams on your engines or workload show longer inter-chunk gaps, such as on a decode engine that runs long prefills locally.
- Lower it to end streams on a hung decode engine sooner, while keeping it above the largest gap of healthy streams.

A [configuration overlay](06-Controlling-the-Fleet.md#configuration-overlays-cold-restarts-and-restores) cannot change the `engine` section. Change this field in the fleet file, then restart the router.

## Checking a changed setting

Run a candidate value against the same request mix and offered load as the current value:

1. On a test fleet, apply the value with a [configuration overlay](06-Controlling-the-Fleet.md#configuration-overlays-cold-restarts-and-restores), or edit the fleet file and restart the router.
2. Run load at and above the fleet's capacity with a [load job](06-Controlling-the-Fleet.md#load-jobs).
3. Classify the outcomes by reason with the queries in [Fleet overload with healthy engines](../Troubleshoot.md#1-classify-outcomes-by-reason).
4. Compare completed requests and the requests meeting the SLO, from `narwhal_served_total` and `narwhal_slo_met_total`, with the current value's run.

The [dashboard](../observability/05-Dashboard.md#pool-pressure-admission-and-queue-depth) charts admission in-flight against its limit, queue depth by stage, waits and retry credits during the run.
