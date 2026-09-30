---
description: How the Narwhal role controller moves engines between prefill and decode while holding capacity floors.
---

# Role control and capacity floors

## Role control

The role controller runs at most one regular evaluation per `controller.reactive.step_s`.

| Evaluation property | Value |
| --- | --- |
| Candidates | Every adjacent prefill/decode split, one engine move away from the current split |
| Price | The worst projected service-level objective (SLO) ratio across TTFT, TPOT, and decode queueing |
| Demand input | Measured window demand, with the larger of the short- and long-horizon decode estimates for decode-to-prefill candidates |
| Available moves | Limited by role floors and engine eligibility |
| Floor restoration | One engine per monitor pass while a phase sits below its configured floor |

### Prefill queue projections

FIFO prefill completion projection inputs:

- measured engine profiles
- the live prefill pool
- resident prefill requests
- requests waiting for prefill

Projected time to first token (TTFT) above `slo.ttft_s` triggers one coalesced projected-TTFT recovery evaluation between regular passes.

| Condition | Result |
| --- | --- |
| The adjacent decode-to-prefill split strictly improves projected service for the triggering request | The evaluation applies one adjacent move. |
| Otherwise | The split stays. |
| The queue drains before the evaluation | The trigger clears. |
| TTFT stays high after a move | The new state can trigger another evaluation. |
| Any recovery evaluation | `/narwhal/state` reports the decision under the [Projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields). |

### Expansion and consolidation

| Threshold | Condition | Effect |
| --- | --- | --- |
| `shrink` | Source load at or below the threshold | Drives consolidation |
| `expand` | Sustained prefill load at or above the threshold | Can move decode capacity into prefill |

Both paths require passing the profile, safety, and confirmation checks.

[Arrival-evidence window](../configuration/02-Serving-and-Role-Control.md#76-evidence-gating-for-decode-to-prefill-consolidation) requirements by move:

| Move | Requirement |
| --- | --- |
| Decode to prefill, by the role controller | A closed window and stable decode demand |
| Prefill to decode | Proceeds with the window open |
| Floor restoration, in either direction | Proceeds with the window open |

| Window event | Condition |
| --- | --- |
| Closes | `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` arrivals exist. |
| Closes under sparse traffic | `controller.reactive.evidence_max_span_s` has elapsed. |
| Restarts | A first-token timeout, an applied move toward decode, or a decode-floor restoration. |

The [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting) fields in `/narwhal/state` report the demand, overflow, and inputs behind each role-controller decision.

### Guards on role changes

| Guard | Effect |
| --- | --- |
| Pinned engine | Keeps its configured role. |
| `controller.min_prefill`, `controller.min_decode` | Moves preserve these floors when enough healthy capacity remains. |
| `controller.thresholds.cooldown_s` | Minimum time between prefill-to-decode moves. |
| `controller.thresholds.dwell_s` | Keeps a recently moved engine in its new role for the configured interval. |
| `controller.thresholds.flip_resident_guard` | Ceiling on the lightest eligible decode donor's resident stream count before a decode-to-prefill move. |
| Lifecycle hold | Takes draining and recovering engines out of placement. |

The role controller scores splits over live engines, so it keeps moving roles while an engine is ejected, draining or recovering. It holds while a configured role has no live engine.

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

| Floor breach | Repair on each monitor pass |
| --- | --- |
| Live decode engines below `controller.min_decode` | Moves one eligible prefill engine to decode, within `controller.min_prefill` |
| Live prefill engines below `controller.min_prefill` | Moves one eligible decode engine to prefill, within `controller.min_decode` |

A readmitted engine rejoins placement in its last assigned role.

### Aggregate fallback from an idle decode engine

If failures or drains empty a phase's pool, the scheduler places that phase on a live unpinned engine of the other role.

With only pinned engines of the other role live, the router answers HTTP 503 before prefill.

| Fallback engine state | Predictive admission |
| --- | --- |
| Resident decode work present | Rejects new work on the engine |
| Resident decode work drained | Prices the engine for aggregate prefill placement |
