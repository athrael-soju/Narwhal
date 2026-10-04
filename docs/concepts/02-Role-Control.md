---
description: How Narwhal decides which engines process prompts and which generate output, and how it keeps a minimum number of engines in each role.
---

# Role control and capacity floors

Narwhal splits its engines between two roles. Prefill engines process incoming prompts, and decode engines generate the output tokens. The role controller moves engines between these roles as demand changes, while keeping a minimum number of engines in each role.

## Role control

During normal operation, the role controller runs at most one evaluation every `controller.reactive.step_s`. Each evaluation compares the current split of engines between prefill and decode with every split that differs from it by one engine. Each split gets a score based on three metrics: time to first token (TTFT), time per output token (TPOT), and how long requests wait for a decode slot. For each metric, the projected value is divided by its service-level objective (SLO) target, and the split's score is the worst of these ratios.

The [decode queueing ratio](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions) covers requests waiting for a decode slot and requests still in prefill. For each request, it divides the projected wait for a decode slot by the time left in the request's `slo.ttft_s` budget once prefill finishes.

Prompts longer than any in the test range are still included in the demand estimate. For these, the model extends the [prompt-processing cost curve](../telemetry/02-Profiles.md#prefill-price-derived-from-the-profile) beyond the tested range to estimate their cost.

### Prefill queue projections

Narwhal predicts when each prefill request will finish, assuming requests are served in the order they arrive. The prediction uses measured engine profiles, the engines currently in the prefill role, the requests already running on them, and the requests waiting for prefill.

When a request's projected TTFT is above `slo.ttft_s`, the role controller schedules an extra evaluation before the next regular one, called a projected-TTFT recovery evaluation. Any further triggers that arrive before it runs are handled by the same evaluation. The evaluation picks the queued request with the highest projected TTFT. If moving one decode engine to prefill would lower that request's projected TTFT, the role controller makes the move. Otherwise, it keeps the current split.

The trigger clears once every queued request is projected to finish within `slo.ttft_s` at the time of the evaluation. If projected TTFT is still above `slo.ttft_s` after a move, the role controller schedules another evaluation. `/narwhal/state` reports each recovery decision under the [Projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields).

### Expansion and consolidation

Consolidation takes an engine away from a role that has spare capacity. The role controller can consolidate when the projected load of the role giving up the engine, called the source load, is at or below `controller.thresholds.shrink`. Expansion adds prefill capacity. When prefill load stays at or above `controller.thresholds.expand`, the role controller can move decode engines to prefill. Both kinds of move must pass the profile, safety, and confirmation checks.

Before the role controller moves a decode engine to prefill, two things must be true: the [arrival-evidence window](../configuration/02-Serving-and-Role-Control.md#76-evidence-gating-for-decode-to-prefill-consolidation), the period used to collect enough arrivals to judge demand, must be closed, and decode demand must be stable. Two kinds of move are allowed while the window is still open: moving a prefill engine to decode when the source load is at or below `controller.thresholds.shrink`, and floor repair (restoring a role's minimum engine count) in either direction.

The window closes when `controller.reactive.evidence_span_s` has passed and at least `controller.reactive.evidence_min_arrivals` requests have arrived, or when `controller.reactive.evidence_max_span_s` has passed if traffic is light. The window starts over after a first-token timeout, after a move to decode takes effect, or after a decode floor repair.

In `/narwhal/state`, the [demand accounting](../http-api/06-SLO-and-Demand.md#demand-accounting) fields show the demand, overflow, and inputs behind each role-controller decision. The [controller decision](../http-api/05-Live-State.md#controller-decisions) fields name the rule each decision followed and the time spans of demand it used.

### Departures from a settled split

A departure is a change away from a split that has been stable, made when demand shifts. A split counts as settled while no split one engine away would improve the worst projected SLO ratio by `controller.reactive.movement_margin` or more. This must hold for both demand measures: window demand, measured over the full measurement window, and confirmation-span demand, measured over a short recent span.

The confirmation span is `controller.reactive.step_s` multiplied by the sustained confirmation count. That count is the larger of `controller.reactive.confirmations` and `controller.thresholds.sustained_intervals`. By default, the span is 15 s.

A departure reverses the role controller's recent moves when all of these are true as it starts:

- The role controller moved at least one engine in the preceding `controller.reactive.window_s` plus `controller.reactive.evidence_span_s`.
- Each of those moves went opposite the departure's direction.
- The latest of those moves is at least `controller.reactive.evidence_span_s` old.

The role controller starts a departure when all of these are true:

- The split has been settled for at least `controller.reactive.evidence_span_s`, or for one confirmation span less when the departure reverses the role controller's recent moves.
- For prefill or decode, confirmation-span demand differs from window demand by more than `controller.reactive.demand_rise_tolerance` times whichever of the two is larger.
- Based on confirmation-span demand, a split one engine away improves the worst projected SLO ratio by at least `controller.reactive.movement_margin`.

The role controller makes the first move toward that split only if both of these are true:

- The move has one confirmation.
- Based on confirmation-span demand, the source load is at or below `controller.thresholds.shrink`.

While the shift lasts, a departure can keep moving engines in its direction based on confirmation-span demand:

| Departure | Starting at                                           |
| --------- | ----------------------------------------------------- |
| Reversing | Its first move                                        |
| Other     | `controller.reactive.evidence_span_s` after it starts |

All other moves are based on window demand.

Once a departure has moved an engine, the role controller blocks moves in the opposite direction until [the departure closes](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions), which is at most `controller.reactive.window_s` after it started.

### Steady demand

Demand counts as steady when the arrival-evidence window is closed and, for the last `controller.reactive.evidence_span_s`, confirmation-span demand has stayed within `controller.reactive.demand_rise_tolerance` of window demand.

When demand is steady, the role controller can take an engine from a role whose source load is above `controller.thresholds.shrink` only if all of these hold:

- The source load is at or below `controller.thresholds.expand`.
- The move lowers the worst projected SLO ratio by at least `controller.reactive.movement_margin`.
- The move has been confirmed for the sustained confirmation count.

### Incomplete demand

When [demand is incomplete](../http-api/06-SLO-and-Demand.md#demand-completeness), the role controller makes only two kinds of move: moving an engine toward a role whose recovery ratio reaches `controller.thresholds.expand`, and moving an engine to prefill after a projected-TTFT recovery evaluation. Both require a measured profile for every live engine. Departures and steady-demand moves wait until demand is complete again.

### Guards on role changes

A pinned engine always keeps its configured role. Moves keep at least `controller.min_prefill` prefill engines and `controller.min_decode` decode engines, as long as enough healthy engines are available. Engines that are draining or recovering are on a lifecycle hold and receive no new work.

`controller.thresholds.cooldown_s` is the minimum time between two moves from prefill to decode. A moved engine stays in its new role for at least `controller.thresholds.dwell_s`. When a decode engine moves to prefill, the engine given up is the least busy eligible decode engine, and it can have no more than `controller.thresholds.flip_resident_guard` requests still running on it.

While an engine is ejected, quarantined, draining, or recovering, the role controller scores splits using only the remaining live engines. If that leaves a role with engines assigned but none of them live, the role controller records a held decision.

After an engine changes role, requests already running on it finish there.

A projected-TTFT recovery evaluation applies all the guards above. It also checks that every engine has a profile, that enough decode capacity remains, that there is enough evidence to consolidate, and which way decode demand is trending.

### Advisory mode

When `controller.advisory` is `true`, as in an [advisory rollout](../configuration/02-Serving-and-Role-Control.md#74-advisory-rollout), the role controller doesn't change the live split. It only records what it would have done: the proposed split, what triggered the evaluation, the reason, and the advisory result.

## Floors, fallback, and degraded capacity

### Floor repair

If live decode engines drop below `controller.min_decode`, or live prefill engines drop below `controller.min_prefill`, engine monitoring fixes the shortfall one engine per monitoring pass. It moves an eligible engine from the other role, as long as that role stays at or above its own minimum.

An engine that is readmitted returns to the role it last had.

### Aggregate fallback

If failures or drains leave a role with no engines, the scheduler sends that role's work to a live, unpinned engine in the other role. If every live engine in the other role is pinned, the router rejects the request with HTTP 503 before prefill starts.

When a decode engine takes on prefill work, predictive admission refuses new work on it until its in-progress decode requests finish. After that, admission includes the engine when placing prefill work and estimates its cost as an engine handling both phases.