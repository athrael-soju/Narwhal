---
description: How Narwhal decides which engines process prompts and which generate output, and how it keeps a minimum number of engines in each role.
---

# Role control and capacity floors

Narwhal splits its engines between two roles: prefill engines process incoming prompts, and decode engines generate the output tokens. The role controller moves engines between the roles as demand changes and keeps a minimum number of engines in each.

Every vLLM engine runs with the `kv_both` role, so a vLLM role change takes effect in the router, which sends the engine the legs of its new role. On SGLang, the router switches the engine's role through its `POST /pd_role_switch` route.

## Role control

During normal operation, the role controller runs at most one evaluation every `controller.reactive.step_s`. Each evaluation scores the current split of engines and every split that differs from it by one engine. A split's score is the worst of three ratios, each a projected value divided by its service-level objective (SLO) target: time to first token (TTFT), time per output token (TPOT), and the wait for a decode slot.

The [decode queueing ratio](../configuration/02-Serving-and-Role-Control.md#adjacent-split-decisions) covers requests waiting for a decode slot and requests still in prefill. For each request, it divides the projected wait for a decode slot by the time left in the request's `slo.ttft_s` budget once prefill finishes.

The demand estimate includes prompts longer than any in the profiled range, pricing them by extending the [prompt-processing cost curve](../telemetry/02-Profiles.md#prefill-price-derived-from-the-profile) past that range.

### Prefill queue projections

Narwhal projects when each prefill request will finish, serving requests in arrival order. The projection uses the measured engine profiles, the engines in the prefill role, the requests running on them, and the requests waiting for prefill.

When a request's projected TTFT exceeds `slo.ttft_s`, the role controller schedules a projected-TTFT recovery evaluation ahead of the next regular one, and triggers that arrive before it runs join the same evaluation. The evaluation takes the queued request with the highest projected TTFT and moves one decode engine to prefill when that move lowers the request's projected TTFT. Otherwise the split stays as it is.

The trigger clears once every queued request is projected to finish within `slo.ttft_s` at the time of the evaluation. When projected TTFT still exceeds `slo.ttft_s` after a move, the role controller schedules another evaluation. `/narwhal/state` reports each recovery decision in the [projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields).

### Expansion and consolidation

Consolidation takes an engine from a role with spare capacity. The role controller may consolidate when the projected load of the role giving up the engine, the source load, is at or below `controller.thresholds.shrink`. Expansion adds capacity to a role whose load reaches `controller.thresholds.expand`. With complete demand, a move from decode to prefill then needs one confirmation. The [`mixed_pressure` rule](../configuration/02-Serving-and-Role-Control.md#adjacent-split-decisions) allows expansion while the other role's load is above `controller.thresholds.shrink`. Both kinds of move must pass the profile, safety, and confirmation checks.

A move from decode to prefill needs stable decode demand and a closed [arrival-evidence window](../configuration/02-Serving-and-Role-Control.md#evidence-gating-for-decode-to-prefill-consolidation), the period in which the router collects enough arrivals to judge demand. While the window is open, the role controller may still move a prefill engine to decode when the source load is at or below `controller.thresholds.shrink`, and may repair a role's floor in either direction. When projected prefill load is above `controller.thresholds.shrink`, the role controller may move an engine back to prefill while the window is open. A window that a first-token timeout reset still holds this move.

On a prefill surge, the role controller may move an engine from decode back to prefill when all of these are true:

- projected prefill load is above `controller.thresholds.expand`
- projected decode load is above `controller.thresholds.shrink` and at or below `controller.thresholds.expand`
- no departure toward prefill is open

This move needs `max(controller.reactive.confirmations, controller.thresholds.sustained_intervals)` confirmations. It may reverse a departure toward decode. That departure then closes.

The window closes once `controller.reactive.evidence_span_s` has passed and at least `controller.reactive.evidence_min_arrivals` requests have arrived, or, under light traffic, once `controller.reactive.evidence_max_span_s` has passed. It starts over after a first-token timeout, after a move to decode takes effect, and after a decode floor repair.

In `/narwhal/state`, the [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting) fields show the demand, overflow, and inputs behind each decision, and the [controller decision](../http-api/05-Live-State.md#controller-decisions) fields name the rule each decision followed and the spans of demand it used.

### Departures from a settled split

A departure is a move away from a settled split when demand shifts. A split is settled while no split one engine away improves the worst projected SLO ratio by `controller.reactive.movement_margin` or more, both on window demand, measured over the full measurement window, and on confirmation-span demand, measured over a short recent span.

The confirmation span is `controller.reactive.step_s` times the sustained confirmation count, the larger of `controller.reactive.confirmations` and `controller.thresholds.sustained_intervals`. By default it is 15 s.

A departure reverses the role controller's recent moves when all of these are true as it starts:

- The role controller moved at least one engine in the preceding `controller.reactive.window_s` plus `controller.reactive.evidence_span_s`.
- Each of those moves went opposite the departure's direction.
- The latest of those moves is at least `controller.reactive.evidence_span_s` old.

The role controller starts a departure when all of these are true:

- The split has been settled for at least `controller.reactive.evidence_span_s`, or for one confirmation span less when the departure reverses the role controller's recent moves.
- For prefill or decode, confirmation-span demand differs from window demand by more than `controller.reactive.demand_rise_tolerance` times the larger of the two.
- On confirmation-span demand, a split one engine away improves the worst projected SLO ratio by at least `controller.reactive.movement_margin`.

The first move toward that split needs one confirmation and, on confirmation-span demand, a source load at or below `controller.thresholds.shrink`.

While the shift lasts, a departure keeps moving engines in its direction on confirmation-span demand, starting from the time in this table. Every other move uses window demand.

| Departure | Starting at                                           |
| --------- | ----------------------------------------------------- |
| Reversing | Its first move                                        |
| Other     | `controller.reactive.evidence_span_s` after it starts |

Once a departure has moved an engine, the role controller blocks moves in the opposite direction until [the departure closes](../configuration/02-Serving-and-Role-Control.md#adjacent-split-decisions), at most `controller.reactive.window_s` after it started. A prefill surge move is exempt from this block.

### Steady demand

Demand is steady when the arrival-evidence window is closed and confirmation-span demand has stayed within `controller.reactive.demand_rise_tolerance` of window demand for the last `controller.reactive.evidence_span_s`.

Under steady demand, the role controller may take an engine from a role whose source load is above `controller.thresholds.shrink` when all of these hold:

- The source load is at or below `controller.thresholds.expand`.
- The move lowers the worst projected SLO ratio by at least `controller.reactive.movement_margin`.
- The move has been confirmed for the sustained confirmation count.

### Incomplete demand

While [demand is incomplete](../http-api/06-SLO-and-Demand.md#demand-completeness), the role controller makes two kinds of move, each requiring a measured profile for every live engine: it moves an engine toward a role whose recovery ratio reaches `controller.thresholds.expand`, and it moves an engine to prefill after a projected-TTFT recovery evaluation. Departures and steady-demand moves resume once demand is complete.

### Guards on role changes

A pinned engine keeps its configured role. Moves keep at least `controller.min_prefill` prefill engines and `controller.min_decode` decode engines while enough healthy engines are available. Draining and recovering engines are on a lifecycle hold and take no new work.

`controller.thresholds.cooldown_s` is the minimum time between two moves from prefill to decode, and the first such move waits the same time after the router starts. A moved engine stays in its new role for at least `controller.thresholds.dwell_s`. A move from decode to prefill gives up the least busy eligible decode engine, and with `controller.thresholds.flip_resident_guard` above `0`, that engine may have at most that many decode requests still running.

While an engine is ejected, quarantined, draining, or recovering, the role controller scores splits on the remaining live engines. When that leaves a role with engines assigned and none of them live, it records a held decision.

Requests already running on a vLLM engine finish there after the engine changes role.

An SGLang engine changes roles only while idle. When every engine in the source role is busy, the role controller holds the least busy one out of new placements while another engine in its role can take them, and changes its role once it is idle if the controller still selects the move. The controller renews the hold each time it asks for the move, and at each evaluation that selects no move while the held engine still runs requests. The hold ends when any engine changes roles, or three `controller.reactive.step_s` intervals after its last renewal. The engine takes no work while the switch runs. When SGLang refuses the switch as not idle, the router retries it for up to 10 s. A switch that still fails ejects the engine.

A projected-TTFT recovery evaluation applies every guard above. It also needs a profile for every engine, decode KV capacity for the work of the new split, a closed arrival-evidence window, and recent decode demand at most `1 + controller.reactive.demand_rise_tolerance` times window demand.

### Advisory mode

When `controller.advisory` is `true`, as in an [advisory rollout](../configuration/02-Serving-and-Role-Control.md#advisory-rollout), the role controller leaves the live split as it is and records what it would have done: the proposed split, the trigger, the reason, and the advisory result.

## Floors, fallback, and degraded capacity

### Floor repair

When live decode engines drop below `controller.min_decode`, or live prefill engines drop below `controller.min_prefill`, engine monitoring repairs the shortfall one engine per monitoring pass. It moves an eligible engine from the other role while that role stays at or above its own minimum.

A readmitted engine returns to the role it last had.

### Aggregate fallback

On vLLM, when failures or drains leave a role with no live engines, the scheduler sends that role's work to a live, unpinned engine in the other role. When every live engine in the other role is pinned, the router rejects the request with HTTP 503 before prefill starts. On SGLang, each engine serves only its live role, so a role with no live engines makes the router reject the request with HTTP 503 before prefill starts.

Under predictive admission, the router prices prefill on a decode engine only once that engine's decode requests finish. When every candidate engine still carries decode work, it refuses the request with HTTP 429 and the cause `aggregate_unpriced`.
