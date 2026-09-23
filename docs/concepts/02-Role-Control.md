# Role control and capacity floors

## Role control

During regular operation, Narwhal rate-limits role evaluations to one per `controller.reactive.step_s`, then prices adjacent prefill/decode splits from measured demand, projected service times, role floors, and engine eligibility using the worst projected SLO ratio.

A move may involve more than one engine when the current pool for one phase is already below its configured minimum size.

### Prefill queue projections

Every valid, locally sized prefill offer enters a bounded waiting set.

Narwhal computes a FIFO completion projection from:

- measured engine profiles;
- the live prefill pool;
- currently resident requests;
- output work awaiting prefill completion.

Candidate pricing uses the larger of the short- and long-horizon demand estimates.

If projected TTFT exceeds `slo.ttft_s`, Narwhal schedules one coalesced controller evaluation between regular controller passes.

At that evaluation, Narwhal:

1. rechecks the current queue;
2. prices the adjacent decode-to-prefill split;
3. compares projected service for the request that triggered the evaluation;
4. moves capacity when the candidate split strictly improves that projection.

The trigger disappears if the queue drains before the evaluation.

The controller retains the current split when one request's prefill duration alone exceeds the SLO and both splits produce the same projection.

One urgent wake may apply one adjacent move. If TTFT remains elevated after the topology changes, the new state can trigger another guarded evaluation.

### Expansion and consolidation

Narwhal uses two pressure directions:

- source pressure up to `shrink` drives consolidation;
- sustained prefill pressure at or above `expand` can move decode capacity into prefill.

Both paths pass profile, safety, and confirmation gates before any role changes.

Scheduled prefill-to-decode moves retain their normal cooldown and confirmation requirements.

Before moving a decode engine into prefill, Narwhal closes the arrival-evidence window and checks decode stability.

The evidence window closes when both conditions are satisfied:

- `controller.reactive.evidence_span_s` has elapsed;
- at least `controller.reactive.evidence_min_arrivals` samples exist.

Under sparse traffic, it closes at `controller.reactive.evidence_max_span_s`.

A first-token timeout or a decode-recovery move restarts the consolidation evidence window.

Decode expansion and emergency floor restoration may proceed using the open-window thresholds defined in the [role-control reference](../configuration/02-Serving-and-Role-Control.md#7-role-control).

`/narwhal/state` records retained demand, overflow, and the priced inputs used for each controller decision under the [demand accounting contract](../http-api/06-SLO-and-Demand.md#demand-accounting).

### Guards on role changes

Pinned engines keep their configured roles.

Moves preserve `controller.min_prefill` and `controller.min_decode` whenever enough healthy capacity remains.

`controller.thresholds.cooldown_s` limits moves toward decode.

`controller.thresholds.dwell_s` keeps a recently moved engine in its new role for the configured interval.

`controller.thresholds.flip_resident_guard` requires the lightest eligible decode donor's resident stream count to be at or below the configured ceiling before a decode-to-prefill move.

Narwhal applies role changes to new placements while existing requests continue on their assigned engines.

Lifecycle holds remove draining and recovering engines from placement.

Urgent decode-to-prefill evaluation still enforces:

- profile coverage;
- decode capacity;
- consolidation evidence;
- consolidation trend;
- role floors;
- health exclusions;
- pins;
- dwell;
- resident ownership;
- `flip_resident_guard`.

### Advisory mode

With:

```yaml
controller.advisory: true
```

Narwhal evaluates a proposed role move while the live split stays fixed.

It records:

- the proposed split;
- caller;
- reason;
- advisory result.

## Floors, fallback, and degraded capacity

### Decode-floor repair

If live decode capacity falls below its configured floor, each monitor pass restores one eligible engine.

A health failure can remove an engine from placement; Narwhal repairs the decode floor with another eligible engine while preserving the prefill floor.

When the failed engine later passes readmission, Narwhal assigns its role according to current demand.

### Aggregate fallback from an idle decode engine

If failures or drains empty the prefill pool, the scheduler can select an idle decode-labelled engine as the aggregate fallback.

While that engine owns decode work, predictive admission rejects new work because the measured curves price one phase at a time.

After resident decode work drains, Narwhal can price the engine for aggregate prefill placement.
