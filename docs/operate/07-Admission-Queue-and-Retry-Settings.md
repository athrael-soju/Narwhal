---
description: Choose Narwhal's admission, queue, retry, quarantine, in-flight, and decode-gap settings and validate a change.
---

# Choosing admission, queue and retry settings

Restart the router after changing a fleet setting. Defaults and bounds are in [Serving and role control](../configuration/02-Serving-and-Role-Control.md). Client responses are in [Admission and refusal semantics](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) and [engine failure handling](../http-api/03-Backend-and-Failures.md#engine-failure-handling).

## Admission mode

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#global-admission) is `predictive` (the default) or `open`. In `open` mode the router skips the predictive checks, and the [request journal](../telemetry/01-Journal.md#admission-price) still records the price of each prefill placement.

- Keep `predictive` when clients should receive a refusal instead of a first token after the TTFT target.
- Use `open` for an in-flight sweep or any measurement that predictive refusals would distort.
- Before you rely on `predictive`, compare `price_s` plus `slot_wait_s` with `first_byte_s` on completed journal rows. When the median ratio is materially above or below 1, remeasure the [engine profiles](../measure/01-Profile.md).
- Raise `serving.admission_margin` (default `0.0`, a fraction of the TTFT target added to the admission budget) only when you accept first tokens beyond `slo.ttft_s`.

## Queue

With `serving.queue_capacity` at `0`, the default, the router answers HTTP 429 as soon as requests reach the in-flight limit. A positive value lets that many more requests wait, in arrival order, for an admission seat and then for a prefill seat.

Turn the queue on for short bursts when clients should wait for a seat. Set `serving.queue_timeout_s` (default `0.0`, and positive whenever the capacity is positive) to the longest acceptable wait before prefill. The admission-seat and prefill-seat waits share that budget. A wait for a decode seat ends at the KV handoff bound instead, as described below.

Under `predictive` admission, the router prices a request that waits for an admission seat with a cold prefill curve, before it sizes the prompt and without prefix-cache evidence. A long, mostly cached prompt can leave the queue with a `queue` or `prompt` refusal that its sized price would have passed. Review journal refusals for such prompts before you enable the queue. [Queue waits](../configuration/02-Serving-and-Role-Control.md#queue-waits) lists every wait, bound and response.

## Engine seats and KV handoff

To change decode seats, relaunch the engine with a different `runtime.max_sequences` and attest again, or remeasure the profile. To change prefill seats, remeasure the profile or change `slo.ttft_s`. [Engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) defines how the router derives both, and `/narwhal/state` reports the result under `seats`.

For vLLM and TensorRT-LLM engines, the router derives its [KV handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) from the KV lease. [`runtime.kv_lease_s`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification) sets the lease, at least 6 seconds. The default is 30 seconds on vLLM, a 25-second bound, and 60 seconds on TensorRT-LLM, a 50-second bound. A longer lease lets a decode-seat wait run longer and keeps abandoned handoff blocks longer. After you change it, relaunch and attest the engines.

## Retries and quarantine

`serving.max_attempts` (default `2`, from 1 to 3) caps the complete prefill and decode attempts per request. The router retries only an attempt that fails before visible output, and each retry spends one credit from the pool that `serving.retry_budget` (default `10`) sizes. Each successful original request adds `serving.retry_replenish` credits (default `0.1`). [Retries](../http-api/03-Backend-and-Failures.md#retries) lists the retryable failures and the placement rules.

- Set `serving.max_attempts` to `1` when clients retry failed requests themselves.
- Set it to `3` only when each role keeps at least three live engines, including unpinned engines that [aggregate fallback](../concepts/02-Role-Control.md#aggregate-fallback) can use. Check this before you [remove an engine](08-Joining-and-Removing-Engines.md#remove-an-engine), which lowers the count.
- Raise `serving.retry_budget` when `narwhal_retry_denied_total` rises during an engine failure and the remaining engines have spare capacity.
- Lower `serving.retry_replenish` to limit prefill load from retries during a sustained failure.

[`recovery.failure_quarantine_s`](../configuration/03-Recovery-and-Validation.md) defaults to `0.0`, which disables quarantine. A positive value keeps an engine that failed a request leg out of placement for that time while other live engines place its roles. Set one when an engine fails successive requests faster than the breaker removes it and the remaining engines can carry the load. [Failure quarantine](../concepts/03-Failure-and-State.md#failure-quarantine) gives the coverage and the release conditions.

## In-flight limit and decode gaps

To choose the in-flight limit, run an open-admission sweep, read `narwhal_admission_inflight` when the first `saturated` rejection appears, and set the limit at or below that count with `narwhal-serve --max-concurrent` or `serving.max_connections`. [In-flight limit](../configuration/02-Serving-and-Role-Control.md#in-flight-limit) and [Router saturation](../configuration/02-Serving-and-Role-Control.md#router-saturation) define the limit and the `inflight_limit` and `saturated` rejections. [In-flight limit rejections](../Troubleshoot.md#in-flight-limit-rejections) covers production diagnosis.

`engine.decode_read_timeout_s` (default `10.0`, `0` disables it) is the longest silent interval the router accepts between decode chunks after the first token. Raise it when healthy streams show longer gaps. Lower it to end a hung stream sooner, keeping it above the largest healthy gap. [Decode timeouts](../http-api/03-Backend-and-Failures.md#decode-timeouts) gives the client and journal outcomes.

## Check a changed setting

Compare the candidate value with the current one under the same request mix and offered load:

1. On a test fleet, apply the value with a [configuration overlay](fleet-control/07-Configuration-Changes.md#configuration-overlays), or edit the fleet file and restart the router.
2. Run load at and above fleet capacity with a [load job](fleet-control/06-Load-Jobs.md).
3. Classify the outcomes as in [Fleet overload with healthy engines](../Troubleshoot.md#classify-outcomes-by-reason).
4. Compare completed requests and requests meeting the SLO (`narwhal_served_total` and `narwhal_slo_met_total`) with the run at the current value.

The [dashboard](../observability/05-Dashboard.md#pool-pressure-admission-and-queue-depth) shows admission in-flight, queue depth, waits and retry credits.
