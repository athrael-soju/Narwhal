---
description: Choose Narwhal's admission, queue, retry, quarantine, in-flight, and decode-gap settings and validate a change.
---

# Choosing admission, queue and retry settings

[Serving and role control](../configuration/02-Serving-and-Role-Control.md) defines field defaults, bounds, and router behaviour. [Admission and refusal semantics](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) and [engine failure handling](../http-api/03-Backend-and-Failures.md#engine-failure-handling) define client responses.

Restart the router after changing a fleet setting.

## Admission mode

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#global-admission) selects predictive or open admission. The [request journal](../telemetry/01-Journal.md#admission-price) records the price used for each prefill placement.

- Keep predictive admission when clients should receive a refusal instead of a first token after the TTFT target.
- Use open admission for an in-flight sweep or another measurement without predictive refusals.
- Compare `price_s` with `ttft_s` on completed journal rows before relying on predictive admission. Remeasure the [engine profiles](../measure/01-Profile.md) when the median ratio is materially above or below 1.
- Raise `serving.admission_margin` only when you accept first tokens beyond `slo.ttft_s`.

## Queue

[`serving.queue_capacity` and `serving.queue_timeout_s`](../configuration/02-Serving-and-Role-Control.md#waiting-engine-seats-and-retries) control waits for admission and engine seats. [Queue waits](../configuration/02-Serving-and-Role-Control.md#queue-waits) defines their bounds and outcomes.

Turn the queue on for short bursts when clients should wait for a seat. Set `serving.queue_timeout_s` to the longest acceptable wait before prefill. Review journal refusals for long, mostly cached prompts before enabling it under predictive admission.

## Engine seats and KV handoff

The router derives [engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) from attestation and profiles. To change decode seats, relaunch with a different `--max-num-seqs` and attest again, or remeasure the profile. Prefill seats follow the profile and `slo.ttft_s`.

The [KV handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) comes from the lease in engine attestation. [`runtime.kv_lease_s`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification) sets the lease at engine launch. A longer lease permits a longer decode-seat wait and retains abandoned handoff blocks longer. Relaunch and attest engines after changing it. [KV handoff expiry](../http-api/03-Backend-and-Failures.md#kv-handoff-expiry) defines the client outcome.

## Retries and quarantine

[`serving.max_attempts` and retry credits](../configuration/02-Serving-and-Role-Control.md#waiting-engine-seats-and-retries) bound retries before visible output. [Retries](../http-api/03-Backend-and-Failures.md#retries) defines eligible failures, placement, responses, and stream behaviour.

- Set `serving.max_attempts` to `1` when clients retry failed requests themselves.
- Set it to `3` only when each role keeps at least three live engines, including unpinned engines that [aggregate fallback](../concepts/02-Role-Control.md#aggregate-fallback) can use.
- Raise `serving.retry_budget` when `narwhal_retry_denied_total` rises during an engine failure and the remaining engines have spare capacity.
- Lower `serving.retry_replenish` to limit prefill load from retries during a sustained failure.

[`recovery.failure_quarantine_s`](../configuration/03-Recovery-and-Validation.md) controls the timed hold after a failed request leg. [Failure quarantine](../concepts/03-Failure-and-State.md#failure-quarantine) defines coverage and release. Set a positive value when one engine fails successive requests faster than the breaker removes it and the remaining engines can carry the load.

## In-flight limit and decode gaps

[`serving.max_connections`](../configuration/02-Serving-and-Role-Control.md#global-admission) protects router resources; [`narwhal-serve --max-concurrent`](../cli/Serve.md) can lower its in-flight limit. Use an open-admission sweep to find the first saturation rejection, then set the limit at or below that count. [In-flight limit rejections](../Troubleshoot.md#in-flight-limit-rejections) gives the production diagnosis.

[`engine.decode_read_timeout_s`](../configuration/02-Serving-and-Role-Control.md#streaming-failure-semantics) bounds decode silence. [Decode timeouts](../http-api/03-Backend-and-Failures.md#decode-timeouts) defines the client and journal outcomes. Raise the limit when healthy streams show longer inter-chunk gaps; lower it to end a hung stream sooner while keeping it above the largest healthy gap.

## Check a changed setting

Run the candidate against the current value with the same request mix and offered load:

1. On a test fleet, apply the value with a [configuration overlay](fleet-control/07-Configuration-Changes.md#configuration-overlays), or edit the fleet file and restart the router.
2. Run load at and above fleet capacity with a [load job](fleet-control/06-Load-Jobs.md).
3. Classify outcomes with [Fleet overload with healthy engines](../Troubleshoot.md#classify-outcomes-by-reason).
4. Compare completed requests and requests meeting the SLO from `narwhal_served_total` and `narwhal_slo_met_total` with the current-value run.

The [dashboard](../observability/05-Dashboard.md#pool-pressure-admission-and-queue-depth) shows admission in-flight, queue depth, waits, and retry credits.
