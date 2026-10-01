---
description: How the Narwhal role controller moves engines between prefill and decode while holding capacity floors.
---

# Role control and capacity floors

## Role control

The role controller runs at most one regular evaluation per `controller.reactive.step_s`. Each evaluation considers every adjacent prefill/decode split, one engine move away from the current split. It prices each split by the worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing.

The evaluation prices splits from measured window demand. Decode-to-prefill candidates use the larger of the short- and long-horizon decode estimates. Role floors and engine eligibility limit the available moves, and floor restoration moves one engine per monitor pass while a phase sits below its configured floor.

### Prefill queue projections

Narwhal projects FIFO prefill completion from:

- measured engine profiles
- the live prefill pool
- resident prefill requests
- requests waiting for prefill

Projected TTFT above `slo.ttft_s` triggers one coalesced projected-TTFT recovery evaluation between regular passes. The evaluation applies one adjacent move when the adjacent decode-to-prefill split strictly improves projected service for the triggering request. Otherwise the split stays.

When the queue drains before the evaluation, the trigger clears. When TTFT stays high after a move, the new state can trigger another evaluation. `/narwhal/state` reports each recovery decision under the [Projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields).

### Expansion and consolidation

Source load at or below `controller.thresholds.shrink` drives consolidation. Sustained prefill load at or above `controller.thresholds.expand` can move decode capacity into prefill. Both paths require passing the profile, safety, and confirmation checks.

A decode-to-prefill move by the role controller requires a closed [arrival-evidence window](../configuration/02-Serving-and-Role-Control.md#76-evidence-gating-for-decode-to-prefill-consolidation) and stable decode demand. Prefill-to-decode moves and floor restorations in either direction proceed with the window open.

The window closes when `controller.reactive.evidence_span_s` has elapsed with at least `controller.reactive.evidence_min_arrivals` arrivals, or when `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic. A first-token timeout, an applied move toward decode, or a decode-floor restoration restarts the window.

The [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting) fields in `/narwhal/state` report the demand, overflow, and inputs behind each role-controller decision.

### Guards on role changes

A pinned engine keeps its configured role. Moves preserve the `controller.min_prefill` and `controller.min_decode` floors when enough healthy capacity remains. A lifecycle hold takes draining and recovering engines out of placement.

`controller.thresholds.cooldown_s` sets the minimum time between prefill-to-decode moves. `controller.thresholds.dwell_s` keeps a recently moved engine in its new role for the configured interval. `controller.thresholds.flip_resident_guard` caps the lightest eligible decode donor's resident stream count before a decode-to-prefill move.

While an engine is ejected, quarantined, draining, or recovering, the role controller scores splits over the other live engines. When that leaves a role with assigned engines at zero live engines, the role controller records a held decision.

Existing requests finish on their assigned engines after a role change.

A projected-TTFT recovery evaluation applies:

- the guards above
- the profile coverage check
- the decode capacity check
- the consolidation evidence and trend check

### Advisory mode

With `controller.advisory` set to `true` for an [advisory rollout](../configuration/02-Serving-and-Role-Control.md#74-advisory-rollout), the role controller leaves the live split unchanged.

Each advisory decision records:

- the proposed split
- the caller
- the reason
- the advisory result

## Floors, fallback, and degraded capacity

### Floor repair

Engine monitoring repairs a floor breach one engine per monitor pass. With live decode engines below `controller.min_decode`, it moves one eligible prefill engine to decode, within `controller.min_prefill`. With live prefill engines below `controller.min_prefill`, it moves one eligible decode engine to prefill, within `controller.min_decode`.

A readmitted engine rejoins placement in its last assigned role.

### Aggregate fallback

If failures or drains empty a phase's pool, the scheduler places that phase on a live unpinned engine of the other role.

With only pinned engines of the other role live, the router answers HTTP 503 before prefill.

On a decode engine that takes prefill, predictive admission rejects new work while resident decode work is present. With the resident decode work drained, predictive admission prices the engine for aggregate prefill placement.
