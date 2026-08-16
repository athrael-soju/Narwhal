# Architectures

This page describes four ways to organize a fleet for LLM serving and
the case for the one Narwhal implements. *The Price of Order in
Disaggregated Inference* measures all four on one fleet with one
trace, and it carries the numbers. In the figures, prefill is navy
and decode is olive.

## The argument in four moves

In aggregated serving both phases share a batch, so a long prefill
stalls every decode beside it, and attainment falls as rate rises.
Static disaggregation removes the interference but pins the ratio,
and production traces move their phase mix minute to minute (Arrow §3.1),
so a pinned ratio is wrong for most of a trace. The split has to
adapt, so the remaining question is the price of a move. Cold-swap
pays minutes of offline capacity per move, which amortizes only when
the workload shifts slowly. Hot-swap prices a move as a label write,
because roles attach to requests rather than instances (Arrow §5.2).
Adaptation settles in seconds and capacity never leaves the fleet.
The price is an engine contract (stateless, any-peer KV) and a
control loop worth tuning.

## Aggregated (colocated)

![Fig. 1, aggregated: one pool of identical replicas. Simplest to run, but the phases interfere and a long prefill stalls every decode on its node.](../assets/architectures/aggregated.svg)

*Fig. 1 - one pool of identical replicas. Both phases contend for the
same compute and HBM, so a long prefill stalls every decode on that
node.*

Every instance runs both phases, so a chunked long prefill and a
latency-sensitive decode step share the same batch. Attainment falls
monotonically with rate, with the misses concentrated in
prefill-heavy stretches, and it collapses under pressure. It is the
simplest fleet to run and the first to fail when phases collide.

## Static disaggregated

![Fig. 2, static disaggregated: fixed prefill and decode pools with per-request KV handoff. The ratio is pinned, so floods queue on one side while the other pool idles.](../assets/architectures/static.svg)

*Fig. 2 - a fixed 2:2 split with roles set at deploy time and no
runtime controller. Prefill runs compute-bound, decode runs
memory-bound, and KV caches stream across the fabric per request.*

Fixed prefill and decode pools, with KV crossing per request. It fits
the workload it was sized for and no other. On a stationary trace a
well-sized pin is the strongest arm, because it pays no adaptation
overhead. When the optimum moves, the pinned split queues floods on
one side while capacity idles on the other. The study behind this
page measured the scope of that weakness: decode floods did not
punish the pin at any rate measured, and prefill walls punish it
catastrophically past a measurable knee. The value of adaptation on a
given day is the fraction of the day spent past such knees.

## Adaptive cold-swap (drain-and-reprovision)

![Fig. 3, adaptive cold-swap: a planner drains and re-provisions a node between pools. It works with stock engines, but each move costs minutes of offline capacity.](../assets/architectures/drain.svg)

*Fig. 3 - a planner re-provisions node-02 into the decode pool. It
drains in-flight work and tears down the engine, then boots the new
role with the weights reloading. The moving capacity is offline for
the whole transition.*

Pools resize, but a role change drains the instance and reloads it in
its new role. §2 of the Arrow paper characterizes these transitions
at minutes each. The moving capacity is offline during each
transition, so adaptation pays only when phases last much longer than
the move. On a workload whose optimum moves faster than that, chasing
it with cold swaps scores worse than never adapting at all.

## Adaptive hot-swap (this project)

![Fig. 4, adaptive hot-swap: a node's role flips in place with the weights resident and no capacity offline. The price is capable engines and a tuned control loop.](../assets/architectures/hotswap.svg)

*Fig. 4 - node-02 changes phase in place as the mix shifts. The
weights stay resident, the label changes, and the pool boundary
flexes in seconds.*

Every engine can serve either phase: instances are stateless, with KV
transferable to any peer (Arrow §5.2). A role is a label the scheduler
holds, so a re-split costs one label write plus the KV handoff its
requests already pay.
When the optimum moves, the adaptive fleet follows it. When the
optimum holds still, the same adaptivity costs a small overhead
against the best pin. The damping knobs buy most of that back.

---

The full measured comparison ships with *The Price of Order in
Disaggregated Inference*: every architecture on the same fleet,
trace, and build, with error bars, the journal-side scorer, and each
run's configuration. The drain-and-reprovision characterization comes
from the Arrow paper, where it motivates stateless instances. To
measure the comparison on your own fleet, see
[Benchmarking](Benchmarking.md).
