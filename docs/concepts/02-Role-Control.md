# Role control and capacity floors

## Role control

Narwhal evaluates roles at most once per `controller.reactive.step_s`. Each evaluation scores every adjacent prefill/decode split by its worst projected SLO ratio, using measured demand and projected service times. Candidate pricing uses the larger of the short- and long-horizon demand estimates. Role floors and engine eligibility constrain the available moves.

A move involves more than one engine when the current pool for one phase is below its configured minimum size.

### Urgent evaluation on TTFT projections

Every valid prefill offer sized for this node enters a bounded waiting set. Narwhal computes a FIFO completion projection from:

- measured engine profiles
- the live prefill pool
- currently resident requests
- output work still waiting on prefill to complete

If projected TTFT exceeds `slo.ttft_s`, Narwhal schedules one urgent evaluation between regular controller passes. The urgent evaluation:

1. Rechecks the current queue.
2. Prices the adjacent decode-to-prefill split.
3. Compares the projected service time of the request that triggered the evaluation under the current split and under the candidate split.
4. Moves capacity only if the candidate split strictly improves that projection.

The evaluation is skipped if the queue drains first. If a single request's prefill time alone exceeds the SLO and both splits give the same projection, the current split is kept.

Each urgent evaluation applies at most one adjacent move. If TTFT is still above `slo.ttft_s` afterwards, the new state triggers another urgent evaluation.

### Expansion and consolidation

Two conditions trigger role moves:

- Source pressure at or below `controller.thresholds.shrink` drives consolidation.
- Sustained prefill pressure at or above `controller.thresholds.expand` moves decode capacity into prefill.

Both pass profile, safety, and confirmation gates, including the [guards on role changes](#guards-on-role-changes). Scheduled prefill-to-decode moves also keep their normal cooldown and confirmation requirements.

A decode-to-prefill move requires a closed evidence window and stable decode capacity. The evidence window closes when `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` arrivals have been recorded. Under sparse traffic it closes at `controller.reactive.evidence_max_span_s`. A first-token timeout or a decode-recovery move restarts the window.

Decode expansion and emergency floor restoration proceed before the window closes. They use the open-window thresholds in the [role-control reference](../configuration/02-Serving-and-Role-Control.md#7-role-control).

### Guards on role changes

These guards apply to role moves, including urgent decode-to-prefill evaluations:

- Pinned engines keep their configured roles.
- Moves preserve `controller.min_prefill` and `controller.min_decode` whenever enough healthy capacity remains.
- `controller.thresholds.cooldown_s` sets the minimum interval between prefill-to-decode moves.
- `controller.thresholds.dwell_s` keeps a recently moved engine in its new role for the configured interval.
- When `controller.thresholds.flip_resident_guard` is above `0`, the lightest eligible decode donor must have a resident stream count at or below that ceiling before a decode-to-prefill move.
- Draining and recovering engines are excluded from placement.

Urgent decode-to-prefill evaluations also enforce the pin, role-floor, dwell, `flip_resident_guard`, and health-exclusion guards above, resident ownership, profile coverage, sufficient decode capacity, and the consolidation evidence and trend checks.

Role changes apply to new placements. Existing requests continue on their assigned engines.

### Advisory mode

When `controller.advisory` is `true`, Narwhal evaluates a proposed role move and leaves the live split unchanged. It records the proposed split, the caller, the reason, and the advisory result.

## Floors, fallback, and degraded capacity

### Decode-floor repair

If live decode capacity falls below its configured floor, each monitor pass restores one eligible engine.

When a health failure removes an engine from placement, Narwhal fills the decode floor from another eligible engine while keeping the prefill floor. When the failed engine passes readmission, Narwhal assigns its role according to current demand.

### Aggregate fallback from an idle decode engine

If failures or drains empty the prefill pool, the scheduler selects a live decode-labeled engine as the aggregate fallback.

Measured profiles price one phase at a time, so predictive admission rejects new work on that engine until its resident decode work drains. After that, Narwhal prices the engine for aggregate prefill placement.

## See also

`/narwhal/state` reports retained demand, overflow, and the priced inputs for each controller decision. See [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting).
