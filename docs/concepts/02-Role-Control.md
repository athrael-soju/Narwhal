# Role control and capacity floors

## Role control

During regular operation, the role controller performs at most one regular evaluation per `controller.reactive.step_s`.

Each evaluation prices every adjacent prefill/decode split by its worst projected service-level objective (SLO) ratio, using measured demand and projected service times. An adjacent split moves one engine between the two phases. Role floors and engine eligibility limit which moves are available. When a phase sits below its configured minimum size, a move can shift several engines.

### Prefill queue projections

Every valid, locally sized prefill offer enters a bounded waiting set. Narwhal projects FIFO completion from measured engine profiles, the live prefill pool, resident requests, and output work still waiting on prefill. Candidate pricing uses the larger of the short- and long-horizon demand estimates.

If the projected time to first token (TTFT) exceeds `slo.ttft_s`, the role controller schedules one coalesced projected-TTFT recovery evaluation between its regular passes.

The evaluation rechecks the current queue, prices the adjacent decode-to-prefill split, and compares projected service for the triggering request. It moves capacity only when the candidate split strictly improves that projection. If the queue drains first, the trigger clears. A single request whose prefill alone exceeds the SLO leaves the split unchanged when both splits project the same.

Each recovery evaluation applies at most one adjacent move. If TTFT stays high after a move, the new state can trigger another evaluation.

`/narwhal/state` reports these decisions under the [Projected-TTFT recovery fields](../http-api/05-Live-State.md#projected-ttft-recovery-fields).

### Expansion and consolidation

Two load thresholds steer the controller:

- source load at or below `shrink` drives consolidation;
- sustained prefill load at or above `expand` can move decode capacity into prefill.

Both paths must clear the profile, safety, and confirmation checks before a role changes.

A decode-to-prefill move additionally requires a closed arrival-evidence window and stable decode demand.

The evidence window closes once `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` samples exist. Under sparse traffic it closes at `controller.reactive.evidence_max_span_s` instead. A first-token timeout or a prefill-to-decode recovery move restarts the window.

Moves toward decode, including emergency floor restoration, can proceed while the window is still open. [Evidence gating for decode-to-prefill consolidation](../configuration/02-Serving-and-Role-Control.md#76-evidence-gating-for-decode-to-prefill-consolidation) defines the gate.

`/narwhal/state` shows the demand, overflow, and inputs behind each role-controller decision; see the [demand accounting contract](../http-api/06-SLO-and-Demand.md#demand-accounting).

### Guards on role changes

Pinned engines keep their configured roles, and moves preserve `controller.min_prefill` and `controller.min_decode` whenever enough healthy capacity remains.

`controller.thresholds.cooldown_s` sets the minimum time between prefill-to-decode moves. `controller.thresholds.dwell_s` keeps a recently moved engine in its new role for the configured interval.

`controller.thresholds.flip_resident_guard` requires the lightest eligible decode donor's resident stream count to be at or below the configured ceiling before a decode-to-prefill move.

Role changes affect new placements. Existing requests finish on their assigned engines. Lifecycle holds take draining and recovering engines out of placement.

A projected-TTFT recovery evaluation honors these guards and also checks profile coverage, decode capacity, and consolidation evidence and trend.

### Advisory mode

With `controller.advisory` at `true`, the role controller evaluates proposed role moves and leaves the live split alone. It records the proposed split, the caller, the reason, and the advisory result.

Fleet setup is in [Advisory rollout](../configuration/02-Serving-and-Role-Control.md#74-advisory-rollout).

## Floors, fallback, and degraded capacity

### Decode-floor repair

If live decode capacity falls below its configured floor, each monitor pass restores one eligible engine while preserving the prefill floor. A health failure removes an engine from placement, so repair picks another eligible engine.

When the failed engine later passes readmission, Narwhal assigns its role according to current demand.

### Aggregate fallback from an idle decode engine

If failures or drains empty the prefill pool, the scheduler selects a live decode-labelled engine as the aggregate fallback.

Predictive admission rejects new work on that engine until its resident decode work drains, because the measured curves price one phase at a time. Once the engine has drained, Narwhal prices it for aggregate prefill placement.
