# Role control and capacity floors

## Role control

The role controller runs at most one regular evaluation per `controller.reactive.step_s`.

| Evaluation property | Value |
| --- | --- |
| Candidates | Every adjacent prefill/decode split, one engine move away from the current split |
| Price | The worst projected service-level objective (SLO) ratio, from measured demand and projected service times |
| Available moves | Limited by role floors and engine eligibility |
| Multi-engine moves | Allowed when a phase sits below its configured minimum size |

### Prefill queue projections

Narwhal projects FIFO prefill completion from these inputs:

- measured engine profiles;
- the live prefill pool;
- resident requests;
- output work waiting on prefill.

Candidate pricing uses the larger of the short- and long-horizon demand estimates.

If the projected time to first token (TTFT) exceeds `slo.ttft_s`, the role controller schedules one coalesced projected-TTFT recovery evaluation between its regular passes.

| Condition | Result |
| --- | --- |
| The adjacent decode-to-prefill split strictly improves projected service for the triggering request | The evaluation applies one adjacent move. |
| Otherwise | The split stays. |
| The queue drains before the evaluation | The trigger clears. |
| TTFT stays high after a move | The new state can trigger another evaluation. |
| Any recovery evaluation | `/narwhal/state` reports the decision under the [Projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields). |

### Expansion and consolidation

Two load thresholds steer the controller:

| Threshold | Condition | Effect |
| --- | --- | --- |
| `shrink` | Source load at or below the threshold | Drives consolidation |
| `expand` | Sustained prefill load at or above the threshold | Can move decode capacity into prefill |

Both paths clear the profile, safety, and confirmation checks before a role changes.

A decode-to-prefill move requires stable decode demand and a closed [arrival-evidence window](../configuration/02-Serving-and-Role-Control.md#76-evidence-gating-for-decode-to-prefill-consolidation).

| Window event | Condition |
| --- | --- |
| Closes | `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` samples exist. |
| Closes under sparse traffic | `controller.reactive.evidence_max_span_s` has elapsed. |
| Restarts | A first-token timeout or a prefill-to-decode recovery move. |

Moves toward decode, including emergency floor restoration, proceed while the window is open.

The [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting) fields in `/narwhal/state` report the demand, overflow, and inputs behind each role-controller decision.

### Guards on role changes

| Guard | Effect |
| --- | --- |
| Pinned engine | Keeps its configured role. |
| `controller.min_prefill`, `controller.min_decode` | Moves preserve these floors while enough healthy capacity remains. |
| `controller.thresholds.cooldown_s` | Minimum time between prefill-to-decode moves. |
| `controller.thresholds.dwell_s` | Keeps a recently moved engine in its new role for the configured interval. |
| `controller.thresholds.flip_resident_guard` | Ceiling on the lightest eligible decode donor's resident stream count before a decode-to-prefill move. |
| Lifecycle hold | Takes draining and recovering engines out of placement. |

Existing requests finish on their assigned engines after a role change.

A projected-TTFT recovery evaluation applies these guards plus checks of profile coverage, decode capacity, and consolidation evidence and trend.

### Advisory mode

With `controller.advisory` set to `true` for an [advisory rollout](../configuration/02-Serving-and-Role-Control.md#74-advisory-rollout), the role controller leaves the live split unchanged.

Each advisory decision records:

- the proposed split;
- the caller;
- the reason;
- the advisory result.

## Floors, fallback, and degraded capacity

### Decode-floor repair

If live decode capacity falls below its configured floor, each monitor pass moves one eligible engine to decode, keeping the prefill floor.

A readmitted engine gets its role from current demand.

### Aggregate fallback from an idle decode engine

If failures or drains empty the prefill pool, the scheduler selects a live decode-labelled engine as the aggregate fallback.

| Fallback engine state | Predictive admission |
| --- | --- |
| Resident decode work present | Rejects new work on the engine |
| Resident decode work drained | Prices the engine for aggregate prefill placement |
